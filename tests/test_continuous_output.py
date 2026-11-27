import time
import types
import unittest
from unittest import mock

from tests.test_word_source_data_safety import main_controller_module


class ContinuousOutputTests(unittest.TestCase):
    def make_controller(self, *, elapsed=0, foreground=41, process=None):
        controller = types.SimpleNamespace(
            pending_paste_request={
                "hwnd": 41, "pid": 7, "sequence": 30, "started": time.monotonic() - elapsed,
                "stable_hits": 0, "grace_ms": 0, "process": process, "completed": mock.Mock(),
            },
            pending_paste_timer=mock.Mock(), paste_timeout_ms=650,
            _get_foreground_hwnd=lambda: foreground,
            _get_pressed_modifiers=lambda: [],
            _get_window_process_id=lambda hwnd: 7,
            _copy_continuous_output=mock.Mock(),
            user32=mock.Mock(),
            perform_paste_now=mock.Mock(),
        )
        controller.user32.GetClipboardSequenceNumber.return_value = 30
        for name in ("_poll_pending_paste", "_finish_pending_paste", "_cancel_pending_paste"):
            setattr(controller, name, types.MethodType(
                getattr(main_controller_module.MainController, name), controller,
            ))
        return controller

    def test_timeout_never_dispatches_to_the_other_window(self):
        controller = self.make_controller(elapsed=1, foreground=55)
        completed = controller.pending_paste_request["completed"]
        controller._poll_pending_paste()
        controller.perform_paste_now.assert_not_called()
        completed.assert_called_once_with(False, True)
        controller._copy_continuous_output.assert_not_called()

    def test_waits_for_stable_target_before_shared_dispatch(self):
        controller = self.make_controller()
        request = controller.pending_paste_request
        controller._poll_pending_paste()
        controller.perform_paste_now.assert_not_called()
        controller._poll_pending_paste()
        controller.perform_paste_now.assert_called_once_with(request)

    def test_clipboard_change_prevents_dispatch(self):
        controller = self.make_controller()
        completed = controller.pending_paste_request["completed"]
        controller.user32.GetClipboardSequenceNumber.return_value = 31
        controller._poll_pending_paste()
        controller.perform_paste_now.assert_not_called()
        completed.assert_called_once_with(False, False)

    def test_reports_dispatch_only_for_successful_process(self):
        for exit_code in (0, 2):
            with self.subTest(exit_code=exit_code):
                process = mock.Mock()
                process.poll.return_value = exit_code
                controller = self.make_controller(process=process)
                completed = controller.pending_paste_request["completed"]
                controller._poll_pending_paste()
                completed.assert_called_once_with(exit_code == 0, True)

    def test_reused_window_identity_cancels_before_dispatch(self):
        controller = self.make_controller()
        completed = controller.pending_paste_request["completed"]
        controller._get_window_process_id = lambda hwnd: 8
        controller._poll_pending_paste()
        controller.perform_paste_now.assert_not_called()
        completed.assert_called_once_with(False, True)

    def test_both_editors_use_shared_scheduler_and_prepare_output_once(self):
        controller_type = main_controller_module.MainController
        for composition in (False, True):
            with self.subTest(composition=composition):
                ui = mock.Mock()
                controller = types.SimpleNamespace(
                    popup=ui, pending_paste_request=None,
                    composition_capture=types.SimpleNamespace(request=None),
                    settings=types.SimpleNamespace(paste_mode="ctrl_v"),
                    _prepare_editor_output=mock.Mock(return_value="transformed"),
                    _copy_continuous_output=mock.Mock(return_value=True),
                    _verify_paste_clipboard=mock.Mock(return_value=30),
                    _get_foreground_hwnd=lambda: 93,
                    _is_quickkv_foreground=lambda hwnd: False,
                    schedule_paste=mock.Mock(return_value=True),
                )
                with mock.patch.object(main_controller_module, "PASTE_MODE_TYPING", "typing", create=True):
                    controller_type._submit_editor_output(controller, "source", composition=composition)
                controller._prepare_editor_output.assert_called_once()
                controller._copy_continuous_output.assert_called_once_with("transformed")
                controller.schedule_paste.assert_called_once()
                target, = controller.schedule_paste.call_args.args
                self.assertEqual(target, 93)
                self.assertEqual(controller.schedule_paste.call_args.kwargs["clipboard_sequence"], 30)
                controller.schedule_paste.call_args.kwargs["completed"](False, True)
                controller._copy_continuous_output.assert_called_once_with("transformed")

    def test_copy_keeps_clipboard_if_transcription_removes_the_draft(self):
        controller_type = main_controller_module.MainController
        controller = types.SimpleNamespace(
            popup=mock.Mock(),
            settings=types.SimpleNamespace(transcription_enabled=True, transcription_rules="fixture"),
            _copy_continuous_output=mock.Mock(),
        )
        for name in ("_prepare_editor_output", "_copy_editor_output"):
            setattr(controller, name, types.MethodType(getattr(controller_type, name), controller))
        with mock.patch.object(main_controller_module, "transcribe", return_value="") as transform:
            controller_type.copy_continuous_draft(controller, "draft")

        transform.assert_called_once_with("draft", "fixture")
        controller._copy_continuous_output.assert_not_called()
        controller.popup.continuous_editor.report_output.assert_called_once_with("转写后的正文为空")
