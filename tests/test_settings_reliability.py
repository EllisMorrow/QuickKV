import importlib
import json
import os
import stat
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
    """Undo temporary stub imports before discovery continues to another file."""
    for name in list(sys.modules):
        if name not in original_modules:
            del sys.modules[name]
    for name, module in original_modules.items():
        sys.modules[name] = module


def _load_settings_module():
    widgets = {name: _Dummy for name in (
        "QApplication QWidget QVBoxLayout QHBoxLayout QLineEdit QListWidget "
        "QListWidgetItem QSystemTrayIcon QMenu QSizeGrip QGraphicsDropShadowEffect "
        "QPushButton QInputDialog QMessageBox QStyledItemDelegate QStyle QFileDialog "
        "QCheckBox QWidgetAction QScrollArea QLabel QFrame"
    ).split()}
    core = {name: _Dummy for name in "Qt Signal Slot QObject QTimer QEvent QRect QProcess".split()}
    gui = {name: _Dummy for name in "QIcon QAction QCursor QPixmap QPainter QColor QPalette QActionGroup".split()}
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
        PASTE_MODE_CTRL_V="ctrl_v",
        PASTE_MODE_CTRL_SHIFT_V="ctrl_shift_v",
        PASTE_MODE_TYPING="typing",
        SUPPORTED_PASTE_MODES={"ctrl_v", "ctrl_shift_v", "typing"},
        WORD_FILE="",
        log=lambda message: None,
        normalize_library_path=lambda path: os.path.normcase(os.path.abspath(path)),
        is_eligible_library_file=lambda path: os.path.isfile(path) and path.lower().endswith(".md"),
    )
    sys.modules.pop("core.settings", None)
    return importlib.import_module("core.settings")


_modules_before_stub_loading = dict(sys.modules)
try:
    _settings_module = _load_settings_module()
finally:
    _restore_module_registry(_modules_before_stub_loading)
_stub_registry_restored = all(
    sys.modules.get(name) is module
    for name, module in _modules_before_stub_loading.items()
) and all(
    name in _modules_before_stub_loading
    for name in sys.modules
    if name.startswith(("PySide6", "pynput", "watchdog", "core.", "services.", "ui."))
)


class SettingsReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_module = _settings_module

    def settings_for(self, path):
        return self.settings_module.SettingsManager(str(path))

    def test_invalid_number_and_boolean_only_use_their_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            library_path = Path(temp_dir) / "library.md"
            library_path.write_text("# test", encoding="utf-8")
            config_path.write_text(
                "[Window]\nwidth = not-a-number\nheight = 320\n"
                "[General]\nhotkeys_enabled = definitely\nlibraries = "
                + json.dumps([{"path": str(library_path), "enabled": True}]) + "\n"
                "[Search]\nmulti_word_search = false\n",
                encoding="utf-8",
            )

            settings = self.settings_for(config_path)

            self.assertEqual(450, settings.width)
            self.assertEqual(320, settings.height)
            self.assertTrue(settings.hotkeys_enabled)
            self.assertFalse(settings.multi_word_search)
            self.assertEqual([{"path": str(library_path.resolve()), "enabled": True, "kind": "file"}], settings.libraries)

    def test_malformed_ini_leaves_a_fully_initialized_default_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text("this is not an ini file", encoding="utf-8")

            settings = self.settings_for(config_path)

            self.assertEqual(450, settings.width)
            self.assertTrue(settings.hotkeys_enabled)
            self.assertEqual([], settings.libraries)
            self.assertEqual([], settings.auto_libraries)
            self.assertEqual({"id": "", "version": ""}, settings.accepted_disclaimer_info)

    def test_invalid_libraries_json_falls_back_without_losing_other_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text("[General]\nlibraries = {not json}\n[Theme]\nmode = light\n", encoding="utf-8")

            settings = self.settings_for(config_path)

            self.assertEqual([], settings.libraries)
            self.assertEqual("light", settings.theme)

    def test_save_then_load_preserves_valid_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            library_path = Path(temp_dir) / "library.md"
            library_path.write_text("# test", encoding="utf-8")
            settings = self.settings_for(config_path)
            settings.width = 640
            settings.hotkeys_enabled = False
            settings.libraries = [{"path": str(library_path), "enabled": True, "kind": "file"}]

            self.assertTrue(settings.save())
            reloaded = self.settings_for(config_path)

            self.assertEqual(640, reloaded.width)
            self.assertFalse(reloaded.hotkeys_enabled)
            self.assertEqual([{"path": str(library_path.resolve()), "enabled": True, "kind": "file"}], reloaded.libraries)

    def test_save_preserves_existing_file_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text("[Window]\nwidth = 111\n", encoding="utf-8")
            os.chmod(config_path, 0o600)
            expected_mode = stat.S_IMODE(os.stat(config_path).st_mode)
            settings = self.settings_for(config_path)
            settings.width = 640

            self.assertTrue(settings.save())
            self.assertEqual(expected_mode, stat.S_IMODE(os.stat(config_path).st_mode))

    def test_replace_failure_preserves_original_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            original = "[Window]\nwidth = 111\n"
            config_path.write_text(original, encoding="utf-8")
            settings = self.settings_for(config_path)
            settings.width = 640

            with mock.patch.object(self.settings_module.os, "replace", side_effect=OSError("replace failed")):
                self.assertFalse(settings.save())

            self.assertEqual(original, config_path.read_text(encoding="utf-8"))
            self.assertEqual([], list(Path(temp_dir).glob(".config-*.tmp")))

    def test_replace_failure_keeps_candidate_out_of_memory_and_next_save_does_not_revive_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text("[Window]\nwidth = 111\n", encoding="utf-8")
            settings = self.settings_for(config_path)
            previous_config_width = settings.config.getint("Window", "width")
            settings.width = 640

            with mock.patch.object(self.settings_module.os, "replace", side_effect=OSError("replace failed")):
                self.assertFalse(settings.save())

            self.assertEqual(640, settings.width)
            self.assertEqual(previous_config_width, settings.config.getint("Window", "width"))
            self.assertEqual("[Window]\nwidth = 111\n", config_path.read_text(encoding="utf-8"))

            settings.width = 720
            self.assertTrue(settings.save())
            self.assertEqual(720, self.settings_for(config_path).width)

    def test_stub_loader_restores_real_qt_before_single_instance_import(self):
        self.assertTrue(_stub_registry_restored)
        previous = sys.modules.pop("core.single_instance", None)
        try:
            single_instance = importlib.import_module("core.single_instance")
            try:
                from PySide6.QtNetwork import QLocalSocket
            except ImportError:
                self.assertIsNone(single_instance.QLocalSocket)
            else:
                self.assertIs(single_instance.QLocalSocket, QLocalSocket)
        finally:
            sys.modules.pop("core.single_instance", None)
            if previous is not None:
                sys.modules["core.single_instance"] = previous


if __name__ == "__main__":
    unittest.main()
