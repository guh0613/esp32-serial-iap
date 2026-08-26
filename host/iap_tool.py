"""Command-line host utility for the ESP32-S3 Serial IAP project."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Any

from host.transport import IapClient, IapTransportError, SerialTransport


DEFAULT_BAUD_RATE = 115200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ESP32-S3 Serial IAP v1 host tool"
    )
    parser.add_argument("--port", help="serial port, for example COM4")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD_RATE)
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="maximum identical transmissions per request (default: 3)",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="do not pulse EN; connect to an already running application",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ports", help="list available serial ports")
    commands.add_parser("info", help="query the connected IAP implementation")
    commands.add_parser("boot", help="leave IAP and reboot the current app")

    flash = commands.add_parser("flash", help="transfer an ESP-IDF app .bin")
    flash.add_argument("image", type=Path)
    flash.add_argument(
        "--version",
        help="display-only image version (default: file stem, max 32 UTF-8 bytes)",
    )
    return parser


def _import_pyserial() -> tuple[Any, Any]:
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as error:
        raise RuntimeError(
            "pyserial is required; activate the ESP-IDF v5.5.5 Python "
            "environment or install host/requirements.txt"
        ) from error
    return serial, list_ports


def list_serial_ports() -> int:
    _, list_ports = _import_pyserial()
    ports = sorted(list_ports.comports(), key=lambda item: item.device)
    if not ports:
        print("No serial ports found.")
        return 0
    for port in ports:
        description = port.description or "no description"
        hardware_id = port.hwid or "no hardware id"
        print(f"{port.device}: {description} [{hardware_id}]")
    return 0


def normal_reset(stream: Any, sleep: Any = time.sleep) -> None:
    """Pulse EN through RTS while DTR keeps GPIO0 high for SPI boot."""

    stream.dtr = False
    stream.rts = True
    sleep(0.1)
    stream.rts = False
    sleep(0.05)


def open_serial(port: str, baud_rate: int, *, reset_device: bool) -> Any:
    serial, _ = _import_pyserial()
    stream = serial.Serial(port=None, baudrate=baud_rate,
                           timeout=0.05, write_timeout=1.0)
    stream.dtr = False
    stream.rts = False
    stream.port = port
    stream.open()
    stream.reset_input_buffer()
    if reset_device:
        normal_reset(stream)
    return stream


def require_port(parser: argparse.ArgumentParser, port: str | None) -> str:
    if not port:
        parser.error("--port is required for this command; run 'ports' first")
    return port


def validate_peer(detail: str, *, expect_application: bool) -> None:
    parts = detail.split(";")
    implementation = parts[0] if parts else ""
    fields: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value

    expected_implementation = (
        "serial-iap-app" if expect_application else "serial-iap-bootloader"
    )
    if implementation != expected_implementation:
        raise IapTransportError(
            f"expected {expected_implementation}, connected to "
            f"{implementation or '<unknown>'}"
        )
    if fields.get("protocol") != "1":
        raise IapTransportError(
            f"peer reports unsupported protocol {fields.get('protocol', '<missing>')}"
        )
    if fields.get("chip") != "esp32s3":
        raise IapTransportError(
            f"peer reports unexpected chip {fields.get('chip', '<missing>')}"
        )


def print_progress(sent: int, total: int) -> None:
    percentage = 100.0 if total == 0 else sent * 100.0 / total
    print(
        f"\rTransferred {sent}/{total} bytes ({percentage:6.2f}%)",
        end="" if sent < total else "\n",
        flush=True,
    )


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "ports":
        return list_serial_ports()

    port = require_port(parser, arguments.port)
    stream = open_serial(
        port,
        arguments.baud,
        reset_device=not arguments.no_reset,
    )
    try:
        client = IapClient(
            SerialTransport(stream, max_attempts=arguments.attempts)
        )
        hello = client.hello()
        validate_peer(hello.detail, expect_application=arguments.no_reset)
        print(f"Connected: {hello.detail or 'Serial IAP v1'}")

        if arguments.command == "info":
            info = client.info()
            print(info.detail or "Device returned no descriptive information.")
        elif arguments.command == "boot":
            client.boot()
            print("Boot command acknowledged; device is restarting.")
        elif arguments.command == "flash":
            image: Path = arguments.image
            if not image.is_file():
                parser.error(f"image does not exist or is not a file: {image}")
            version = arguments.version or image.stem
            if len(version.encode("utf-8")) > 32:
                parser.error("--version exceeds 32 UTF-8 bytes")
            info = client.info()
            print(f"Device: {info.detail or 'no details'}")
            print(f"Image: {image} ({image.stat().st_size} bytes)")
            client.flash_file(
                image,
                image_version=version,
                progress=print_progress,
            )
            print("Image accepted; device is restarting into the new slot.")
        return 0
    finally:
        stream.close()


def main() -> None:
    try:
        raise SystemExit(run())
    except (IapTransportError, OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
