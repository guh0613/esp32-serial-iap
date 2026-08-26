"""Tkinter front end for the ESP32-S3 Serial IAP host tool.

Serial work runs on a worker thread; the Tk main loop only renders. The two
sides talk through a queue that the main loop drains with ``after()``, because
Tk widgets may only be touched from the thread that created them.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from host.iap_tool import (
    DEFAULT_BAUD_RATE,
    _import_pyserial,
    open_serial,
    validate_peer,
)
from host.transport import IapClient, IapTransportError, SerialTransport


BAUD_CHOICES = ("115200", "230400", "460800", "921600")
MAX_VERSION_BYTES = 32
POLL_INTERVAL_MS = 50
MAX_EVENTS_PER_POLL = 256

# Ports that never carry a board; hiding them keeps the picker usable.
IGNORED_PORT_HINTS = ("Bluetooth-Incoming-Port", "debug-console")
# Substrings that suggest a USB-to-UART bridge rather than a built-in port.
LIKELY_BRIDGE_HINTS = ("usbserial", "usbmodem", "wchusbserial", "UART", "COM")

Emit = Callable[[tuple[Any, ...]], None]
Job = Callable[[IapClient, Emit], None]


class _Cancelled(Exception):
    """Raised from the progress callback to unwind an in-flight transfer."""


def ensure_tcl_library_path() -> None:
    """Point Tcl at the base interpreter's library when running inside a venv.

    Tcl locates ``init.tcl`` relative to ``sys.prefix``. A virtualenv built
    from a relocatable CPython (uv, python-build-standalone) therefore hides
    the real ``lib/tcl8.6``, and ``Tk()`` dies with "Can't find a usable
    init.tcl" even though ``import tkinter`` succeeded. Call this before
    creating the first Tk instance; it is a no-op outside a venv and when the
    variables are already set.
    """

    if sys.prefix == sys.base_prefix:
        return
    base_lib = Path(sys.base_prefix) / "lib"
    for variable, pattern in (("TCL_LIBRARY", "tcl8.*"), ("TK_LIBRARY", "tk8.*")):
        if os.environ.get(variable):
            continue
        candidates = sorted(base_lib.glob(pattern))
        if candidates:
            os.environ[variable] = str(candidates[-1])


def discover_ports() -> list[tuple[str, str]]:
    """Return ``(device, label)`` pairs for every plausible serial port."""

    _, list_ports = _import_pyserial()
    found: list[tuple[str, str]] = []
    for port in sorted(list_ports.comports(), key=lambda item: item.device):
        if any(hint in port.device for hint in IGNORED_PORT_HINTS):
            continue
        description = port.description or "no description"
        found.append((port.device, f"{port.device} — {description}"))
    return found


def preferred_port(ports: list[tuple[str, str]]) -> str | None:
    """Pick the port most likely to be the board, or None when unsure."""

    for device, label in ports:
        if any(hint in label for hint in LIKELY_BRIDGE_HINTS):
            return device
    return ports[0][0] if ports else None


class IapGui:
    """Window state plus the worker-thread plumbing behind it."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._ports: dict[str, str] = {}

        root.title("ESP32-S3 Serial IAP")
        root.minsize(700, 540)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        self._build_connection_frame()
        self._build_image_frame()
        self._build_action_frame()
        self._build_log_frame()

        self.refresh_ports()
        self._log("就绪。执行任何命令前请先关闭 idf.py monitor，串口不能被两个进程同时占用。")
        root.after(POLL_INTERVAL_MS, self._drain_events)

    # ---------------------------------------------------------------- layout

    def _build_connection_frame(self) -> None:
        frame = ttk.LabelFrame(self._root, text="连接", padding=8)
        frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="串口").grid(row=0, column=0, sticky="w")
        self._port_var = tk.StringVar()
        self._port_box = ttk.Combobox(frame, textvariable=self._port_var)
        self._port_box.grid(row=0, column=1, sticky="ew", padx=6)
        self._refresh_button = ttk.Button(
            frame, text="刷新", width=8, command=self.refresh_ports
        )
        self._refresh_button.grid(row=0, column=2)

        options = ttk.Frame(frame)
        options.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        ttk.Label(options, text="波特率").pack(side="left")
        self._baud_var = tk.StringVar(value=str(DEFAULT_BAUD_RATE))
        ttk.Combobox(
            options,
            textvariable=self._baud_var,
            values=BAUD_CHOICES,
            width=10,
        ).pack(side="left", padx=(6, 16))

        ttk.Label(options, text="重试次数").pack(side="left")
        self._attempts_var = tk.StringVar(value="3")
        ttk.Spinbox(
            options, from_=1, to=10, textvariable=self._attempts_var, width=5
        ).pack(side="left", padx=(6, 16))

        self._reset_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options,
            text="连接前复位设备（进入 Bootloader）",
            variable=self._reset_var,
        ).pack(side="left")

    def _build_image_frame(self) -> None:
        frame = ttk.LabelFrame(self._root, text="固件", padding=8)
        frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="镜像文件").grid(row=0, column=0, sticky="w")
        self._image_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self._image_var).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        self._browse_button = ttk.Button(
            frame, text="浏览…", width=8, command=self._choose_image
        )
        self._browse_button.grid(row=0, column=2)

        ttk.Label(frame, text="版本号").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._version_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self._version_var).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(8, 0)
        )
        ttk.Label(frame, text=f"留空用文件名，上限 {MAX_VERSION_BYTES} 字节").grid(
            row=1, column=2, sticky="w", pady=(8, 0)
        )

    def _build_action_frame(self) -> None:
        frame = ttk.Frame(self._root, padding=(8, 4))
        frame.grid(row=2, column=0, sticky="ew")
        frame.columnconfigure(4, weight=1)

        self._info_button = ttk.Button(
            frame, text="设备信息", command=self._on_info
        )
        self._info_button.grid(row=0, column=0)
        self._flash_button = ttk.Button(
            frame, text="烧写固件", command=self._on_flash
        )
        self._flash_button.grid(row=0, column=1, padx=6)
        self._boot_button = ttk.Button(
            frame, text="启动应用", command=self._on_boot
        )
        self._boot_button.grid(row=0, column=2)
        self._cancel_button = ttk.Button(
            frame, text="取消", command=self._on_cancel, state="disabled"
        )
        self._cancel_button.grid(row=0, column=3, padx=6)

        self._progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self._progress.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self._status_var = tk.StringVar(value="空闲")
        ttk.Label(frame, textvariable=self._status_var).grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(4, 0)
        )

    def _build_log_frame(self) -> None:
        frame = ttk.LabelFrame(self._root, text="日志", padding=8)
        frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=(4, 8))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self._log_text = tk.Text(frame, height=12, wrap="word", state="disabled")
        self._log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self._log_text.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._log_text.configure(yscrollcommand=scrollbar.set)

    # ------------------------------------------------------------ ui helpers

    def _log(self, message: str) -> None:
        self._log_text.configure(state="normal")
        self._log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _set_busy(self, busy: bool, *, cancellable: bool = False) -> None:
        state = "disabled" if busy else "normal"
        for widget in (
            self._info_button,
            self._flash_button,
            self._boot_button,
            self._refresh_button,
            self._browse_button,
            self._port_box,
        ):
            widget.configure(state=state)
        self._cancel_button.configure(
            state="normal" if busy and cancellable else "disabled"
        )

    def refresh_ports(self) -> None:
        try:
            ports = discover_ports()
        except RuntimeError as error:
            messagebox.showerror("无法枚举串口", str(error))
            return

        self._ports = {label: device for device, label in ports}
        self._port_box.configure(values=list(self._ports))
        current = self._port_var.get()
        if current in self._ports or current in self._ports.values():
            self._log(f"发现 {len(ports)} 个串口，保留当前选择。")
            return

        chosen = preferred_port(ports)
        for label, device in self._ports.items():
            if device == chosen:
                self._port_var.set(label)
                break
        else:
            self._port_var.set("")
        self._log(
            f"发现 {len(ports)} 个串口"
            + (f"，已选中 {chosen}。" if chosen else "；未找到可用串口。")
        )

    def _choose_image(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 ESP-IDF 应用镜像",
            filetypes=[("应用镜像", "*.bin"), ("所有文件", "*.*")],
        )
        if selected:
            self._image_var.set(selected)

    def _selected_port(self) -> str:
        raw = self._port_var.get().strip()
        return self._ports.get(raw, raw)

    # ------------------------------------------------------------- job start

    def _read_connection(self) -> tuple[str, int, int, bool] | None:
        """Validate the connection widgets, reporting the first problem."""

        port = self._selected_port()
        if not port:
            messagebox.showwarning("缺少串口", "请先选择或输入一个串口。")
            return None
        try:
            baud = int(self._baud_var.get())
            attempts = int(self._attempts_var.get())
        except ValueError:
            messagebox.showwarning("参数无效", "波特率和重试次数必须是整数。")
            return None
        if baud <= 0 or attempts < 1:
            messagebox.showwarning(
                "参数无效", "波特率必须为正数，重试次数至少为 1。"
            )
            return None
        return port, baud, attempts, self._reset_var.get()

    def _start(self, title: str, job: Job, *, cancellable: bool = False) -> None:
        connection = self._read_connection()
        if connection is None:
            return
        port, baud, attempts, reset_device = connection

        self._cancel.clear()
        self._set_busy(True, cancellable=cancellable)
        self._status_var.set(f"{title}中…")
        self._progress.configure(value=0)
        self._worker = threading.Thread(
            target=self._run_job,
            args=(title, job, port, baud, attempts, reset_device),
            daemon=True,
        )
        self._worker.start()

    def _on_info(self) -> None:
        def job(client: IapClient, emit: Emit) -> None:
            info = client.info()
            emit(("log", info.detail or "设备未返回描述信息。"))

        self._start("查询", job)

    def _on_boot(self) -> None:
        def job(client: IapClient, emit: Emit) -> None:
            client.boot()
            emit(("log", "BOOT 已确认，设备正在重启。"))

        self._start("启动", job)

    def _on_flash(self) -> None:
        raw_path = self._image_var.get().strip()
        if not raw_path:
            messagebox.showwarning("缺少镜像", "请先选择要烧写的 .bin 文件。")
            return
        image = Path(raw_path).expanduser()
        if not image.is_file():
            messagebox.showwarning("镜像无效", f"文件不存在：{image}")
            return
        version = self._version_var.get().strip() or image.stem
        if len(version.encode("utf-8")) > MAX_VERSION_BYTES:
            messagebox.showwarning(
                "版本号过长", f"版本号超过 {MAX_VERSION_BYTES} 个 UTF-8 字节。"
            )
            return

        def job(client: IapClient, emit: Emit) -> None:
            info = client.info()
            emit(("log", f"设备：{info.detail or '无描述'}"))
            emit(("log", f"镜像：{image}（{image.stat().st_size} 字节），版本 {version}"))
            client.flash_file(
                image,
                image_version=version,
                progress=self._make_progress(emit),
            )
            emit(("log", "镜像已通过校验，设备正在重启进入新分区。"))

        self._start("烧写", job, cancellable=True)

    def _on_cancel(self) -> None:
        self._cancel.set()
        self._status_var.set("正在取消…")
        self._cancel_button.configure(state="disabled")

    # ----------------------------------------------------------- worker side

    def _make_progress(self, emit: Emit) -> Callable[[int, int], None]:
        def progress(sent: int, total: int) -> None:
            if self._cancel.is_set():
                raise _Cancelled
            emit(("progress", sent, total))

        return progress

    def _run_job(
        self,
        title: str,
        job: Job,
        port: str,
        baud: int,
        attempts: int,
        reset_device: bool,
    ) -> None:
        """Run one command end to end. Never touches a widget."""

        emit: Emit = self._events.put
        stream: Any = None
        client: IapClient | None = None
        try:
            emit(("log", f"打开 {port}，{baud} bps，最多 {attempts} 次重传。"))
            stream = open_serial(port, baud, reset_device=reset_device)
            client = IapClient(SerialTransport(stream, max_attempts=attempts))
            hello = client.hello()
            validate_peer(hello.detail, expect_application=not reset_device)
            emit(("log", f"已连接：{hello.detail or 'Serial IAP v1'}"))
            job(client, emit)
            emit(("done", True, f"{title}完成"))
        except _Cancelled:
            emit(("log", "已请求取消，正在发送 ABORT…"))
            emit(("done", False, f"{title}已取消" + self._abort(client)))
        except (IapTransportError, OSError, RuntimeError, ValueError) as error:
            emit(("done", False, f"{title}失败：{error}"))
        finally:
            if stream is not None:
                stream.close()

    def _abort(self, client: IapClient | None) -> str:
        """Tell the device to drop the partial image; otadata stays untouched."""

        if client is None:
            return ""
        try:
            client.abort()
        except (IapTransportError, OSError) as error:
            return f"（ABORT 未确认：{error}，设备将在超时后启动旧应用）"
        return "（已确认 ABORT，设备仍运行旧应用）"

    # ------------------------------------------------------------- main loop

    def _drain_events(self) -> None:
        latest_progress: tuple[int, int] | None = None
        for _ in range(MAX_EVENTS_PER_POLL):
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "log":
                self._log(event[1])
            elif event[0] == "progress":
                latest_progress = (event[1], event[2])
            elif event[0] == "done":
                self._finish(succeeded=event[1], message=event[2])
        if latest_progress is not None:
            self._show_progress(*latest_progress)
        self._root.after(POLL_INTERVAL_MS, self._drain_events)

    def _show_progress(self, sent: int, total: int) -> None:
        percentage = 100.0 if total == 0 else sent * 100.0 / total
        self._progress.configure(value=percentage)
        self._status_var.set(f"已传输 {sent}/{total} 字节（{percentage:.2f}%）")

    def _finish(self, *, succeeded: bool, message: str) -> None:
        self._worker = None
        self._set_busy(False)
        self._status_var.set(message)
        self._log(message)
        if not succeeded:
            messagebox.showerror("Serial IAP", message)


def main() -> None:
    ensure_tcl_library_path()
    root = tk.Tk()
    IapGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
