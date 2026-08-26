"""Stop-and-wait transport and high-level client for Serial IAP v1."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
from pathlib import Path
import time
from typing import BinaryIO, Protocol

from host.protocol import (
    BeginRequest,
    Command,
    DataRequest,
    Frame,
    MAX_PAYLOAD_LENGTH,
    PROTOCOL_VERSION,
    ProtocolError,
    Response,
    Status,
    StreamDecoder,
    decode_response,
    encode_begin_request,
    encode_data_request,
    encode_frame,
    response_command,
)


DEFAULT_RESPONSE_TIMEOUT_S = 1.0
BEGIN_RESPONSE_TIMEOUT_S = 120.0
END_RESPONSE_TIMEOUT_S = 120.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_DATA_LENGTH = MAX_PAYLOAD_LENGTH - 4
READ_SIZE = 256


class SerialStream(Protocol):
    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...


class IapTransportError(RuntimeError):
    """Base error for host transport or device response failures."""


class ResponseTimeoutError(IapTransportError):
    pass


class DeviceResponseError(IapTransportError):
    def __init__(self, response: Response) -> None:
        self.response = response
        try:
            status_name = Status(response.status).name
        except ValueError:
            status_name = f"UNKNOWN_0x{response.status:04X}"
        detail = f": {response.detail}" if response.detail else ""
        super().__init__(f"device returned {status_name}{detail}")


class SerialTransport:
    """Send one request at a time and retry the identical encoded frame."""

    def __init__(
        self,
        stream: SerialStream,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._stream = stream
        self._max_attempts = max_attempts
        self._decoder = StreamDecoder()

    def pop_decoder_issues(self) -> list[str]:
        return self._decoder.pop_issues()

    def request(
        self,
        frame: Frame,
        *,
        timeout_s: float = DEFAULT_RESPONSE_TIMEOUT_S,
    ) -> Response:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be greater than zero")

        encoded = encode_frame(frame)
        expected_command = response_command(frame.command)

        for _attempt in range(1, self._max_attempts + 1):
            written = self._stream.write(encoded)
            if written != len(encoded):
                raise IapTransportError(
                    f"serial write accepted {written} of {len(encoded)} bytes"
                )
            self._stream.flush()

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                chunk = self._stream.read(READ_SIZE)
                if not chunk:
                    time.sleep(0.001)
                    continue
                for response_frame in self._decoder.feed(chunk):
                    if (
                        response_frame.version != PROTOCOL_VERSION
                        or response_frame.command != expected_command
                        or response_frame.sequence != frame.sequence
                    ):
                        continue
                    try:
                        return decode_response(response_frame.payload)
                    except ProtocolError as error:
                        raise IapTransportError(
                            f"malformed response payload: {error}"
                        ) from error

        raise ResponseTimeoutError(
            f"no response for command 0x{frame.command:02X}, "
            f"sequence {frame.sequence}, after {self._max_attempts} attempts"
        )


ProgressCallback = Callable[[int, int], None]


class IapClient:
    """Stateful Serial IAP v1 client built on a stop-and-wait transport."""

    def __init__(self, transport: SerialTransport) -> None:
        self._transport = transport
        self._next_sequence = 0
        self._next_offset = 0

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    @property
    def next_offset(self) -> int:
        return self._next_offset

    def hello(self) -> Response:
        self._next_sequence = 0
        self._next_offset = 0
        return self._successful_request(Command.HELLO, b"")

    def info(self) -> Response:
        return self._successful_request(Command.INFO, b"")

    def boot(self) -> Response:
        response = self._successful_request(Command.BOOT, b"")
        self._next_offset = 0
        return response

    def abort(self) -> Response:
        response = self._successful_request(Command.ABORT, b"")
        self._next_offset = 0
        return response

    def flash_file(
        self,
        image_path: Path,
        *,
        image_version: str,
        progress: ProgressCallback | None = None,
    ) -> None:
        image_path = Path(image_path)
        image_size = image_path.stat().st_size
        digest = hashlib.sha256()
        with image_path.open("rb") as image:
            while chunk := image.read(64 * 1024):
                digest.update(chunk)

        with image_path.open("rb") as image:
            self.flash_stream(
                image,
                image_size=image_size,
                image_sha256=digest.digest(),
                image_version=image_version,
                progress=progress,
            )

    def flash_stream(
        self,
        image: BinaryIO,
        *,
        image_size: int,
        image_sha256: bytes,
        image_version: str,
        progress: ProgressCallback | None = None,
    ) -> None:
        begin_payload = encode_begin_request(
            BeginRequest(image_size, image_sha256, image_version)
        )
        begin_response = self._successful_request(
            Command.BEGIN,
            begin_payload,
            timeout_s=BEGIN_RESPONSE_TIMEOUT_S,
        )
        if begin_response.next_offset != 0:
            raise IapTransportError(
                f"BEGIN returned unexpected offset {begin_response.next_offset}"
            )
        self._next_offset = 0
        if progress is not None:
            progress(0, image_size)

        while self._next_offset < image_size:
            remaining = image_size - self._next_offset
            data = image.read(min(DEFAULT_DATA_LENGTH, remaining))
            if not data:
                raise IapTransportError(
                    f"image stream ended at {self._next_offset} of {image_size} bytes"
                )
            request_offset = self._next_offset
            payload = encode_data_request(DataRequest(request_offset, data))
            response = self._successful_request(Command.DATA, payload)
            expected_offset = request_offset + len(data)
            if response.next_offset != expected_offset:
                raise IapTransportError(
                    f"DATA at {request_offset} returned next offset "
                    f"{response.next_offset}, expected {expected_offset}"
                )
            self._next_offset = response.next_offset
            if progress is not None:
                progress(self._next_offset, image_size)

        response = self._successful_request(
            Command.END,
            b"",
            timeout_s=END_RESPONSE_TIMEOUT_S,
        )
        if response.next_offset != image_size:
            raise IapTransportError(
                f"END returned offset {response.next_offset}, expected {image_size}"
            )

    def _successful_request(
        self,
        command: int,
        payload: bytes,
        *,
        timeout_s: float = DEFAULT_RESPONSE_TIMEOUT_S,
    ) -> Response:
        sequence = self._next_sequence
        response = self._transport.request(
            Frame(command=command, sequence=sequence, payload=payload),
            timeout_s=timeout_s,
        )
        if response.status != Status.OK:
            raise DeviceResponseError(response)

        expected_sequence = (sequence + 1) & 0xFFFF
        if response.expected_sequence != expected_sequence:
            raise IapTransportError(
                f"response expects sequence {response.expected_sequence}, "
                f"host expected {expected_sequence}"
            )
        self._next_sequence = response.expected_sequence
        self._next_offset = response.next_offset
        return response
