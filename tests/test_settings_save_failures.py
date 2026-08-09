import importlib
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


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


def _restore_module_registry(original_modules):
    """Undo every temporary import so discovery order cannot change other tests."""
    for name in list(sys.modules):
        if name not in original_modules:
            del sys.modules[name]
    for name, module in original_modules.items():
        sys.modules[name] = module


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass


class _Signal:
    def connect(self, *args, **kwargs):
        pass

    def emit(self, *args, **kwargs):
        pass


class _Widget(_Dummy):
    def hideEvent(self, event):
        self.hide_event_called = True


class _MessageBox:
    messages = []

    @classmethod
    def reset(cls):
        cls.messages = []

    @classmethod
    def warning(cls, *args):
        cls.messages.append(("warning", args))

    @classmethod
    def information(cls, *args):
        cls.messages.append(("information", args))

    @classmethod
    def critical(cls, *args):
        cls.messages.append(("critical", args))


class _QTimer(_Dummy):
    scheduled = []

    @classmethod
    def singleShot(cls, *args):
        cls.scheduled.append(args)


def _install_stubs():
    widget_names = (
        "QApplication QVBoxLayout QHBoxLayout QLineEdit QListWidget QListWidgetItem "
        "QSystemTrayIcon QMenu QSizeGrip QGraphicsDropShadowEffect QPushButton "
        "QInputDialog QStyledItemDelegate QStyle QFileDialog QCheckBox QWidgetAction "
        "QScrollArea QLabel QFrame"
    ).split()
    widgets = {name: _Dummy for name in widget_names}
    widgets.update(QWidget=_Widget, QMessageBox=_MessageBox)
    widgets["QInputDialog"] = types.SimpleNamespace(getInt=lambda *args: (0, False))
    widgets["QDialog"] = type("QDialog", (_Dummy,), {"Accepted": 1})
    _module("PySide6")
    _module("PySide6.QtWidgets", **widgets)
    _module(
        "PySide6.QtCore",
        Qt=types.SimpleNamespace(),
        Signal=lambda *args, **kwargs: _Signal(),
        Slot=lambda *args, **kwargs: lambda function: function,
        QObject=_Dummy,
        QTimer=_QTimer,
        QEvent=_Dummy,
        QRect=_Dummy,
        QProcess=_Dummy,
    )
    _module("PySide6.QtGui", **{name: _Dummy for name in "QIcon QAction QCursor QPixmap QPainter QColor QPalette QActionGroup".split()})
    _module("PySide6.QtNetwork", QLocalServer=_Dummy, QLocalSocket=_Dummy)
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
        THEMES={"dark": {}},
        SUPPORTED_PASTE_MODES={"ctrl_v"},
        AUTO_LOAD_DIR="unused",
        normalize_library_path=lambda path: path,
    )
    _module("core.template_renderer", TemplateRenderer=_Dummy, TemplateRenderError=Exception)
    _module("ui.search_popup", SearchPopup=_Dummy)
    _module("ui.delegates", StyledItemDelegate=_Dummy)
    _module("ui.components", HotkeyDialog=_Dummy, DisclaimerDialog=_Dummy, ScrollableMessageBox=_Dummy, EditDialog=_Dummy, TemplateInputDialog=_Dummy, get_disclaimer_html_text=lambda: "")
    _module("services.hotkey_manager", NativeHotkeyManager=_Dummy)
    _module("services.shortcut_listener", ShortcutListener=_Dummy)
    _module("core.ranking_state", RankingStateManager=_Dummy)
    _module("core.settings", SettingsManager=_Dummy)
    _module("core.word_manager", WordManager=_Dummy)


def _load_settings_manager_for_controller_test():
    """Load the real SettingsManager while keeping this module's UI stubs isolated."""
    original_modules = dict(sys.modules)
    try:
        _install_stubs()
        config = sys.modules["core.config"]
        config.PASTE_MODE_CTRL_V = "ctrl_v"
        config.PASTE_MODE_CTRL_SHIFT_V = "ctrl_shift_v"
        config.PASTE_MODE_TYPING = "typing"
        config.SUPPORTED_PASTE_MODES = {"ctrl_v", "ctrl_shift_v", "typing"}
        config.WORD_FILE = ""
        config.is_eligible_library_file = lambda path: False
        sys.modules.pop("core.settings", None)
        return importlib.import_module("core.settings")
    finally:
        _restore_module_registry(original_modules)


_modules_before_stub_loading = dict(sys.modules)
try:
    _install_stubs()
    sys.modules.pop("main_controller", None)
    main_controller = importlib.import_module("main_controller")
    sys.modules.pop("ui.search_popup", None)
    search_popup = importlib.import_module("ui.search_popup")
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
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


class _Popup:
    def __init__(self):
        self.hidden = False
        self.theme_updates = 0

    def hide(self):
        self.hidden = True

    def apply_theme(self):
        self.theme_updates += 1

    def isVisible(self):
        return False


class _Listener:
    def __init__(self):
        self.stops = 0
        self.starts = 0

    def stop(self):
        self.stops += 1

    def start(self):
        self.starts += 1


class _HotkeyManager:
    def __init__(self):
        self.stops = 0
        self.unregisters = 0
        self.registrations = []

    def stop(self):
        self.stops += 1

    def unregister_all(self):
        self.unregisters += 1

    def reregister(self, hotkey):
        self.registrations.append(hotkey)
        return True, hotkey, ""


class _InstanceManager:
    def __init__(self):
        self.releases = 0

    def release(self):
        self.releases += 1


class _CleanupController:
    def __init__(self):
        self.cleanup_calls = 0

    def cleanup_and_exit(self):
        self.cleanup_calls += 1


class SettingsSaveFailureTests(unittest.TestCase):
    def setUp(self):
        _MessageBox.reset()
        _QTimer.scheduled = []

    def _controller(self, settings):
        controller = object.__new__(main_controller.MainController)
        controller.settings = settings
        controller.popup = _Popup()
        controller.hotkey_manager = _HotkeyManager()
        controller.shortcut_listener = _Listener()
        controller.instance_manager = _InstanceManager()
        controller.observer_restarts = 0
        controller.reloads = 0
        controller.rebuilds = 0
        controller.stop_file_observer = lambda: setattr(controller, "observer_restarts", controller.observer_restarts + 1)
        controller.restart_file_observer = lambda: setattr(controller, "observer_restarts", controller.observer_restarts + 1)
        controller.perform_full_reload = lambda: setattr(controller, "reloads", controller.reloads + 1)
        controller.rebuild_library_menu = lambda: setattr(controller, "rebuilds", controller.rebuilds + 1)
        return controller

    def test_restart_save_failure_keeps_current_process_and_services(self):
        settings = types.SimpleNamespace(save=lambda: False)
        controller = self._controller(settings)

        main_controller.MainController.perform_restart(controller)

        self.assertFalse(controller.popup.hidden)
        self.assertEqual(0, controller.hotkey_manager.stops)
        self.assertEqual(0, controller.shortcut_listener.stops)
        self.assertEqual(0, controller.observer_restarts)
        self.assertEqual(0, controller.instance_manager.releases)
        self.assertEqual([], _QTimer.scheduled)
        self.assertEqual("warning", _MessageBox.messages[0][0])

    def test_stub_loader_restores_the_module_registry_before_later_tests_load(self):
        self.assertTrue(_stub_registry_restored)

    def test_font_size_failure_restores_value_and_does_not_claim_success(self):
        settings = types.SimpleNamespace(font_size=14, save=lambda: False)
        controller = self._controller(settings)

        with mock.patch.object(main_controller.QInputDialog, "getInt", return_value=(20, True)):
            main_controller.MainController.set_font_size(controller)

        self.assertEqual(14, settings.font_size)
        self.assertEqual(0, controller.popup.theme_updates)
        self.assertEqual(["warning"], [kind for kind, _ in _MessageBox.messages])

    def test_hotkey_save_failure_restores_runtime_listener_state(self):
        settings = types.SimpleNamespace(hotkeys_enabled=False, hotkey="ctrl+space", save=lambda: False)
        controller = self._controller(settings)

        main_controller.MainController.toggle_hotkeys_enabled(controller)

        self.assertFalse(settings.hotkeys_enabled)
        self.assertEqual(["ctrl+space"], controller.hotkey_manager.registrations)
        self.assertEqual(1, controller.hotkey_manager.unregisters)

    def test_library_failure_does_not_reload_or_rebuild(self):
        libraries = [{"path": "library.md", "enabled": True, "kind": "file"}]
        settings = types.SimpleNamespace(libraries=libraries, save=lambda: False)
        controller = self._controller(settings)

        main_controller.MainController.remove_library(controller, "library.md")

        self.assertEqual(libraries, settings.libraries)
        self.assertEqual(0, controller.observer_restarts)
        self.assertEqual(0, controller.reloads)
        self.assertEqual(0, controller.rebuilds)

    def test_protocol_acceptance_failure_is_not_treated_as_accepted(self):
        settings = types.SimpleNamespace(
            accepted_disclaimer_info={"id": "old", "version": "old"},
            theme="dark",
            font_size=14,
            save=lambda: False,
        )
        dialog = types.SimpleNamespace(exec=lambda: 1)

        accepted = main.ensure_current_disclaimer_accepted(settings, "device", "version", lambda **kwargs: dialog)

        self.assertFalse(accepted)
        self.assertEqual({"id": "old", "version": "old"}, settings.accepted_disclaimer_info)
        self.assertEqual("critical", _MessageBox.messages[0][0])

    def test_startup_protocol_failure_uses_controller_cleanup(self):
        controller = _CleanupController()

        main.cleanup_after_startup_failure(controller)

        self.assertEqual(1, controller.cleanup_calls)

    def test_controller_rollback_keeps_real_settings_config_at_the_last_durable_value(self):
        settings_module = _load_settings_manager_for_controller_test()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text("[Window]\nwidth = 111\n", encoding="utf-8")
            settings = settings_module.SettingsManager(str(config_path))
            controller = self._controller(settings)

            with mock.patch.object(settings_module.os, "replace", side_effect=OSError("replace failed")):
                self.assertFalse(main_controller.MainController._update_settings(controller, width=640))

            self.assertEqual(111, settings.width)
            self.assertEqual(111, settings.config.getint("Window", "width"))
            self.assertEqual("[Window]\nwidth = 111\n", config_path.read_text(encoding="utf-8"))

            self.assertTrue(main_controller.MainController._update_settings(controller, width=720))
            self.assertEqual(720, settings_module.SettingsManager(str(config_path)).width)

    def test_background_window_size_failure_restores_state_and_closes_normally(self):
        popup = object.__new__(search_popup.SearchPopup)
        popup.settings = types.SimpleNamespace(width=450, height=300, save=lambda: False)
        popup.width = lambda: 640
        popup.height = lambda: 480

        popup.hideEvent(object())

        self.assertEqual((450, 300), (popup.settings.width, popup.settings.height))
        self.assertTrue(popup.hide_event_called)
        self.assertEqual([], _MessageBox.messages)


if __name__ == "__main__":
    unittest.main()
