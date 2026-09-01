from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct
import zlib


SOF = b"\xA5\x5A"
PROTOCOL_VERSION = 1
MAX_PAYLOAD_LENGTH = 1024
MAX_VERSION_TEXT_LENGTH = 32
MAX_DETAIL_LENGTH = 96

_HEADER_CRC_INPUT = struct.Struct("<BBHH")
_HEADER = struct.Struct("<2sBBHHH")
_TRAILER = struct.Struct("<I")
_BEGIN_PREFIX = struct.Struct("<I32sB")
_DATA_OFFSET = struct.Struct("<I")
_RESPONSE_PREFIX = struct.Struct("<HHIB")

MIN_FRAME_LENGTH = _HEADER.size + _TRAILER.size


class Command(IntEnum):
    HELLO = 0x01
    INFO = 0x02
    BEGIN = 0x10
    DATA = 0x11
    END = 0x12
    ABORT = 0x13
    BOOT = 0x20


class Status(IntEnum):
    OK = 0x0000
    ERR_VERSION = 0x0001
    ERR_COMMAND = 0x0002
    ERR_SEQUENCE = 0x0003
    ERR_LENGTH = 0x0004
    ERR_CRC = 0x0005
    ERR_STATE = 0x0006
    ERR_OFFSET = 0x0007
    ERR_FLASH = 0x0008
    ERR_IMAGE = 0x0009
    ERR_HASH = 0x000A
    ERR_TIMEOUT = 0x000B
    ERR_BUSY = 0x000C
    ERR_INTERNAL = 0x000D


class ProtocolError(ValueError):
    """Raised when bytes or a command payload violate protocol v1."""


@dataclass(frozen=True, slots=True)
class Frame:
    command: int
    sequence: int
    payload: bytes = b""
    version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class BeginRequest:
    image_size: int
    image_sha256: bytes
    image_version: str


@dataclass(frozen=True, slots=True)
class DataRequest:
    offset: int
    data: bytes


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    expected_sequence: int
    next_offset: int
    detail: str = ""


def crc16_ccitt_false(data: bytes) -> int:
    """Return CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF)."""

    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def payload_crc32(payload: bytes) -> int:
    """Return the unsigned IEEE CRC-32 used by a frame payload."""

    return zlib.crc32(payload) & 0xFFFFFFFF


def response_command(request_command: int) -> int:
    _require_uint("request_command", request_command, 8)
    if request_command & 0x80:
        raise ProtocolError("request command already has the response bit set")
    return request_command | 0x80


def encode_frame(frame: Frame) -> bytes:
    payload = bytes(frame.payload)
    _require_uint("version", frame.version, 8)
    _require_uint("command", frame.command, 8)
    _require_uint("sequence", frame.sequence, 16)
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise ProtocolError(
            f"payload length {len(payload)} exceeds {MAX_PAYLOAD_LENGTH}"
        )

    crc_input = _HEADER_CRC_INPUT.pack(
        frame.version, frame.command, frame.sequence, len(payload)
    )
    header_crc = crc16_ccitt_false(crc_input)
    header = _HEADER.pack(
        SOF,
        frame.version,
        frame.command,
        frame.sequence,
        len(payload),
        header_crc,
    )
    return header + payload + _TRAILER.pack(payload_crc32(payload))


def decode_frame(raw: bytes) -> Frame:
    raw = bytes(raw)
    if len(raw) < MIN_FRAME_LENGTH:
        raise ProtocolError("frame is truncated before the fixed fields")

    sof, version, command, sequence, payload_length, header_crc = _HEADER.unpack_from(raw)
    if sof != SOF:
        raise ProtocolError("invalid SOF")
    if payload_length > MAX_PAYLOAD_LENGTH:
        raise ProtocolError(
            f"payload length {payload_length} exceeds {MAX_PAYLOAD_LENGTH}"
        )

    crc_input = _HEADER_CRC_INPUT.pack(
        version, command, sequence, payload_length
    )
    actual_header_crc = crc16_ccitt_false(crc_input)
    if header_crc != actual_header_crc:
        raise ProtocolError(
            f"header CRC mismatch: got 0x{header_crc:04X}, "
            f"expected 0x{actual_header_crc:04X}"
        )

    expected_length = MIN_FRAME_LENGTH + payload_length
    if len(raw) != expected_length:
        raise ProtocolError(
            f"frame length {len(raw)} does not match declared length {expected_length}"
        )

    payload_start = _HEADER.size
    payload_end = payload_start + payload_length
    payload = raw[payload_start:payload_end]
    (received_payload_crc,) = _TRAILER.unpack_from(raw, payload_end)
    actual_payload_crc = payload_crc32(payload)
    if received_payload_crc != actual_payload_crc:
        raise ProtocolError(
            f"payload CRC mismatch: got 0x{received_payload_crc:08X}, "
            f"expected 0x{actual_payload_crc:08X}"
        )

    return Frame(
        command=command,
        sequence=sequence,
        payload=payload,
        version=version,
    )


class StreamDecoder:
    """Incrementally recover complete frames from an arbitrary byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._issues: list[str] = []

    @property
    def buffered_length(self) -> int:
        return len(self._buffer)

    def pop_issues(self) -> list[str]:
        issues = self._issues
        self._issues = []
        return issues

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        frames: list[Frame] = []

        while True:
            sof_index = self._buffer.find(SOF)
            if sof_index < 0:
                keep = 1 if self._buffer.endswith(SOF[:1]) else 0
                discarded = len(self._buffer) - keep
                if discarded:
                    self._issues.append(f"discarded {discarded} byte(s) before SOF")
                    del self._buffer[:discarded]
                break

            if sof_index:
                self._issues.append(f"discarded {sof_index} byte(s) before SOF")
                del self._buffer[:sof_index]

            if len(self._buffer) < _HEADER.size:
                break

            (
                sof,
                version,
                command,
                sequence,
                payload_length,
                header_crc,
            ) = _HEADER.unpack_from(self._buffer)
            assert sof == SOF

            crc_input = _HEADER_CRC_INPUT.pack(
                version, command, sequence, payload_length
            )
            actual_header_crc = crc16_ccitt_false(crc_input)
            if payload_length > MAX_PAYLOAD_LENGTH or header_crc != actual_header_crc:
                self._issues.append("discarded candidate with invalid header")
                del self._buffer[0]
                continue

            frame_length = MIN_FRAME_LENGTH + payload_length
            if len(self._buffer) < frame_length:
                break

            raw_frame = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            try:
                frames.append(decode_frame(raw_frame))
            except ProtocolError as error:
                self._issues.append(str(error))

        return frames


def encode_begin_request(request: BeginRequest) -> bytes:
    _require_uint("image_size", request.image_size, 32)
    if request.image_size == 0:
        raise ProtocolError("image_size must be greater than zero")
    digest = bytes(request.image_sha256)
    if len(digest) != 32:
        raise ProtocolError("image_sha256 must contain exactly 32 bytes")
    version = request.image_version.encode("utf-8")
    if len(version) > MAX_VERSION_TEXT_LENGTH:
        raise ProtocolError(
            f"UTF-8 image version exceeds {MAX_VERSION_TEXT_LENGTH} bytes"
        )
    return _BEGIN_PREFIX.pack(request.image_size, digest, len(version)) + version


def decode_begin_request(payload: bytes) -> BeginRequest:
    if len(payload) < _BEGIN_PREFIX.size:
        raise ProtocolError("BEGIN payload is truncated")
    image_size, digest, version_length = _BEGIN_PREFIX.unpack_from(payload)
    if image_size == 0:
        raise ProtocolError("image_size must be greater than zero")
    if version_length > MAX_VERSION_TEXT_LENGTH:
        raise ProtocolError("BEGIN version text is too long")
    if len(payload) != _BEGIN_PREFIX.size + version_length:
        raise ProtocolError("BEGIN payload length does not match version_length")
    try:
        version = payload[_BEGIN_PREFIX.size :].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("BEGIN version is not valid UTF-8") from error
    return BeginRequest(image_size, digest, version)


def encode_data_request(request: DataRequest) -> bytes:
    _require_uint("offset", request.offset, 32)
    data = bytes(request.data)
    if not data:
        raise ProtocolError("DATA must contain at least one data byte")
    maximum_data = MAX_PAYLOAD_LENGTH - _DATA_OFFSET.size
    if len(data) > maximum_data:
        raise ProtocolError(f"DATA length exceeds {maximum_data} bytes")
    return _DATA_OFFSET.pack(request.offset) + data


def decode_data_request(payload: bytes) -> DataRequest:
    if len(payload) <= _DATA_OFFSET.size:
        raise ProtocolError("DATA payload is missing image bytes")
    (offset,) = _DATA_OFFSET.unpack_from(payload)
    return DataRequest(offset, payload[_DATA_OFFSET.size :])


def encode_response(response: Response) -> bytes:
    _require_uint("status", response.status, 16)
    _require_uint("expected_sequence", response.expected_sequence, 16)
    _require_uint("next_offset", response.next_offset, 32)
    detail = response.detail.encode("utf-8")
    if len(detail) > MAX_DETAIL_LENGTH:
        raise ProtocolError(f"response detail exceeds {MAX_DETAIL_LENGTH} bytes")
    return _RESPONSE_PREFIX.pack(
        response.status,
        response.expected_sequence,
        response.next_offset,
        len(detail),
    ) + detail


def decode_response(payload: bytes) -> Response:
    if len(payload) < _RESPONSE_PREFIX.size:
        raise ProtocolError("response payload is truncated")
    status, expected_sequence, next_offset, detail_length = (
        _RESPONSE_PREFIX.unpack_from(payload)
    )
    if detail_length > MAX_DETAIL_LENGTH:
        raise ProtocolError("response detail is too long")
    if len(payload) != _RESPONSE_PREFIX.size + detail_length:
        raise ProtocolError("response payload length does not match detail_length")
    try:
        detail = payload[_RESPONSE_PREFIX.size :].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("response detail is not valid UTF-8") from error
    return Response(status, expected_sequence, next_offset, detail)


def _require_uint(name: str, value: int, bits: int) -> None:
    if not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise ProtocolError(f"{name} must be an unsigned {bits}-bit integer")
