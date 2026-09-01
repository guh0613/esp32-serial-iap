import os
import sys
import unittest
from unittest import mock

try:
    from host import gui
except ImportError:  # pragma: no cover - Python built without _tkinter
    gui = None


@unittest.skipIf(gui is None, "tkinter is unavailable in this interpreter")
class PreferredPortTests(unittest.TestCase):
    def test_prefers_a_usb_bridge_over_a_bluetooth_port(self) -> None:
        ports = [
            ("/dev/cu.HECATEG2000", "/dev/cu.HECATEG2000 — n/a"),
            ("/dev/cu.usbserial-A5069RR4", "/dev/cu.usbserial-A5069RR4 — FT232R USB UART"),
        ]
        self.assertEqual(gui.preferred_port(ports), "/dev/cu.usbserial-A5069RR4")

@unittest.skipIf(gui is None, "tkinter is unavailable in this interpreter")
class TclLibraryPathTests(unittest.TestCase):
    def test_no_op_outside_a_virtual_environment(self) -> None:
        with mock.patch.object(sys, "prefix", sys.base_prefix), mock.patch.dict(
            os.environ, {}, clear=True
        ):
            gui.ensure_tcl_library_path()
            self.assertNotIn("TCL_LIBRARY", os.environ)

    def test_points_at_the_base_prefix_library_inside_a_virtual_environment(self) -> None:
        if sys.prefix == sys.base_prefix:
            self.skipTest("test interpreter is not a virtual environment")
        with mock.patch.dict(os.environ, {}, clear=True):
            gui.ensure_tcl_library_path()
            self.assertTrue(os.environ["TCL_LIBRARY"].startswith(sys.base_prefix))

    def test_keeps_an_explicit_override(self) -> None:
        with mock.patch.dict(os.environ, {"TCL_LIBRARY": "/custom/tcl"}, clear=True):
            gui.ensure_tcl_library_path()
            self.assertEqual(os.environ["TCL_LIBRARY"], "/custom/tcl")


if __name__ == "__main__":
    unittest.main()
