import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.clipboard_timestamps import ClipboardTimestampStore, content_key, utf8_size_at_most


class ClipboardTimestampStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temporary_directory.name, "clipboard_timestamps.json")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_restart_keeps_first_seen_timestamp_and_expired_entry_remains_expired(self):
        store = ClipboardTimestampStore(self.path)
        original = "- confidential text"
        store.sync([original], current_time=100.0)

        restarted = ClipboardTimestampStore(self.path)
        timestamps = restarted.sync([original], current_time=999.0)

        self.assertEqual(timestamps[content_key(original)], 100.0)
        self.assertLess(timestamps[content_key(original)] + 60, 999.0)

    def test_missing_and_invalid_sidecar_migrate_current_history_safely(self):
        entry = "- older item"
        missing = ClipboardTimestampStore(self.path)
        self.assertEqual(missing.sync([entry], current_time=50.0)[content_key(entry)], 50.0)

        with open(self.path, "w", encoding="utf-8") as target:
            target.write("not json")
        invalid = ClipboardTimestampStore(self.path)
        self.assertEqual(invalid.sync([entry], current_time=75.0)[content_key(entry)], 75.0)

    def test_atomic_replace_failure_preserves_existing_file_and_cleans_temp_file(self):
        original = {"version": 1, "timestamps": {"old": 1.0}}
        with open(self.path, "w", encoding="utf-8") as target:
            json.dump(original, target)
        store = ClipboardTimestampStore(self.path)
        store.timestamps = {"new": 2.0}

        with mock.patch("core.clipboard_timestamps.os.replace", side_effect=OSError("blocked")):
            self.assertFalse(store.save())

        with open(self.path, encoding="utf-8") as source:
            self.assertEqual(json.load(source), original)
        self.assertEqual(
            list(Path(self.temporary_directory.name).glob(".clipboard-timestamps-*.tmp")), []
        )

    def test_sync_removes_metadata_after_manual_capacity_and_automatic_cleanup(self):
        entries = ["- manual", "- capacity", "- automatic"]
        store = ClipboardTimestampStore(self.path)
        store.sync(entries, current_time=10.0)
        store.sync(entries[1:], current_time=20.0)  # manual cleanup
        store.sync(entries[2:], current_time=30.0)  # capacity cleanup
        remaining = store.sync([], current_time=40.0)  # automatic cleanup

        self.assertEqual(remaining, {})
        self.assertEqual(ClipboardTimestampStore(self.path).timestamps, {})

    def test_utf8_limit_uses_bounded_chunk_encoding(self):
        size, exceeded = utf8_size_at_most("测" * 10, 29, chunk_size=4)
        self.assertEqual(size, 30)
        self.assertTrue(exceeded)


class ClipboardCaptureSafetyTests(unittest.TestCase):
    @staticmethod
    def _controller(module, text, logs):
        clipboard = types.SimpleNamespace(
            mimeData=lambda: types.SimpleNamespace(hasText=lambda: True),
            text=lambda: text,
        )
        manager = types.SimpleNamespace(add_calls=[])
        manager.add_to_clipboard_history = lambda value: manager.add_calls.append(value) or True
        return types.SimpleNamespace(
            settings=types.SimpleNamespace(clipboard_memory_enabled=True),
            app=types.SimpleNamespace(clipboard=lambda: clipboard),
            ignore_next_clipboard_change=False,
            word_manager=manager,
            popup=types.SimpleNamespace(isVisible=lambda: False),
            sync_clipboard_timestamps=lambda current_time=None: None,
        )

    def _load_controller(self):
        # The focused controller tests do not initialize Qt; install minimal modules first.
        return __import__("tests.test_word_source_data_safety", fromlist=["main_controller_module"]).main_controller_module

    def test_oversized_text_is_not_persisted(self):
        module = self._load_controller()
        logs = []
        controller = self._controller(module, "x" * (1024 * 1024 + 1), logs)
        with mock.patch.object(module, "log", logs.append), mock.patch.object(module, "CLIPBOARD_TEXT_MAX_BYTES", 1024 * 1024, create=True):
            module.MainController.on_clipboard_changed(controller)

        self.assertEqual(controller.word_manager.add_calls, [])
        self.assertTrue(any("跳过过大的剪贴板文本" in message for message in logs))

    def test_clipboard_logs_do_not_include_captured_text(self):
        module = self._load_controller()
        secret = "secret-token-do-not-log"
        logs = []
        controller = self._controller(module, secret, logs)
        with mock.patch.object(module, "log", logs.append), mock.patch.object(module, "CLIPBOARD_TEXT_MAX_BYTES", 1024 * 1024, create=True):
            module.MainController.on_clipboard_changed(controller)

        self.assertEqual(controller.word_manager.add_calls, [secret])
        self.assertTrue(logs)
        self.assertTrue(all(secret not in message for message in logs))

    def test_selecting_clipboard_entry_does_not_log_its_text(self):
        module = self._load_controller()
        secret = "- clipboard-secret-do-not-log"
        logs = []
        clipboard_block = {
            "full_content": secret,
            "parent": secret[2:],
            "raw_lines": [secret],
            "exclude_parent": False,
            "is_clipboard": True,
        }
        controller = types.SimpleNamespace(
            _find_block_by_full_content=lambda text: clipboard_block,
            _build_output_content=lambda text, block: block["parent"],
            render_template_output=lambda text: text,
            settings=types.SimpleNamespace(
                transcription_enabled=True,
                transcription_rules='"clipboard-secret-do-not-log" => "converted"',
            ),
            record_entry_usage=lambda block: None,
            ignore_next_clipboard_change=False,
            last_popup_target_hwnd=0,
            schedule_paste=lambda target_hwnd, origin: None,
        )

        with mock.patch.object(module, "log", logs.append), mock.patch.object(
            module.pyperclip,
            "copy",
            return_value=None,
            create=True,
        ) as copy_text:
            module.MainController.on_suggestion_selected(controller, secret)
        copy_text.assert_called_once_with("converted")

        self.assertTrue(any("source=clipboard" in message for message in logs))
        self.assertTrue(all(secret not in message for message in logs))

    def test_expired_entry_is_deleted_after_restart(self):
        module = self._load_controller()
        old_entry = "- persisted old entry"
        deleted = []
        sync_calls = []
        controller = types.SimpleNamespace(
            settings=types.SimpleNamespace(
                clipboard_auto_clear_enabled=True,
                clipboard_auto_clear_minutes=1,
            ),
            word_manager=types.SimpleNamespace(
                clipboard_history=[{"full_content": old_entry}],
                clipboard_source=types.SimpleNamespace(
                    delete_entry=lambda content: deleted.append(content) or True,
                ),
            ),
            clipboard_timestamps={content_key(old_entry): 100.0},
            sync_clipboard_timestamps=lambda current_time=None: sync_calls.append(current_time),
            popup=types.SimpleNamespace(isVisible=lambda: False),
        )
        controller.word_manager.load_clipboard_history = lambda: None

        with mock.patch.object(module.time, "time", return_value=200.0):
            module.MainController.check_clipboard_auto_clear(controller)

        self.assertEqual(deleted, [old_entry])
        self.assertEqual(sync_calls, [200.0])


if __name__ == "__main__":
    unittest.main()
