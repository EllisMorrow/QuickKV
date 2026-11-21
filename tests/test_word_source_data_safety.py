import importlib
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


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


def _restore_module_registry(original_modules):
    """Keep local import fakes from changing discovery behavior downstream."""
    for name in list(sys.modules):
        if name not in original_modules:
            del sys.modules[name]
    for name, module in original_modules.items():
        sys.modules[name] = module


def _load_word_source_module():
    widgets = {
        name: _Dummy
        for name in (
            "QApplication QWidget QVBoxLayout QHBoxLayout QLineEdit QListWidget "
            "QListWidgetItem QSystemTrayIcon QMenu QSizeGrip QGraphicsDropShadowEffect "
            "QPushButton QInputDialog QMessageBox QStyledItemDelegate QStyle QFileDialog "
            "QCheckBox QWidgetAction QScrollArea QLabel QFrame"
        ).split()
    }
    core = {
        name: _Dummy
        for name in "Qt Signal Slot QObject QTimer QEvent QRect QProcess".split()
    }
    gui = {
        name: _Dummy
        for name in "QIcon QAction QCursor QPixmap QPainter QColor QPalette QActionGroup".split()
    }

    _module("PySide6")
    _module("PySide6.QtWidgets", **widgets)
    _module("PySide6.QtCore", **core)
    _module("PySide6.QtGui", **gui)
    _module("pyperclip")
    _module("pypinyin", pinyin=lambda *args, **kwargs: [], Style=_Dummy)
    pynput = _module("pynput")
    pynput.keyboard = _module("pynput.keyboard")
    _module("fuzzywuzzy", fuzz=_Dummy())
    _module("watchdog")
    _module("watchdog.observers", Observer=_Dummy)
    _module("watchdog.events", FileSystemEventHandler=_Dummy)
    _module(
        "core.config",
        log=lambda message: None,
        list_eligible_md_files_with_status=lambda path: ([], True),
    )

    sys.modules.pop("core.word_source", None)
    return importlib.import_module("core.word_source")


def _load_search_popup_module():
    qt = types.SimpleNamespace(UserRole=object())
    qt_core = sys.modules["PySide6.QtCore"]
    qt_core.Qt = qt
    qt_core.Signal = lambda *args, **kwargs: _Dummy()
    qt_core.Slot = lambda *args, **kwargs: lambda function: function
    _module("ui.delegates", StyledItemDelegate=_Dummy)
    _module("ui.components", EditDialog=_Dummy, ScrollableMessageBox=_Dummy)
    _module("ui.continuous_editor", ContinuousEditor=_Dummy)

    sys.modules.pop("ui.search_popup", None)
    return importlib.import_module("ui.search_popup")


def _load_word_manager_module():
    sys.modules.pop("core.word_manager", None)
    return importlib.import_module("core.word_manager")


def _load_main_controller_module():
    class QDialog(_Dummy):
        Accepted = 1

    widgets = sys.modules["PySide6.QtWidgets"]
    widgets.QDialog = QDialog
    widgets.QMessageBox = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
    _module("PySide6.QtNetwork", QLocalServer=_Dummy, QLocalSocket=_Dummy)
    _module(
        "core.template_renderer",
        TemplateRenderer=_Dummy,
        TemplateRenderError=Exception,
    )

    components = sys.modules["ui.components"]
    for name in (
        "HotkeyDialog DisclaimerDialog ScrollableMessageBox EditDialog TemplateInputDialog"
    ).split():
        setattr(components, name, _Dummy)
    components.get_disclaimer_html_text = lambda: ""

    _module("services.hotkey_manager", NativeHotkeyManager=_Dummy)
    _module("services.shortcut_listener", ShortcutListener=_Dummy)

    sys.modules.pop("main_controller", None)
    return importlib.import_module("main_controller")


_modules_before_stub_loading = dict(sys.modules)
try:
    word_source_module = _load_word_source_module()
    WordSource = word_source_module.WordSource
    search_popup_module = _load_search_popup_module()
    word_manager_module = _load_word_manager_module()
    main_controller_module = _load_main_controller_module()
finally:
    _restore_module_registry(_modules_before_stub_loading)


class WordSourceDataSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_bytes(self, name, content):
        path = self.directory / name
        path.write_bytes(content)
        return path

    def test_update_preserves_preamble_other_blocks_and_original_whitespace(self):
        original = (
            b"\xef\xbb\xbf---\r\ntitle: Sample\r\n---\r\n# Heading  \r\n\r\n"
            b"- first  \r\nchild line \t\r\n"
            b"- second  \r\nsecond child  \r\n"
        )
        path = self._write_bytes("library.md", original)
        source = WordSource(path)

        first = source.word_blocks[0]
        self.assertTrue(source.update_entry(first["full_content"], "- changed\nchild line", first["block_index"]))

        self.assertEqual(
            path.read_bytes(),
            (
                b"\xef\xbb\xbf---\r\ntitle: Sample\r\n---\r\n# Heading  \r\n\r\n"
                b"- changed\nchild line \t\r\n"
                b"- second  \r\nsecond child  \r\n"
            ),
        )

    def test_duplicate_content_updates_and_deletes_only_selected_occurrence(self):
        path = self._write_bytes(
            "duplicates.md",
            b"intro\r\n\r\n- same\r\n- same\r\n- tail\r\n",
        )
        source = WordSource(path)
        second_duplicate = source.word_blocks[1]

        self.assertTrue(
            source.update_entry(
                second_duplicate["full_content"],
                "- changed",
                second_duplicate["block_index"],
            )
        )
        self.assertEqual(path.read_bytes(), b"intro\r\n\r\n- same\r\n- changed\r\n- tail\r\n")

        source.load()
        changed = source.word_blocks[1]
        self.assertTrue(source.delete_entry(changed["full_content"], changed["block_index"]))
        self.assertEqual(path.read_bytes(), b"intro\r\n\r\n- same\r\n- tail\r\n")

    def test_same_content_in_different_sources_changes_only_selected_source(self):
        first_path = self._write_bytes("first.md", b"# First\n- same\n")
        second_path = self._write_bytes("second.md", b"# Second\n- same\n")
        first_source = WordSource(first_path)
        second_source = WordSource(second_path)
        selected = second_source.word_blocks[0]

        self.assertTrue(
            second_source.update_entry(
                selected["full_content"],
                "- selected source",
                selected["block_index"],
            )
        )

        self.assertEqual(first_path.read_bytes(), b"# First\n- same\n")
        self.assertEqual(second_path.read_bytes(), b"# Second\n- selected source\n")

    def test_atomic_replace_uses_same_directory_and_cleans_temporary_file(self):
        path = self._write_bytes("atomic.md", b"- old\n")
        source = WordSource(path)
        selected = source.word_blocks[0]
        replace_name = "_replace_file_windows" if sys.platform == "win32" else "replace"
        replace_target = source if sys.platform == "win32" else word_source_module.os
        real_replace = getattr(replace_target, replace_name)

        with mock.patch.object(replace_target, replace_name, wraps=real_replace) as replace:
            self.assertTrue(
                source.update_entry(
                    selected["full_content"],
                    "- new",
                    selected["block_index"],
                )
            )

        if sys.platform == "win32":
            destination, temp_path, backup_path = replace.call_args.args
            self.assertEqual(Path(backup_path).parent, path.parent)
        else:
            temp_path, destination = replace.call_args.args
        self.assertEqual(Path(temp_path).parent, path.parent)
        self.assertEqual(Path(destination), path)
        self.assertEqual(path.read_bytes(), b"- new\n")
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.bak")), [])

    def test_atomic_replace_failure_keeps_original_and_cleans_temporary_file(self):
        path = self._write_bytes("failure.md", b"preamble\n- old\n")
        source = WordSource(path)
        selected = source.word_blocks[0]

        replace_name = "_replace_file_windows" if sys.platform == "win32" else "replace"
        replace_target = source if sys.platform == "win32" else word_source_module.os
        with mock.patch.object(replace_target, replace_name, side_effect=OSError("replace failed")):
            self.assertFalse(
                source.update_entry(
                    selected["full_content"],
                    "- new",
                    selected["block_index"],
                )
            )

        self.assertEqual(path.read_bytes(), b"preamble\n- old\n")
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.bak")), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows ReplaceFileW guarantee")
    def test_replacement_instant_race_restores_external_version_for_update_and_delete(self):
        original = b"preamble\n- selected\n- tail\n"
        external = b"external writer won\n- external\n"

        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                path = self._write_bytes(f"replacement-race-{operation}.md", original)
                source = WordSource(path)
                selected = source.word_blocks[0]
                real_replace = source._replace_file_windows
                replace_count = 0

                def replace_with_race(target_path, replacement_path, backup_path):
                    nonlocal replace_count
                    replace_count += 1
                    if replace_count == 1:
                        path.write_bytes(external)
                    return real_replace(target_path, replacement_path, backup_path)

                with mock.patch.object(
                    source,
                    "_replace_file_windows",
                    side_effect=replace_with_race,
                ):
                    if operation == "update":
                        result = source.update_entry(
                            selected["full_content"],
                            "- changed",
                            selected["block_index"],
                            selected["source_revision"],
                        )
                    else:
                        result = source.delete_entry(
                            selected["full_content"],
                            selected["block_index"],
                            selected["source_revision"],
                        )

                self.assertFalse(result)
                self.assertEqual(path.read_bytes(), external)
                self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
                self.assertEqual(list(path.parent.glob(f".{path.name}.*.bak")), [])

    def test_stale_block_index_fails_instead_of_editing_another_entry(self):
        path = self._write_bytes("stale.md", b"- first\n- second\n")
        source = WordSource(path)
        selected = source.word_blocks[1]
        path.write_bytes(b"- inserted\n- first\n- second\n")

        self.assertFalse(
            source.update_entry(
                selected["full_content"],
                "- changed",
                selected["block_index"],
            )
        )
        self.assertEqual(path.read_bytes(), b"- inserted\n- first\n- second\n")

    def test_revision_rejects_identical_duplicate_inserted_before_selected_entry(self):
        original = b"- same\r\n- same\r\n- tail\r\n"
        externally_changed = b"- same\r\n- same\r\n- same\r\n- tail\r\n"

        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                path = self._write_bytes(f"revision-{operation}.md", original)
                source = WordSource(path)
                selected = source.word_blocks[1]
                path.write_bytes(externally_changed)

                if operation == "update":
                    result = source.update_entry(
                        selected["full_content"],
                        "- changed",
                        selected["block_index"],
                        selected["source_revision"],
                    )
                else:
                    result = source.delete_entry(
                        selected["full_content"],
                        selected["block_index"],
                        selected["source_revision"],
                    )

                self.assertFalse(result)
                self.assertEqual(path.read_bytes(), externally_changed)


class SearchPopupSelectionTests(unittest.TestCase):
    def test_edit_and_delete_forward_selected_block_instead_of_display_text(self):
        selected_block = {
            "full_content": "- duplicate",
            "source_path": "selected-library.md",
            "block_index": 1,
        }

        class Item:
            def data(self, role):
                self.role = role
                return selected_block

            def text(self):
                raise AssertionError("display text must not be used to re-find the selected entry")

        class Controller:
            def edit_entry(self, block):
                self.edited = block

            def delete_entry(self, block):
                self.deleted = block

        item = Item()
        controller = Controller()
        popup = types.SimpleNamespace(controller=controller)

        search_popup_module.SearchPopup.edit_item(popup, item)
        search_popup_module.SearchPopup.delete_item(popup, item)

        self.assertIs(item.role, search_popup_module.Qt.UserRole)
        self.assertIs(controller.edited, selected_block)
        self.assertIs(controller.deleted, selected_block)

    def test_controller_uses_source_path_and_index_from_selected_block(self):
        selected_block = {
            "full_content": "- duplicate",
            "source_path": "second-library.md",
            "block_index": 3,
            "source_revision": "selected-revision",
        }

        class Source:
            file_path = "second-library.md"

            def update_entry(self, *args):
                self.updated = args
                return True

            def delete_entry(self, *args):
                self.deleted = args
                return True

        class WordManager:
            def __init__(self, source):
                self.source = source
                self.requested_paths = []

            def get_source_by_path(self, path):
                self.requested_paths.append(path)
                return self.source

        class EditDialog:
            def __init__(self, *args):
                pass

            def exec(self):
                return True

            def get_text(self):
                return "- edited"

        class DeleteDialog:
            def __init__(self, *args, **kwargs):
                pass

            def exec(self):
                return main_controller_module.QDialog.Accepted

        source = Source()
        manager = WordManager(source)
        popup = types.SimpleNamespace(isVisible=lambda: False)
        controller = types.SimpleNamespace(
            word_manager=manager,
            popup=popup,
            settings=types.SimpleNamespace(theme="test", font_size=12),
            schedule_full_reload=lambda: None,
        )
        main_controller_module.THEMES = {"test": {}}
        main_controller_module.EditDialog = EditDialog
        main_controller_module.ScrollableMessageBox = DeleteDialog

        main_controller_module.MainController.edit_entry(controller, selected_block)
        main_controller_module.MainController.delete_entry(controller, selected_block)

        self.assertEqual(manager.requested_paths, ["second-library.md", "second-library.md"])
        self.assertEqual(source.updated, ("- duplicate", "- edited", 3, "selected-revision"))
        self.assertEqual(source.deleted, ("- duplicate", 3, "selected-revision"))


class WordManagerCacheRevisionTests(unittest.TestCase):
    def test_cache_hit_assigns_current_revision_to_legacy_cached_blocks(self):
        cached_block = {"parent": "same", "full_content": "- same"}
        current_revision = "current-file-revision"
        library_path = str(PROJECT_ROOT / "cached-library.md")
        normalized_path = os.path.normcase(os.path.abspath(library_path))
        settings = types.SimpleNamespace(libraries=["configured"], auto_libraries=[])
        manager = word_manager_module.WordManager.__new__(word_manager_module.WordManager)
        manager.settings = settings
        manager.ranking_state = None
        manager._load_cache = lambda: {
            normalized_path: {
                "hash": current_revision,
                "data": [cached_block],
            }
        }
        manager._expand_library_entries = lambda libraries: (
            {normalized_path: library_path} if libraries is settings.libraries else {}
        )
        manager._get_file_hash = lambda path: current_revision
        manager._preprocess_block = lambda block: block
        manager._get_pinyin_sort_key = lambda parent: parent
        manager._save_cache = lambda: None
        manager.load_clipboard_history = lambda: None

        word_manager_module.WordManager.reload_all(manager)

        self.assertEqual(manager.word_blocks[0]["block_index"], 0)
        self.assertEqual(manager.word_blocks[0]["source_revision"], current_revision)


class WordManagerRankingStateRetentionTests(unittest.TestCase):
    class _RankingState:
        def __init__(self):
            self.favorites = {}
            self.usage_stats = {}
            self.cleanup_calls = []

        def make_entry_id(self, source_path, full_content):
            return f"{source_path}\n{full_content}"

        def is_favorite(self, entry_id):
            return self.favorites.get(entry_id, False)

        def get_usage_meta(self, entry_id):
            return self.usage_stats.get(entry_id, {"count": 0, "last_used_at": ""})

        def cleanup_orphans(self, valid_entry_ids):
            valid_entry_ids = set(valid_entry_ids)
            self.cleanup_calls.append(valid_entry_ids)
            self.favorites = {
                entry_id: value
                for entry_id, value in self.favorites.items()
                if entry_id in valid_entry_ids
            }
            self.usage_stats = {
                entry_id: value
                for entry_id, value in self.usage_stats.items()
                if entry_id in valid_entry_ids
            }

    def _assert_disabled_library_retains_ranking_state(self, library_attribute, library_kind):
        with tempfile.TemporaryDirectory() as temp_dir:
            library_path = os.path.join(temp_dir, "library.md")
            Path(library_path).write_text("- sample\n", encoding="utf-8")
            normalized_path = os.path.normcase(os.path.abspath(library_path))
            block = {
                "parent": "sample",
                "full_content": "- sample",
                "source_path": library_path,
            }
            cache = {normalized_path: {"hash": "revision", "data": [block]}}
            configured_path = temp_dir if library_kind == "folder" else library_path
            library = {"path": configured_path, "enabled": True, "kind": library_kind}
            settings = types.SimpleNamespace(libraries=[], auto_libraries=[])
            setattr(settings, library_attribute, [library])

            ranking_state = self._RankingState()
            entry_id = ranking_state.make_entry_id(library_path, block["full_content"])
            ranking_state.favorites[entry_id] = True
            ranking_state.usage_stats[entry_id] = {"count": 3, "last_used_at": "2026-08-09T00:00:00"}

            manager = word_manager_module.WordManager.__new__(word_manager_module.WordManager)
            manager.settings = settings
            manager.ranking_state = ranking_state
            def load_valid_cache():
                manager._cache_load_succeeded = True
                return cache
            manager._load_cache = load_valid_cache
            manager._get_file_hash = lambda path: "revision"
            manager._get_pinyin_sort_key = lambda parent: parent
            manager._save_cache = lambda: None
            manager.load_clipboard_history = lambda: None
            manager._expand_library_entries = lambda libraries: (
                {normalized_path: library_path}
                if any(item.get("enabled", True) for item in libraries)
                else {}
            )

            with mock.patch.object(
                word_manager_module,
                "normalize_library_path",
                side_effect=lambda path: os.path.normcase(os.path.abspath(path)),
                create=True,
            ), mock.patch.object(
                word_manager_module,
                "list_eligible_md_files_with_status",
                side_effect=lambda folder: ([library_path], True),
                create=True,
            ):
                word_manager_module.WordManager.reload_all(manager)

                library["enabled"] = False
                word_manager_module.WordManager.reload_all(manager)
                self.assertIn(entry_id, ranking_state.favorites)
                self.assertIn(entry_id, ranking_state.usage_stats)
                self.assertIn(normalized_path, cache)
                self.assertIn(entry_id, ranking_state.cleanup_calls[-1])

                library["enabled"] = True
                word_manager_module.WordManager.reload_all(manager)
                restored_block = manager.word_blocks[0]
                self.assertTrue(restored_block["is_favorite"])
                self.assertEqual(3, restored_block["usage_meta"]["count"])

                setattr(settings, library_attribute, [])
                word_manager_module.WordManager.reload_all(manager)
                self.assertNotIn(entry_id, ranking_state.favorites)
                self.assertNotIn(entry_id, ranking_state.usage_stats)
                self.assertNotIn(normalized_path, cache)

    def test_disabling_normal_or_automatic_library_retains_cached_ranking_state(self):
        for library_attribute in ("libraries", "auto_libraries"):
            for library_kind in ("file", "folder"):
                with self.subTest(
                    library_attribute=library_attribute,
                    library_kind=library_kind,
                ):
                    self._assert_disabled_library_retains_ranking_state(
                        library_attribute,
                        library_kind,
                    )

    def test_empty_cache_for_disabled_file_defers_destructive_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            library_path = os.path.join(temp_dir, "disabled.md")
            Path(library_path).write_text("- protected\n", encoding="utf-8")
            ranking_state = self._RankingState()
            protected_id = ranking_state.make_entry_id(library_path, "- protected")
            ranking_state.favorites[protected_id] = True
            settings = types.SimpleNamespace(
                libraries=[{"path": library_path, "enabled": False, "kind": "file"}],
                auto_libraries=[],
            )
            manager = word_manager_module.WordManager.__new__(word_manager_module.WordManager)
            manager.settings = settings
            manager.ranking_state = ranking_state
            manager._load_cache = lambda: {}
            manager._get_pinyin_sort_key = lambda parent: parent
            manager._save_cache = lambda: None
            manager.load_clipboard_history = lambda: None
            manager._expand_library_entries = lambda libraries: {}

            with mock.patch.object(
                word_manager_module,
                "normalize_library_path",
                side_effect=lambda path: os.path.normcase(os.path.abspath(path)),
                create=True,
            ):
                word_manager_module.WordManager.reload_all(manager)

            self.assertEqual(ranking_state.cleanup_calls, [])
            self.assertIn(protected_id, ranking_state.favorites)

    def test_corrupt_or_version_invalid_cache_for_disabled_folder_defers_cleanup(self):
        cache_payloads = ("not json", '{"version": "old", "files": {}}')
        for payload in cache_payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temp_dir:
                    folder = os.path.join(temp_dir, "disabled-folder")
                    os.mkdir(folder)
                    library_path = os.path.join(folder, "library.md")
                    Path(library_path).write_text("- protected\n", encoding="utf-8")
                    cache_path = os.path.join(temp_dir, "cache.json")
                    Path(cache_path).write_text(payload, encoding="utf-8")

                    ranking_state = self._RankingState()
                    protected_id = ranking_state.make_entry_id(library_path, "- protected")
                    ranking_state.usage_stats[protected_id] = {"count": 1}
                    manager = word_manager_module.WordManager.__new__(word_manager_module.WordManager)
                    manager.settings = types.SimpleNamespace(
                        libraries=[],
                        auto_libraries=[{"path": folder, "enabled": False, "kind": "folder"}],
                    )
                    manager.ranking_state = ranking_state
                    manager._get_pinyin_sort_key = lambda parent: parent
                    manager._save_cache = lambda: None
                    manager.load_clipboard_history = lambda: None
                    manager._expand_library_entries = lambda libraries: {}

                    normalize = lambda path: os.path.normcase(os.path.abspath(path))
                    with mock.patch.multiple(
                        word_manager_module,
                        CACHE_FILE=cache_path,
                        VERSION="current",
                        normalize_library_path=normalize,
                        list_eligible_md_files_with_status=lambda path: ([library_path], True),
                        create=True,
                    ):
                        word_manager_module.WordManager.reload_all(manager)

                    self.assertEqual(ranking_state.cleanup_calls, [])
                    self.assertIn(protected_id, ranking_state.usage_stats)

    def test_disabled_folder_enumeration_failure_defers_cleanup_for_normal_and_automatic_libraries(self):
        for library_attribute in ("libraries", "auto_libraries"):
            with self.subTest(library_attribute=library_attribute):
                with tempfile.TemporaryDirectory() as temp_dir:
                    folder = os.path.join(temp_dir, "disabled-folder")
                    os.mkdir(folder)
                    library_path = os.path.join(folder, "library.md")
                    Path(library_path).write_text("- protected\n", encoding="utf-8")
                    normalized_path = os.path.normcase(os.path.abspath(library_path))
                    block = {
                        "parent": "protected",
                        "full_content": "- protected",
                        "source_path": library_path,
                    }
                    cache = {normalized_path: {"hash": "revision", "data": [block]}}
                    ranking_state = self._RankingState()
                    protected_id = ranking_state.make_entry_id(library_path, block["full_content"])
                    ranking_state.favorites[protected_id] = True
                    settings = types.SimpleNamespace(libraries=[], auto_libraries=[])
                    setattr(
                        settings,
                        library_attribute,
                        [{"path": folder, "enabled": False, "kind": "folder"}],
                    )

                    manager = word_manager_module.WordManager.__new__(word_manager_module.WordManager)
                    manager.settings = settings
                    manager.ranking_state = ranking_state
                    def load_valid_cache():
                        manager._cache_load_succeeded = True
                        return cache
                    manager._load_cache = load_valid_cache
                    manager._get_file_hash = lambda path: "revision"
                    manager._get_pinyin_sort_key = lambda parent: parent
                    manager._save_cache = lambda: None
                    manager.load_clipboard_history = lambda: None
                    manager._expand_library_entries = lambda libraries: {}

                    with mock.patch.object(
                        word_manager_module,
                        "normalize_library_path",
                        side_effect=lambda path: os.path.normcase(os.path.abspath(path)),
                        create=True,
                    ), mock.patch.object(
                        word_manager_module,
                        "list_eligible_md_files_with_status",
                        return_value=([], False),
                        create=True,
                    ):
                        word_manager_module.WordManager.reload_all(manager)

                    self.assertEqual(ranking_state.cleanup_calls, [])
                    self.assertIn(protected_id, ranking_state.favorites)


if __name__ == "__main__":
    unittest.main()
