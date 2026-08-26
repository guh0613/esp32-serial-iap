"""Explicit, destructive-on-inactive-slot hardware validation scenarios.

This module is intentionally separate from the normal host CLI.  Every
scenario verifies that it is talking to the custom bootloader and requires a
confirmation flag before erasing any part of the inactive OTA slot.
"""

from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import sys
import time

from host.iap_tool import normal_reset, open_serial
from host.protocol import (
    BeginRequest,
    Command,
    DataRequest,
    Frame,
    Response,
    Status,
    encode_begin_request,
    encode_data_request,
)
from host.transport import (
    BEGIN_RESPONSE_TIMEOUT_S,
    DEFAULT_DATA_LENGTH,
    DeviceResponseError,
    IapClient,
    IapTransportError,
    SerialTransport,
)


PROTOCOL_TEST_IMAGE_SIZE = 4096
PROTOCOL_TEST_DATA_SIZE = 1020
RESET_TEST_TRANSFER_SIZE = 8 * DEFAULT_DATA_LENGTH
APP_START_TIMEOUT_S = 3.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Destructive-on-inactive-slot Serial IAP hardware tests"
    )
    parser.add_argument("--port", required=True, help="serial port, for example COM4")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--confirm-inactive-write",
        action="store_true",
        help="confirm that the inactive OTA slot may be erased and written",
    )
    commands = parser.add_subparsers(dest="scenario", required=True)
    commands.add_parser(
        "protocol-errors",
        help="test bad offset, incomplete END, duplicate DATA, and ABORT",
    )
    bad_hash = commands.add_parser(
        "bad-hash",
        help="transfer a complete image with an intentionally wrong SHA-256",
    )
    bad_hash.add_argument("image", type=Path)
    interrupted = commands.add_parser(
        "interrupted-transfer",
        help="reset after a partial transfer and verify the old app still boots",
    )
    interrupted.add_argument("image", type=Path)
    return parser


def require_response(
    response: Response,
    expected_status: Status,
    *,
    expected_sequence: int,
    expected_offset: int,
    label: str,
) -> None:
    actual_status = Status(response.status)
    if (
        actual_status != expected_status
        or response.expected_sequence != expected_sequence
        or response.next_offset != expected_offset
    ):
        raise IapTransportError(
            f"{label}: got status={actual_status.name}, "
            f"expected_sequence={response.expected_sequence}, "
            f"next_offset={response.next_offset}"
        )
    print(
        f"PASS {label}: {actual_status.name}, "
        f"sequence={response.expected_sequence}, offset={response.next_offset}"
    )


def connect_bootloader(port: str, baud_rate: int) -> tuple[object, SerialTransport, int]:
    stream = open_serial(port, baud_rate, reset_device=True)
    transport = SerialTransport(stream)
    client = IapClient(transport)
    hello = client.hello()
    if "serial-iap-bootloader" not in hello.detail:
        stream.close()
        raise IapTransportError(
            f"expected custom bootloader, received: {hello.detail or '<empty>'}"
        )
    info = client.info()
    if "inactive=ota_" not in info.detail:
        stream.close()
        raise IapTransportError(
            f"bootloader did not report a writable inactive slot: {info.detail}"
        )
    print(f"Connected: {hello.detail}")
    print(f"Target check: {info.detail}")
    return stream, transport, client.next_sequence


def request(
    transport: SerialTransport,
    command: Command,
    sequence: int,
    payload: bytes = b"",
    *,
    timeout_s: float = 1.0,
) -> Response:
    return transport.request(
        Frame(command=command, sequence=sequence, payload=payload),
        timeout_s=timeout_s,
    )


def verify_application_running(port: str, baud_rate: int) -> None:
    time.sleep(APP_START_TIMEOUT_S)
    stream = open_serial(port, baud_rate, reset_device=False)
    try:
        client = IapClient(SerialTransport(stream))
        hello = client.hello()
        if "serial-iap-app" not in hello.detail:
            raise IapTransportError(
                f"expected existing application after rejected update: {hello.detail}"
            )
        print(f"PASS existing application still runs: {hello.detail}")
    finally:
        stream.close()


def run_protocol_errors(port: str, baud_rate: int) -> None:
    stream, transport, sequence = connect_bootloader(port, baud_rate)
    test_data = bytes((index & 0xFF) for index in range(PROTOCOL_TEST_DATA_SIZE))
    digest = hashlib.sha256(test_data + bytes(
        PROTOCOL_TEST_IMAGE_SIZE - len(test_data)
    )).digest()
    try:
        begin_payload = encode_begin_request(
            BeginRequest(PROTOCOL_TEST_IMAGE_SIZE, digest, "fault-protocol")
        )
        response = request(
            transport,
            Command.BEGIN,
            sequence,
            begin_payload,
            timeout_s=BEGIN_RESPONSE_TIMEOUT_S,
        )
        sequence = (sequence + 1) & 0xFFFF
        require_response(
            response,
            Status.OK,
            expected_sequence=sequence,
            expected_offset=0,
            label="BEGIN inactive-slot erase",
        )

        bad_offset_payload = encode_data_request(DataRequest(4, test_data))
        response = request(transport, Command.DATA, sequence, bad_offset_payload)
        require_response(
            response,
            Status.ERR_OFFSET,
            expected_sequence=sequence,
            expected_offset=0,
            label="wrong DATA offset rejected",
        )

        valid_payload = encode_data_request(DataRequest(0, test_data))
        valid_frame = Frame(Command.DATA, sequence, valid_payload)
        response = transport.request(valid_frame)
        sequence = (sequence + 1) & 0xFFFF
        require_response(
            response,
            Status.OK,
            expected_sequence=sequence,
            expected_offset=len(test_data),
            label="valid DATA accepted",
        )

        response = transport.request(valid_frame)
        require_response(
            response,
            Status.OK,
            expected_sequence=sequence,
            expected_offset=len(test_data),
            label="identical DATA retry is idempotent",
        )

        response = request(transport, Command.END, sequence)
        require_response(
            response,
            Status.ERR_LENGTH,
            expected_sequence=sequence,
            expected_offset=len(test_data),
            label="incomplete image END rejected",
        )

        response = request(transport, Command.ABORT, sequence)
        sequence = (sequence + 1) & 0xFFFF
        require_response(
            response,
            Status.OK,
            expected_sequence=sequence,
            expected_offset=0,
            label="ABORT abandons update",
        )

        response = request(transport, Command.DATA, sequence, valid_payload)
        require_response(
            response,
            Status.ERR_STATE,
            expected_sequence=sequence,
            expected_offset=0,
            label="DATA after ABORT rejected",
        )

        response = request(transport, Command.BOOT, sequence)
        require_response(
            response,
            Status.OK,
            expected_sequence=(sequence + 1) & 0xFFFF,
            expected_offset=0,
            label="BOOT existing application",
        )
    finally:
        stream.close()
    verify_application_running(port, baud_rate)


def run_bad_hash(port: str, baud_rate: int, image_path: Path) -> None:
    image = image_path.read_bytes()
    if not image:
        raise IapTransportError("test image must not be empty")
    actual_digest = hashlib.sha256(image).digest()
    wrong_digest = bytes([actual_digest[0] ^ 0x01]) + actual_digest[1:]

    stream, transport, _sequence = connect_bootloader(port, baud_rate)
    client = IapClient(transport)
    client.hello()
    try:
        try:
            client.flash_stream(
                io.BytesIO(image),
                image_size=len(image),
                image_sha256=wrong_digest,
                image_version="fault-bad-sha256",
            )
        except DeviceResponseError as error:
            if error.response.status != Status.ERR_HASH:
                raise
            print(
                "PASS wrong whole-image SHA-256 rejected: "
                f"{error.response.detail}"
            )
        else:
            raise IapTransportError("bootloader unexpectedly accepted a wrong SHA-256")

        client.abort()
        print("PASS ABORT acknowledged after hash rejection")
        client.boot()
        print("PASS BOOT acknowledged without committing the rejected image")
    finally:
        stream.close()
    verify_application_running(port, baud_rate)


def run_interrupted_transfer(port: str, baud_rate: int, image_path: Path) -> None:
    image = image_path.read_bytes()
    if len(image) <= RESET_TEST_TRANSFER_SIZE:
        raise IapTransportError(
            f"test image must exceed {RESET_TEST_TRANSFER_SIZE} bytes"
        )

    stream, transport, sequence = connect_bootloader(port, baud_rate)
    offset = 0
    try:
        begin_payload = encode_begin_request(
            BeginRequest(
                len(image),
                hashlib.sha256(image).digest(),
                "fault-reset-mid-transfer",
            )
        )
        response = request(
            transport,
            Command.BEGIN,
            sequence,
            begin_payload,
            timeout_s=BEGIN_RESPONSE_TIMEOUT_S,
        )
        sequence = (sequence + 1) & 0xFFFF
        require_response(
            response,
            Status.OK,
            expected_sequence=sequence,
            expected_offset=0,
            label="BEGIN before reset test",
        )

        while offset < RESET_TEST_TRANSFER_SIZE:
            data = image[offset : offset + DEFAULT_DATA_LENGTH]
            payload = encode_data_request(DataRequest(offset, data))
            response = request(transport, Command.DATA, sequence, payload)
            offset += len(data)
            sequence = (sequence + 1) & 0xFFFF
            require_response(
                response,
                Status.OK,
                expected_sequence=sequence,
                expected_offset=offset,
                label=f"partial DATA through {offset} bytes",
            )

        normal_reset(stream)
        print(
            f"PASS hardware reset issued after {offset}/{len(image)} bytes; "
            "END was not sent"
        )
    finally:
        stream.close()

    verify_application_running(port, baud_rate)

    stream, transport, sequence = connect_bootloader(port, baud_rate)
    try:
        response = request(transport, Command.BOOT, sequence)
        require_response(
            response,
            Status.OK,
            expected_sequence=(sequence + 1) & 0xFFFF,
            expected_offset=0,
            label="BOOT after active-slot check",
        )
    finally:
        stream.close()
    verify_application_running(port, baud_rate)


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.confirm_inactive_write:
        parser.error(
            "--confirm-inactive-write is required because this test erases "
            "and writes the inactive OTA slot"
        )

    if arguments.scenario == "protocol-errors":
        run_protocol_errors(arguments.port, arguments.baud)
    elif arguments.scenario == "bad-hash":
        image: Path = arguments.image
        if not image.is_file():
            parser.error(f"image does not exist or is not a file: {image}")
        run_bad_hash(arguments.port, arguments.baud, image)
    elif arguments.scenario == "interrupted-transfer":
        image = arguments.image
        if not image.is_file():
            parser.error(f"image does not exist or is not a file: {image}")
        run_interrupted_transfer(arguments.port, arguments.baud, image)
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (IapTransportError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
