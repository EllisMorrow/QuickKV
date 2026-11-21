import time
import types
import unittest
from unittest import mock

from tests.test_word_source_data_safety import main_controller_module


class ContinuousOutputTests(unittest.TestCase):
    def make_controller(self, *, elapsed=0, foreground=41, process=None):
        controller = types.SimpleNamespace(
            continuous_request={
                "target": (41, 7),
                "draft": "before\nsecond line",
                "output": "after\nsecond line",
                "started_at": time.monotonic() - elapsed,
                "stable_hits": 0,
            },
            continuous_paste_timer=mock.Mock(),
            continuous_process=process,
            paste_timeout_ms=650,
            pending_paste_request=None,
            popup=mock.Mock(),
            user32=mock.Mock(),
            _get_foreground_hwnd=lambda: foreground,
            _get_pressed_modifiers=lambda: [],
            _get_window_process_id=lambda hwnd: 7,
            _copy_continuous_output=mock.Mock(return_value=True),
            _start_continuous_paste_process=mock.Mock(),
        )
        for name in ("_poll_continuous_paste", "_finish_continuous_paste"):
            setattr(controller, name, types.MethodType(
                getattr(main_controller_module.MainController, name), controller,
            ))
        return controller

    def test_timeout_never_copies_or_dispatches_to_the_other_window(self):
        controller = self.make_controller(elapsed=1, foreground=55)

        with mock.patch.object(main_controller_module.QMessageBox, "warning", create=True):
            controller._poll_continuous_paste()

        controller._copy_continuous_output.assert_not_called()
        controller._start_continuous_paste_process.assert_not_called()
        controller.popup.continuous_editor.complete_submission.assert_not_called()
        controller.popup.resume_continuous_after_paste_failure.assert_called_once()

    def test_waits_for_stable_target_and_writes_transformed_draft(self):
        controller = self.make_controller()
        controller.user32.GetClipboardSequenceNumber.return_value = 30
        with mock.patch.object(
            main_controller_module.pyperclip, "paste", return_value="after\nsecond line", create=True,
        ):
            controller._poll_continuous_paste()
            controller._copy_continuous_output.assert_not_called()
            controller._poll_continuous_paste()

        controller._copy_continuous_output.assert_called_once_with("after\nsecond line")
        controller._start_continuous_paste_process.assert_called_once_with(41, 7, 30)
        controller.popup.continuous_editor.complete_submission.assert_not_called()

    def test_clipboard_change_prevents_dispatch(self):
        controller = self.make_controller()
        controller.continuous_request["stable_hits"] = 1
        controller.user32.GetClipboardSequenceNumber.side_effect = (30, 31)
        with (
            mock.patch.object(
                main_controller_module.pyperclip, "paste", return_value="after\nsecond line", create=True,
            ),
            mock.patch.object(main_controller_module.QMessageBox, "warning", create=True),
        ):
            controller._poll_continuous_paste()

        controller._start_continuous_paste_process.assert_not_called()
        controller.popup.continuous_editor.complete_submission.assert_not_called()
        controller.popup.resume_continuous_after_paste_failure.assert_called_once()

    def test_only_successfully_dispatched_draft_is_completed(self):
        for exit_code in (0, 2):
            with self.subTest(exit_code=exit_code):
                process = mock.Mock()
                process.poll.return_value = exit_code
                controller = self.make_controller(process=process)

                with mock.patch.object(main_controller_module.QMessageBox, "warning", create=True):
                    controller._poll_continuous_paste()

                editor = controller.popup.continuous_editor
                editor.set_submission_pending.assert_called_once_with(False)
                if exit_code == 0:
                    editor.complete_submission.assert_called_once_with("before\nsecond line")
                    controller.popup.resume_continuous_after_paste_failure.assert_not_called()
                else:
                    editor.complete_submission.assert_not_called()
                    controller.popup.resume_continuous_after_paste_failure.assert_called_once()

    def test_copy_keeps_clipboard_if_transcription_removes_the_draft(self):
        controller_type = main_controller_module.MainController
        controller = types.SimpleNamespace(
            continuous_request=None,
            popup=mock.Mock(),
            _transcribe_output=lambda text: "",
            _copy_continuous_output=mock.Mock(),
        )
        controller._prepare_continuous_output = types.MethodType(
            controller_type._prepare_continuous_output, controller,
        )
        with mock.patch.object(main_controller_module.QMessageBox, "warning", create=True) as warning:
            controller_type.copy_continuous_draft(controller, "draft")

        controller._copy_continuous_output.assert_not_called()
        warning.assert_called_once()
