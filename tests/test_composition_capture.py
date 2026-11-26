
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from services.composition_capture import CompositionCapture


class CaptureSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        controller = QObject()
        controller._get_foreground_hwnd = mock.Mock(return_value=41)
        controller._get_window_process_id = mock.Mock(side_effect={41: 7, 52: 7, 55: 9}.get)
        controller._get_pressed_modifiers = mock.Mock(return_value=[])
        controller.user32 = mock.Mock()
        controller.user32.GetClipboardSequenceNumber.return_value = 31
        controller.user32.GetClipboardOwner.return_value = 52
        controller.app = mock.Mock()
        controller.app.clipboard.return_value.text.return_value = "Fictional task selection"
        self.controller = controller
        self.capture = CompositionCapture(controller)
        self.capture.request = (41, 7, 30, time.monotonic())
        self.capture.process = mock.Mock()
        self.capture.process.poll.return_value = 0
        self.capture.success_deadline = time.monotonic() + 1
        self.received = []
        self.capture.received.connect(self.received.append)
        self.addCleanup(self.capture.stop)

    def test_capture_requires_the_verified_clipboard_owner(self):
        self.capture.poll()
        self.assertEqual(self.received, ["Fictional task selection"])
        self.assertIsNone(self.capture.request)

    def test_foreign_clipboard_owner_does_not_read_the_clipboard(self):
        self.controller.user32.GetClipboardOwner.return_value = 55
        self.capture.poll()
        self.controller.app.clipboard.assert_not_called()
        self.assertFalse(self.received)

    def test_target_switch_aborts_without_reading_clipboard(self):
        self.controller._get_foreground_hwnd.return_value = 55
        self.capture.poll()
        self.controller.app.clipboard.assert_not_called()
        self.assertIsNone(self.capture.request)

    def test_menu_can_restore_verified_target_but_does_not_steal_unrelated_foreground(self):
        for before_restore, restores in ((99, True), (55, False), (41, False)):
            with self.subTest(before_restore=before_restore):
                self.capture.stop()
                self.controller.quickkv_pid = 42
                self.controller.popup = mock.Mock()
                self.controller._get_window_process_id.side_effect = {41: 7, 55: 9, 99: 42}.get
                self.controller._get_foreground_hwnd.side_effect = [99, before_restore]
                self.controller.user32.SetForegroundWindow.reset_mock()
                self.capture.begin(41)
                self.assertEqual(self.controller.user32.SetForegroundWindow.called, restores)
                self.assertEqual(self.capture.request[:3], (41, 7, 31))

    def test_native_selection_has_priority_without_reading_the_clipboard(self):
        self.capture.process = None
        self.capture.request = (41, 7, 31, time.monotonic())
        with mock.patch("services.composition_capture.native_edit_selection",
                        return_value="Fictional native selection"):
            self.capture.poll()
        self.assertEqual(self.received, ["Fictional native selection"])
        self.controller.app.clipboard.assert_not_called()
