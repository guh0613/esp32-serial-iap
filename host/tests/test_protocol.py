import hashlib
import unittest

from host.protocol import (
    BeginRequest,
    Command,
    DataRequest,
    Frame,
    MAX_PAYLOAD_LENGTH,
    ProtocolError,
    Response,
    Status,
    StreamDecoder,
    crc16_ccitt_false,
    decode_begin_request,
    decode_data_request,
    decode_frame,
    decode_response,
    encode_begin_request,
    encode_data_request,
    encode_frame,
    encode_response,
    response_command,
)


class FrameCodecTests(unittest.TestCase):
    def test_crc16_standard_check_value(self) -> None:
        self.assertEqual(crc16_ccitt_false(b"123456789"), 0x29B1)

    def test_round_trip_preserves_binary_payload(self) -> None:
        frame = Frame(
            command=Command.DATA,
            sequence=0x1234,
            payload=b"\x00\xA5\x5A\xFFbinary",
        )
        self.assertEqual(decode_frame(encode_frame(frame)), frame)

    def test_hello_matches_cross_language_golden_vector(self) -> None:
        expected = bytes.fromhex("a55a010100000000e1e100000000")
        self.assertEqual(encode_frame(Frame(Command.HELLO, 0)), expected)

    def test_oversized_payload_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_frame(
                Frame(Command.DATA, 0, bytes(MAX_PAYLOAD_LENGTH + 1))
            )

    def test_truncated_frame_is_rejected(self) -> None:
        encoded = encode_frame(Frame(Command.HELLO, 0))
        with self.assertRaises(ProtocolError):
            decode_frame(encoded[:-1])

    def test_response_command_sets_high_bit(self) -> None:
        self.assertEqual(response_command(Command.BEGIN), 0x90)
        with self.assertRaises(ProtocolError):
            response_command(0x90)


class StreamDecoderTests(unittest.TestCase):
    def test_one_byte_at_a_time(self) -> None:
        expected = Frame(Command.INFO, 42, b"info")
        decoder = StreamDecoder()
        frames = []
        for value in encode_frame(expected):
            frames.extend(decoder.feed(bytes([value])))
        self.assertEqual(frames, [expected])
        self.assertEqual(decoder.buffered_length, 0)

    def test_noise_and_concatenated_frames(self) -> None:
        first = Frame(Command.HELLO, 0)
        second = Frame(Command.INFO, 1, b"x")
        decoder = StreamDecoder()
        frames = decoder.feed(
            b"boot log\r\n" + encode_frame(first) + encode_frame(second)
        )
        self.assertEqual(frames, [first, second])
        self.assertTrue(decoder.pop_issues())

    def test_partial_sof_is_retained(self) -> None:
        expected = Frame(Command.HELLO, 0)
        encoded = encode_frame(expected)
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(b"noise\xA5"), [])
        self.assertEqual(decoder.feed(encoded[1:]), [expected])

    def test_invalid_header_resynchronizes(self) -> None:
        bad = bytearray(encode_frame(Frame(Command.INFO, 4, b"bad")))
        bad[3] ^= 0x01
        good = Frame(Command.INFO, 5, b"good")
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(bytes(bad) + encode_frame(good)), [good])
        self.assertTrue(decoder.pop_issues())

    def test_invalid_payload_crc_does_not_hide_next_frame(self) -> None:
        bad = bytearray(encode_frame(Frame(Command.DATA, 8, b"payload")))
        bad[-1] ^= 0x80
        good = Frame(Command.ABORT, 9)
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(bytes(bad) + encode_frame(good)), [good])
        self.assertTrue(decoder.pop_issues())


class CommandPayloadTests(unittest.TestCase):
    def test_begin_round_trip(self) -> None:
        digest = hashlib.sha256(b"firmware").digest()
        request = BeginRequest(123456, digest, "v1.2.3")
        self.assertEqual(decode_begin_request(encode_begin_request(request)), request)

    def test_begin_rejects_wrong_digest_size(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_begin_request(BeginRequest(10, b"short", "v1"))

    def test_data_round_trip(self) -> None:
        request = DataRequest(4096, b"\x01\x02\x03")
        self.assertEqual(decode_data_request(encode_data_request(request)), request)

    def test_empty_data_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_data_request(DataRequest(0, b""))

    def test_response_round_trip(self) -> None:
        response = Response(
            status=Status.ERR_OFFSET,
            expected_sequence=11,
            next_offset=8192,
            detail="expected another offset",
        )
        self.assertEqual(decode_response(encode_response(response)), response)


if __name__ == "__main__":
    unittest.main()
