from collections.abc import Callable
import hashlib
import io
import unittest

from host.protocol import (
    Command,
    Frame,
    Response,
    Status,
    decode_begin_request,
    decode_data_request,
    decode_frame,
    encode_frame,
    encode_response,
    response_command,
)
from host.transport import (
    DeviceResponseError,
    IapClient,
    ResponseTimeoutError,
    SerialTransport,
)


RequestHandler = Callable[[Frame, int], bytes]


class FakeSerial:
    def __init__(self, handler: RequestHandler) -> None:
        self.handler = handler
        self.writes: list[bytes] = []
        self.pending = bytearray()
        self.flush_count = 0

    def write(self, data: bytes) -> int:
        wire = bytes(data)
        self.writes.append(wire)
        request = decode_frame(wire)
        self.pending.extend(self.handler(request, len(self.writes)))
        return len(wire)

    def flush(self) -> None:
        self.flush_count += 1

    def read(self, size: int = 1) -> bytes:
        if not self.pending:
            return b""
        returned = bytes(self.pending[: min(size, 3)])
        del self.pending[: len(returned)]
        return returned


def make_response(request: Frame, response: Response) -> bytes:
    return encode_frame(
        Frame(
            command=response_command(request.command),
            sequence=request.sequence,
            payload=encode_response(response),
        )
    )


class SerialTransportTests(unittest.TestCase):
    def test_response_can_follow_text_noise_and_arrive_fragmented(self) -> None:
        def handler(request: Frame, _write_count: int) -> bytes:
            response = Response(Status.OK, request.sequence + 1, 0, "ready")
            return b"boot log\r\n" + make_response(request, response)

        serial = FakeSerial(handler)
        transport = SerialTransport(serial)
        response = transport.request(Frame(Command.HELLO, 0))
        self.assertEqual(response.detail, "ready")
        self.assertTrue(transport.pop_decoder_issues())

    def test_timeout_retries_identical_frame(self) -> None:
        def handler(request: Frame, write_count: int) -> bytes:
            if write_count == 1:
                return b""
            return make_response(request, Response(Status.OK, 1, 0))

        serial = FakeSerial(handler)
        transport = SerialTransport(serial, max_attempts=2)
        response = transport.request(
            Frame(Command.HELLO, 0), timeout_s=0.002
        )
        self.assertEqual(response.status, Status.OK)
        self.assertEqual(len(serial.writes), 2)
        self.assertEqual(serial.writes[0], serial.writes[1])

    def test_timeout_reports_attempt_count(self) -> None:
        serial = FakeSerial(lambda _request, _count: b"")
        transport = SerialTransport(serial, max_attempts=2)
        with self.assertRaisesRegex(ResponseTimeoutError, "after 2 attempts"):
            transport.request(Frame(Command.HELLO, 0), timeout_s=0.001)


class FakeIapDevice:
    def __init__(self) -> None:
        self.expected_sequence = 0
        self.next_offset = 0
        self.expected_size = 0
        self.expected_digest = b""
        self.received = bytearray()
        self.commands: list[int] = []

    def handle(self, request: Frame, _write_count: int) -> bytes:
        self.commands.append(request.command)
        status = Status.OK
        detail = ""

        if request.command == Command.HELLO and request.sequence == 0:
            self.expected_sequence = 0
            self.next_offset = 0
            detail = "fake-device"
        elif request.sequence != self.expected_sequence:
            status = Status.ERR_SEQUENCE
        elif request.command == Command.INFO:
            detail = "mode=test"
        elif request.command == Command.BEGIN:
            begin = decode_begin_request(request.payload)
            self.expected_size = begin.image_size
            self.expected_digest = begin.image_sha256
            self.next_offset = 0
            self.received.clear()
        elif request.command == Command.DATA:
            data = decode_data_request(request.payload)
            if data.offset != self.next_offset:
                status = Status.ERR_OFFSET
            else:
                self.received.extend(data.data)
                self.next_offset += len(data.data)
        elif request.command == Command.END:
            if (
                len(self.received) != self.expected_size
                or hashlib.sha256(self.received).digest() != self.expected_digest
            ):
                status = Status.ERR_HASH
        else:
            status = Status.ERR_COMMAND

        if status == Status.OK:
            self.expected_sequence = (request.sequence + 1) & 0xFFFF
        response = Response(
            status,
            self.expected_sequence,
            self.next_offset,
            detail,
        )
        return make_response(request, response)


class IapClientTests(unittest.TestCase):
    def test_complete_image_transfer(self) -> None:
        image = bytes(range(256)) * 9
        device = FakeIapDevice()
        client = IapClient(SerialTransport(FakeSerial(device.handle)))
        progress: list[tuple[int, int]] = []

        hello = client.hello()
        self.assertEqual(hello.detail, "fake-device")
        info = client.info()
        self.assertEqual(info.detail, "mode=test")
        client.flash_stream(
            io.BytesIO(image),
            image_size=len(image),
            image_sha256=hashlib.sha256(image).digest(),
            image_version="test-v2",
            progress=lambda sent, total: progress.append((sent, total)),
        )

        self.assertEqual(bytes(device.received), image)
        self.assertEqual(progress[0], (0, len(image)))
        self.assertEqual(progress[-1], (len(image), len(image)))
        self.assertEqual(device.commands.count(Command.DATA), 3)
        self.assertEqual(device.commands[-1], Command.END)

    def test_device_error_is_raised(self) -> None:
        def handler(request: Frame, _write_count: int) -> bytes:
            response = Response(
                Status.ERR_IMAGE,
                request.sequence,
                0,
                "bad image",
            )
            return make_response(request, response)

        client = IapClient(SerialTransport(FakeSerial(handler)))
        with self.assertRaisesRegex(DeviceResponseError, "bad image"):
            client.hello()


if __name__ == "__main__":
    unittest.main()
