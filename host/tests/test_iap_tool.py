import unittest

from host.iap_tool import normal_reset, validate_peer
from host.transport import IapTransportError


class FakeControlPort:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, bool]] = []

    @property
    def dtr(self) -> bool:
        return False

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self.transitions.append(("dtr", value))

    @property
    def rts(self) -> bool:
        return False

    @rts.setter
    def rts(self, value: bool) -> None:
        self.transitions.append(("rts", value))


class NormalResetTests(unittest.TestCase):
    def test_gpio0_stays_high_while_en_is_pulsed(self) -> None:
        port = FakeControlPort()
        delays: list[float] = []

        normal_reset(port, delays.append)

        self.assertEqual(
            port.transitions,
            [("dtr", False), ("rts", True), ("rts", False)],
        )
        self.assertEqual(delays, [0.1, 0.05])


class PeerValidationTests(unittest.TestCase):
    def test_default_mode_requires_esp32s3_bootloader(self) -> None:
        validate_peer(
            "serial-iap-bootloader;protocol=1;chip=esp32s3",
            expect_application=False,
        )

    def test_no_reset_mode_requires_esp32s3_application(self) -> None:
        validate_peer(
            "serial-iap-app;protocol=1;chip=esp32s3",
            expect_application=True,
        )

    def test_wrong_implementation_is_rejected(self) -> None:
        with self.assertRaisesRegex(IapTransportError, "expected serial-iap-bootloader"):
            validate_peer(
                "serial-iap-app;protocol=1;chip=esp32s3",
                expect_application=False,
            )

    def test_wrong_protocol_is_rejected(self) -> None:
        with self.assertRaisesRegex(IapTransportError, "unsupported protocol 2"):
            validate_peer(
                "serial-iap-bootloader;protocol=2;chip=esp32s3",
                expect_application=False,
            )

    def test_wrong_chip_is_rejected(self) -> None:
        with self.assertRaisesRegex(IapTransportError, "unexpected chip esp32"):
            validate_peer(
                "serial-iap-bootloader;protocol=1;chip=esp32",
                expect_application=False,
            )


if __name__ == "__main__":
    unittest.main()
