# -*- coding: utf-8 -*-
import sys
import os
import webbrowser
import configparser
import hashlib
import json
import subprocess
import base64
import re
import itertools
import threading
import ctypes
import time
import copy
from ctypes import wintypes
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
                             QListWidget, QListWidgetItem, QSystemTrayIcon, QMenu, QSizeGrip,
                             QGraphicsDropShadowEffect, QPushButton,
                             QInputDialog, QMessageBox, QStyledItemDelegate, QStyle, QFileDialog,
                             QCheckBox, QWidgetAction, QScrollArea, QLabel, QFrame, QDialog)
from PySide6.QtCore import (Qt, Signal, Slot, QObject,
                          QTimer, QEvent, QRect, QProcess)
from PySide6.QtGui import QIcon, QAction, QCursor, QPixmap, QPainter, QColor, QPalette, QActionGroup
import pyperclip
from pypinyin import pinyin, Style
from pynput import keyboard
from fuzzywuzzy import fuzz
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 拼音库修正 ---
# 导入 pypinyin-dict 的高质量词典数据，以修正 pypinyin 默认词典中的罕见音问题
try:
    from pypinyin_dict.pinyin_data import kxhc1983
    kxhc1983.load()
    from pypinyin_dict.phrase_pinyin_data import cc_cedict
    cc_cedict.load()
    print("成功加载 pypinyin-dict 修正词典。")
except ImportError:
    print("警告: 未找到 pypinyin-dict 库，拼音首字母可能不准确。建议安装: pip install pypinyin-dict")


import builtins
# Dependency Injection
from core.config import *
from core.clipboard_timestamps import ClipboardTimestampStore, content_key, utf8_size_at_most
from core.transcription import transcribe, TranscriptionError
from core.template_renderer import TemplateRenderer, TemplateRenderError
from ui.search_popup import SearchPopup
from core.composition.mode import InputMode
from core.composition.store import CompositionStore, STORAGE_ERRORS
from ui.components import HotkeyDialog, DisclaimerDialog, ScrollableMessageBox, get_disclaimer_html_text, EditDialog, TemplateInputDialog
from services.hotkey_manager import NativeHotkeyManager
from services.shortcut_listener import ShortcutListener
from PySide6.QtNetwork import QLocalServer, QLocalSocket


# --- 文件监控处理器 (Watchdog) ---
class LibraryChangeHandler(FileSystemEventHandler):
    """使用 Watchdog 处理文件系统事件的处理器。"""
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        log("Watchdog 事件处理器已初始化。")

    def on_any_event(self, event):
        """
        捕获所有文件系统事件 (创建, 删除, 修改, 移动)。
        - 忽略目录事件。
        - 只关心 .md 文件的变化。
        - 触发带有防抖功能的重载调度器。
        """
        if event.is_directory:
            return

        # 不论是源路径还是目标路径（用于移动事件），只要是.md文件就触发
        if event.src_path.endswith('.md') or (hasattr(event, 'dest_path') and event.dest_path.endswith('.md')):
            log(f"Watchdog 检测到事件: {event.event_type} - {event.src_path}")
            # 【关键修复】通过发射信号来安全地通知主线程，而不是直接调用方法
            self.controller.thread_safe_reload_signal.emit()


# --- 主控制器 ---
class MainController(QObject):
    show_popup_signal = Signal()
    hide_popup_signal = Signal()
    # 新增：用于从 watchdog 线程安全地触发重载的信号
    thread_safe_reload_signal = Signal()

    MODIFIER_VKS = {
        'ctrl': 0x11,
        'shift': 0x10,
        'alt': 0x12,
        'lwin': 0x5B,
        'rwin': 0x5C,
    }

    def _update_settings(self, *, rollback_runtime=None, notify=True, **changes):
        """Persist a settings change or restore the complete affected state.

        External listeners are changed only after this succeeds where possible;
        callers that had to touch one first can supply its restoration callback.
        """
        previous = {
            name: copy.deepcopy(getattr(self.settings, name))
            for name in changes
        }
        for name, value in changes.items():
            setattr(self.settings, name, value)

        if self.settings.save():
            return True

        for name, value in previous.items():
            setattr(self.settings, name, value)
        if rollback_runtime:
            rollback_runtime()
        log("配置保存失败，已恢复本次设置变更。")
        if notify:
            QMessageBox.warning(self.popup, "保存失败", "设置无法保存，已恢复原来的设置。")
        return False

    def _save_settings(self, *, notify=True):
        """Save unchanged settings for actions such as restart."""
        return self._update_settings(notify=notify)

    def __init__(self, app, word_manager, settings_manager, ranking_state_manager=None, template_renderer=None, instance_manager=None, composition_store=None):
        super().__init__(); self.app = app; self.word_manager = word_manager; self.settings = settings_manager; self.ranking_state = ranking_state_manager; self.template_renderer = template_renderer or TemplateRenderer(); self.instance_manager = instance_manager; self.menu = None; self.auto_library_menu = None
        self._composition_store = composition_store
        self.popup = SearchPopup(self.word_manager, self.settings)
        self.popup.apply_global_font()
        composition = self.popup.composition
        composition.factory = self.composition_storage
        composition.expand_entry = self.expand_continuous_entry
        composition.template_expand = self.render_template_output
        composition.entry_inserted.connect(self.record_entry_usage)
        composition.copy_requested.connect(self.copy_composition)
        composition.submit_requested.connect(self.submit_composition)
        composition.external_reload_requested.connect(lambda: self.capture_composition(use_previous=True))
        composition.library_changed.connect(self.composition_library_changed)
        composition.set_output_transcription = lambda enabled: self._update_settings(transcription_enabled=enabled)
        self.word_manager.composition_writer = self.publish_composition_library
        self.popup.controller = self # 将 controller 实例传递给 popup
        self.show_popup_signal.connect(self.popup.show_and_focus)
        self.hide_popup_signal.connect(self.popup.hide)
        self.popup.suggestion_selected.connect(self.on_suggestion_selected)
        self.popup.continuous_editor.expand_entry = self.expand_continuous_entry
        self.popup.continuous_editor.entry_inserted.connect(self.record_entry_usage)
        self.popup.continuous_editor.submit_requested.connect(self.submit_continuous_draft)
        self.popup.continuous_editor.copy_requested.connect(self.copy_continuous_draft)

        self.user32 = ctypes.windll.user32
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetClipboardOwner.restype = wintypes.HWND
        self.user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        self.quickkv_pid = os.getpid()
        from services.composition_capture import CompositionCapture
        self.composition_capture = CompositionCapture(self)
        self.composition_capture.received.connect(self.open_composition_selection)
        self.composition_capture.failed.connect(self.show_composition_capture_error)
        self.composition_hotkey = None
        self.last_popup_target_hwnd = 0
        self.hotkey_manager = NativeHotkeyManager(self.settings.hotkey)
        self.hotkey_manager.suppress_string_trigger = self.is_draft_foreground
        self.hotkey_manager.hotkey_triggered.connect(self.on_hotkey_triggered)
        self.hotkey_manager.hotkey_registration_failed.connect(self.on_hotkey_registration_failed)
        if self.settings.hotkeys_enabled:
            self.hotkey_manager.start()
        self.configure_composition_hotkey(getattr(self.settings, "composition_hotkey", ""))
            
        # 初始化连续字符串盲打触发器
        self.hotkey_manager.update_string_trigger(
            self.settings.string_trigger_enabled, 
            self.settings.string_trigger_str
        )

        # 新增：初始化快捷码监听器
        self.shortcut_listener = ShortcutListener(self.word_manager)
        self.shortcut_listener.suppress_shortcuts = self.is_draft_foreground
        self.shortcut_listener.shortcut_matched.connect(self.on_shortcut_matched)
        # 快捷码监听在启动声明确认后由入口开启。

        # --- 新的 Watchdog 文件监控系统 ---
        self.full_reload_timer = QTimer(self)
        self.full_reload_timer.setSingleShot(True)
        self.full_reload_timer.setInterval(500) # 500ms 防抖
        self.full_reload_timer.timeout.connect(self.perform_full_reload)

        # 【关键修复】连接线程安全信号到实际的调度槽
        self.thread_safe_reload_signal.connect(self.schedule_full_reload)

        self.observer = None
        self.start_file_observer()

        # 新增：初始化自动重启定时器
        self.auto_restart_timer = QTimer(self)
        self.auto_restart_timer.timeout.connect(self._perform_auto_restart)
        self.update_auto_restart_timer()

        # 初始化剪贴板定时清除
        self.clipboard_timestamp_store = ClipboardTimestampStore(CLIPBOARD_TIMESTAMP_FILE, log)
        self.clipboard_timestamps = {}
        self.sync_clipboard_timestamps()
        
        self.clipboard_clear_timer = QTimer(self)
        self.clipboard_clear_timer.timeout.connect(self.check_clipboard_auto_clear)
        self.clipboard_clear_timer.start(60000) # 每分钟检查一次

        self.ignore_next_clipboard_change = False # 用于防止记录自己的输出
        self.app.clipboard().dataChanged.connect(self.on_clipboard_changed)
        self.pending_paste_request = None
        self.popup_paste_grace_ms = 50
        self.shortcut_paste_grace_ms = 50
        self.shortcut_erase_grace_ms = 25
        self.paste_poll_interval_ms = 20
        self.paste_timeout_ms = 650
        self.pending_paste_timer = QTimer(self)
        self.pending_paste_timer.setInterval(self.paste_poll_interval_ms)
        self.pending_paste_timer.timeout.connect(self._poll_pending_paste)

    @Slot()
    def on_clipboard_changed(self):
        """处理剪贴板数据变化信号（事件驱动）"""
        if not self.settings.clipboard_memory_enabled or (
            getattr(self, "composition_capture", None) and self.composition_capture.request
        ):
            return

        # 检查剪贴板内容是否是文本
        clipboard = self.app.clipboard()
        if not clipboard.mimeData().hasText():
            return

        current_text = clipboard.text()
        
        # 防止程序自己触发的复制操作被重复记录
        if self.ignore_next_clipboard_change:
            log("忽略本次剪贴板变化（由程序自身触发）。")
            self.ignore_next_clipboard_change = False
            return

        # 避免空内容和重复内容
        if not current_text or current_text == getattr(self, "_last_clipboard_text", ""):
            return

        text_size, is_too_large = utf8_size_at_most(current_text, CLIPBOARD_TEXT_MAX_BYTES)
        if is_too_large:
            log(
                "跳过过大的剪贴板文本: "
                f"UTF-8 字节数超过 {CLIPBOARD_TEXT_MAX_BYTES}（已检测 {text_size}）"
            )
            return

        # --- 核心逻辑 ---
        self._last_clipboard_text = current_text
        # 换行符规范化
        normalized_text = '\n'.join(current_text.splitlines())
        log(f"检测到新的剪贴板内容（事件驱动，UTF-8 字节数: {text_size}）")
        
        was_added = self.word_manager.add_to_clipboard_history(normalized_text)
        
        if was_added:
            self.sync_clipboard_timestamps(current_time=time.time())
            if self.popup.isVisible():
                self.popup.update_list(self.popup.search_box.text())

    def sync_clipboard_timestamps(self, current_time=None):
        """将时间戳与当前剪贴板历史的完整块内容对齐。"""
        if current_time is None:
            current_time = time.time()

        self.clipboard_timestamps = self.clipboard_timestamp_store.sync(
            (block['full_content'] for block in self.word_manager.clipboard_history),
            current_time,
        )

    def _start_detached_process(self, program, arguments):
        """兼容 PySide6 不同返回值形态的 detached 启动封装。"""
        result = QProcess.startDetached(program, arguments)
        if isinstance(result, tuple):
            success, pid = result
        else:
            success, pid = bool(result), None
        return bool(success), pid

    def _get_foreground_hwnd(self):
        try:
            return int(self.user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    def _get_window_process_id(self, hwnd):
        if not hwnd:
            return 0

        pid = wintypes.DWORD()
        try:
            self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        except Exception:
            return 0
        return int(pid.value)

    def _capture_target_hwnd(self):
        hwnd = self._get_foreground_hwnd()
        log(f"捕获目标窗口 hwnd={hwnd}")
        return hwnd

    def _is_quickkv_foreground(self, hwnd):
        return bool(hwnd) and self._get_window_process_id(hwnd) == self.quickkv_pid

    def is_draft_foreground(self, hwnd=None):
        # The native listeners call this off the GUI thread. Only read a Python flag.
        return self.popup.draft_input_visible and self._is_quickkv_foreground(
            self._get_foreground_hwnd() if hwnd is None else hwnd
        )

    def _get_pressed_modifiers(self):
        pressed = []
        for name, vk in self.MODIFIER_VKS.items():
            try:
                if self.user32.GetAsyncKeyState(vk) & 0x8000:
                    pressed.append(name)
            except Exception:
                continue
        return pressed

    def schedule_paste(self, target_hwnd, origin="popup", grace_ms=None, *, completed=None, clipboard_sequence=None):
        if self.pending_paste_request:
            return False
        hwnd = int(target_hwnd or 0)
        pid = self._get_window_process_id(hwnd)
        sequence = self.user32.GetClipboardSequenceNumber()
        if (not pid or pid == self.quickkv_pid or not sequence
                or clipboard_sequence is not None and sequence != clipboard_sequence):
            return False
        window_class = ctypes.create_unicode_buffer(64)
        self.user32.GetClassNameW(wintypes.HWND(hwnd), window_class, len(window_class))
        if window_class.value in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
            return False
        if grace_ms is None:
            grace_ms = self.popup_paste_grace_ms if origin == "popup" else self.shortcut_paste_grace_ms
        self.pending_paste_request = {
            "hwnd": hwnd, "pid": pid, "sequence": sequence, "mode": self.settings.paste_mode,
            "started": time.monotonic(), "grace_ms": grace_ms, "stable_hits": 0,
            "process": None, "completed": completed,
        }
        self.pending_paste_timer.start()
        return True

    def _finish_pending_paste(self, sent):
        request, self.pending_paste_request = self.pending_paste_request, None
        self.pending_paste_timer.stop()
        if request and request["completed"]:
            request["completed"](sent, self.user32.GetClipboardSequenceNumber() == request["sequence"])

    def _cancel_pending_paste(self):
        request = self.pending_paste_request
        if not request:
            return
        process = request["process"]
        if process and process.poll() is None:
            process.kill()
        self._finish_pending_paste(False)

    def _poll_pending_paste(self):
        request = self.pending_paste_request
        if not request:
            self.pending_paste_timer.stop()
            return
        elapsed = (time.monotonic() - request["started"]) * 1000
        process = request["process"]
        if process:
            result = process.poll()
            if result is not None:
                self._finish_pending_paste(result == 0)
            elif elapsed >= 5000:
                self._cancel_pending_paste()
            return

        hwnd = request["hwnd"]
        if (elapsed >= self.paste_timeout_ms or self._get_window_process_id(hwnd) != request["pid"]
                or self.user32.GetClipboardSequenceNumber() != request["sequence"]):
            self._finish_pending_paste(False)
            return
        if self._get_foreground_hwnd() != hwnd or self._get_pressed_modifiers():
            request["stable_hits"] = 0
            return
        request["stable_hits"] += 1
        if elapsed < request["grace_ms"] or request["stable_hits"] < 2:
            return
        request["process"] = self.perform_paste_now(request)
        if request["process"] is None:
            self._finish_pending_paste(False)
        else:
            request["started"] = time.monotonic()

    @Slot(int)
    def on_hotkey_triggered(self, trigger_id=0):
        # 这个信号现在是从 NativeHotkeyManager 线程发出的
        if not self.settings.hotkeys_enabled: return
        if trigger_id == 9999 and self.is_draft_foreground():
            return
        if self.popup.isVisible():
            log("热键触发：关闭窗口。"); self.hide_popup_signal.emit()
        else:
            self.last_popup_target_hwnd = self._capture_target_hwnd()
            log(f"热键触发：打开窗口。目标 hwnd={self.last_popup_target_hwnd}")
            self.show_popup_signal.emit()

    def activate_from_second_instance(self):
        """Bring the already-owned search window to the foreground via local IPC."""
        log("收到第二实例的激活请求。")
        if not self.popup.isVisible():
            self.last_popup_target_hwnd = self._capture_target_hwnd()
        self.show_popup_signal.emit()

    @Slot(str)
    def on_hotkey_registration_failed(self, message):
        """统一处理系统级热键注册失败，避免 UI 与配置状态脱节。"""
        old_hotkey = self.settings.hotkey
        was_enabled = self.settings.hotkeys_enabled
        self.hotkey_manager.stop_hotkey()
        if was_enabled:
            self._update_settings(
                hotkeys_enabled=False,
                notify=False,
                rollback_runtime=lambda: self.hotkey_manager.reregister(old_hotkey),
            )
        if hasattr(self, 'toggle_hotkeys_action'):
            self.toggle_hotkeys_action.setChecked(self.settings.hotkeys_enabled)
        log(f"CRITICAL: {message}")
        QMessageBox.warning(self.popup, "热键注册失败", message)

    def start_file_observer(self):
        """启动 Watchdog 文件监控线程"""
        if self.observer and self.observer.is_alive():
            log("Watchdog 监控已在运行。")
            return

        self.observer = Observer()
        event_handler = LibraryChangeHandler(self)

        # 监控所有手动添加的词库所在的目录，以及自动加载目录
        watched_dirs = set()
        # 1. 添加自动加载目录
        if os.path.isdir(AUTO_LOAD_DIR):
            watched_dirs.add(AUTO_LOAD_DIR)

        # 2. 添加所有手动词库的父目录
        for lib in self.settings.libraries:
            if lib.get('kind', 'file') == 'folder':
                dir_path = lib.get('path')
            else:
                dir_path = os.path.dirname(lib['path'])
            # 检查目录是否存在且未被添加过
            if os.path.isdir(dir_path):
                watched_dirs.add(dir_path)

        if not watched_dirs:
            log("没有找到有效的词库目录来监控。")
            return

        for path in watched_dirs:
            try:
                self.observer.schedule(event_handler, path, recursive=False) # 非递归，只监控指定目录
                log(f"Watchdog 正在监控目录: {path}")
            except Exception as e:
                log(f"CRITICAL: Watchdog 监控目录 {path} 失败: {e}")

        self.observer.start()
        log("Watchdog 监控线程已启动。")

    def stop_file_observer(self):
        """停止 Watchdog 文件监控线程"""
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=1.5)
            log("Watchdog 监控线程已停止。")
        self.observer = None

    @Slot()
    def schedule_full_reload(self):
        """（防抖）安排一个完整的词库扫描和重载"""
        log("检测到词库相关变化，安排全量重载...")
        self.full_reload_timer.start()

    @Slot()
    def perform_full_reload(self):
        """
        执行完整的词库重新加载流程。
        1. 重新扫描自动加载目录以发现新/删除的文件。
        2. 重新加载所有词库数据（利用缓存）。
        3. 更新快捷码。
        4. 如果UI可见，刷新列表。
        """
        log("--- 开始执行全量重载 ---")
        # 重新扫描自动加载目录，如果发生变化，则重启监视器
        if self.scan_and_update_auto_libraries():
             self.stop_file_observer()
             self.start_file_observer()

        composition_store = getattr(self, "_composition_store", None)
        if composition_store is not None:
            try:
                workspace = self.popup.composition
                if workspace.store is composition_store:
                    self.word_manager.composition_blocked = not workspace.reconcile_library()
                else:
                    composition_store.recover_library()
                    self.word_manager.composition_blocked = False
            except STORAGE_ERRORS:
                self.word_manager.composition_blocked = True
        self.word_manager.reload_all() # 核心：加载所有词库
        self.sync_clipboard_timestamps()
        if self.shortcut_listener and self.settings.shortcut_code_enabled:
            self.shortcut_listener.update_shortcuts()
        if self.popup.isVisible():
            self.popup.update_list(self.popup.search_box.text())
        # 重新构建菜单（特别是自动加载菜单）以反映变化
        self.rebuild_auto_library_menu()
        log("--- 全量重载完成 ---")
    def _find_block_by_full_content(self, text):
        search_pool = self.word_manager.clipboard_history + self.word_manager.word_blocks
        for block in search_pool:
            if block['full_content'] == text:
                return block
        return None

    def _build_output_content(self, selected_text, found_block):
        if not found_block:
            return selected_text.replace('- ', '', 1)

        if found_block['exclude_parent']:
            return '\n'.join(found_block['raw_lines'][1:])

        first_line = found_block['parent']
        return '\n'.join([first_line] + found_block['raw_lines'][1:])

    def _transcribe_output(self, text):
        if not getattr(self.settings, 'transcription_enabled', True):
            return text
        try:
            return transcribe(text, getattr(self.settings, 'transcription_rules', ''))
        except TranscriptionError as error:
            QMessageBox.warning(self.popup, "转写规则错误", f"{error}\n请在转写功能中修正规则，本次输出已取消。")
            return None

    def expand_continuous_entry(self, block):
        return self.render_template_output(self._build_output_content(block['full_content'], block))

    def composition_storage(self):
        if self._composition_store is None:
            self._composition_store = CompositionStore(COMPOSITION_DB, COMPOSITION_LIBRARY)
        return self._composition_store

    def publish_composition_library(self, before, after):
        try:
            store = self.composition_storage()
            store.recover_library()
            store.publish_library(before, after, {})
            return True
        except STORAGE_ERRORS:
            self.word_manager.composition_blocked = True
            self.schedule_full_reload()
            return False

    def composition_library_changed(self):
        self.schedule_full_reload()

    def copy_composition(self, text):
        return self._copy_editor_output(text, self.popup.composition.report)

    def submit_composition(self, text):
        self._submit_editor_output(text, composition=True)

    def capture_composition(self, use_previous=False):
        hwnd = self.last_popup_target_hwnd if use_previous else self._get_foreground_hwnd()
        if not self.popup.composition.flush():
            return
        if self.pending_paste_request:
            self.show_composition_capture_error("请等待当前落字完成。")
            return
        self.composition_capture.begin(hwnd)

    def open_composition_selection(self, text):
        self.popup.set_input_mode(InputMode.COMPOSITION)
        self.popup.show_and_focus()
        self.popup.composition.open_reload(text)

    def show_composition_capture_error(self, message):
        self.popup.set_input_mode(InputMode.COMPOSITION)
        self.popup.show_and_focus()
        self.popup.composition.report(message, persistent=True)

    def configure_composition_hotkey(self, shortcut):
        if self.composition_hotkey:
            self.composition_hotkey.stop()
            self.composition_hotkey = None
        if not shortcut:
            return True
        manager = NativeHotkeyManager(shortcut)
        valid = manager.validate_hotkey(shortcut)
        if valid["normalized"] == self.hotkey_manager.hotkey_str:
            return False
        registered, _ = manager._preflight_hotkey_registration(valid)
        if not registered:
            return False
        manager.hotkey_triggered.connect(lambda _: self.capture_composition())
        manager.hotkey_registration_failed.connect(lambda _: self.show_composition_capture_error("无法注册回载快捷键。"))
        self.composition_hotkey = manager
        if self.settings.hotkeys_enabled:
            manager.start()
        return True

    def set_composition_hotkey(self):
        current = self.settings.composition_hotkey
        shortcut, accepted = QInputDialog.getText(self.popup, "回载快捷键", "例如 Ctrl+Alt+R，留空关闭",
                                                  text=current)
        if not accepted:
            return
        shortcut = shortcut.strip().lower()
        if shortcut == current:
            return
        if not self.configure_composition_hotkey(shortcut):
            self.configure_composition_hotkey(current)
            QMessageBox.warning(self.popup, "快捷键不可用", "请输入有效且尚未占用的系统快捷键。")
            return
        if not self._update_settings(composition_hotkey=shortcut):
            self.configure_composition_hotkey(current)

    def _prepare_editor_output(self, text, feedback):
        if not text:
            feedback("没有可输出的正文")
            return None
        try:
            output = (transcribe(text, self.settings.transcription_rules)
                      if self.settings.transcription_enabled else text)
        except TranscriptionError:
            feedback("转写规则有误，本次输出已取消")
            return None
        if not output:
            feedback("转写后的正文为空")
            return None
        return output

    def _copy_editor_output(self, text, feedback):
        output = self._prepare_editor_output(text, feedback)
        if output is None:
            return False
        copied = self._copy_continuous_output(output)
        feedback("复制成功" if copied else "复制失败：无法写入剪贴板")
        return copied

    def _copy_continuous_output(self, text):
        previous_ignore_state = self.ignore_next_clipboard_change
        self.ignore_next_clipboard_change = self.settings.clipboard_memory_enabled
        try:
            pyperclip.copy(text)
        except pyperclip.PyperclipException:
            self.ignore_next_clipboard_change = previous_ignore_state
            log("草稿的剪贴板写入失败。")
            return False
        return True

    @Slot(str)
    def copy_continuous_draft(self, text):
        return self._copy_editor_output(text, self.popup.continuous_editor.report_output)

    @Slot(str)
    def submit_continuous_draft(self, text):
        self._submit_editor_output(text, composition=False)

    def _submit_editor_output(self, text, *, composition):
        editor = self.popup.continuous_editor
        workspace = self.popup.composition
        feedback = workspace.report if composition else editor.report_output
        if self.pending_paste_request or self.composition_capture.request:
            feedback("另一个输出操作正在进行")
            return
        output = self._prepare_editor_output(text, feedback)
        if output is None:
            return
        if not self._copy_continuous_output(output):
            feedback("无法落字：写入剪贴板失败")
            return
        if self.settings.paste_mode == PASTE_MODE_TYPING and any(char in output for char in "\r\n\t"):
            feedback("逐字输入不支持换行或 Tab，已复制")
            return

        def finished(sent, clipboard_preserved):
            if not composition:
                editor.set_submission_pending(False)
            if sent:
                if not composition:
                    editor.complete_submission(text)
                feedback("已发送落字")
            else:
                if composition:
                    self.popup.reappear_in_place()
                else:
                    self.popup.resume_continuous_after_paste_failure()
                feedback("未能落字，已复制" if clipboard_preserved else "未能落字，剪贴板已变化")

        sequence = self._verify_paste_clipboard(output)
        if not sequence:
            feedback("未能落字，剪贴板已变化")
            return
        foreground = self._get_foreground_hwnd()
        hwnd = self.last_popup_target_hwnd if self._is_quickkv_foreground(foreground) else foreground
        if not self.schedule_paste(hwnd, completed=finished, clipboard_sequence=sequence):
            feedback("未能落字，已复制")
            return
        if not composition:
            editor.set_submission_pending(True)
        self.popup.hide()
        if self._is_quickkv_foreground(self._get_foreground_hwnd()):
            self.user32.SetForegroundWindow(wintypes.HWND(hwnd))

    @Slot(str)
    def on_suggestion_selected(self, text, target_hwnd=None, origin='popup'):
        if getattr(self, "pending_paste_request", None):
            return
        found_block = self._find_block_by_full_content(text)
        if found_block and found_block.get('is_clipboard'):
            source_type = 'clipboard'
        elif found_block:
            source_type = 'library'
        else:
            source_type = 'unresolved'
        log(f"已选择词条块: source={source_type} utf8_bytes={len(text.encode('utf-8'))}")
        content_to_paste = self._build_output_content(text, found_block)

        rendered_content = self.render_template_output(content_to_paste)
        if rendered_content is None:
            log("模板输出已取消或失败，本次不执行粘贴。")
            return

        content_to_paste = self._transcribe_output(rendered_content)
        if content_to_paste is None:
            return
        self.record_entry_usage(found_block)

        previous_ignore = self.ignore_next_clipboard_change
        self.ignore_next_clipboard_change = True
        try:
            pyperclip.copy(content_to_paste)
        except pyperclip.PyperclipException:
            self.ignore_next_clipboard_change = previous_ignore
            log("无法写入普通搜索的落字内容。")
            return
        sequence = self._verify_paste_clipboard(content_to_paste)
        if sequence:
            resolved_target_hwnd = int(target_hwnd or self.last_popup_target_hwnd or 0)
            self.schedule_paste(resolved_target_hwnd, origin=origin, clipboard_sequence=sequence)

    def _verify_paste_clipboard(self, expected):
        sequence = self.user32.GetClipboardSequenceNumber()
        try:
            matches = pyperclip.paste() == expected
        except pyperclip.PyperclipException:
            return 0
        return sequence if matches and self.user32.GetClipboardSequenceNumber() == sequence else 0

    def perform_paste(self):
        self.schedule_paste(self._get_foreground_hwnd(), origin='manual')

    @staticmethod
    def _paste_commands(mode):
        if mode == PASTE_MODE_CTRL_V:
            return "", "if (-not [QuickKV.PasteGuard]::Paste($false)) { exit 3 }"
        if mode == PASTE_MODE_CTRL_SHIFT_V:
            return "", "if (-not [QuickKV.PasteGuard]::Paste($true)) { exit 3 }"
        if mode == PASTE_MODE_TYPING:
            return "$clipboardText = Get-Clipboard -Raw", (
                "if (-not [QuickKV.PasteGuard]::TypeText($clipboardText)) { exit 3 }"
            )
        return "", ""

    def perform_paste_now(self, request):
        setup, send = self._paste_commands(request["mode"])
        if not send:
            return None
        hwnd, pid, sequence = (request[key] for key in ("hwnd", "pid", "sequence"))
        script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -Name PasteGuard -Namespace QuickKV -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
[DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
[DllImport("user32.dll")] public static extern short GetAsyncKeyState(int key);
[StructLayout(LayoutKind.Sequential)]
public struct Key {{ public ushort vk, scan; public uint flags, time; public UIntPtr extra; }}
[StructLayout(LayoutKind.Sequential)]
public struct Mouse {{ public int x, y; public uint data, flags, time; public UIntPtr extra; }}
[StructLayout(LayoutKind.Explicit)]
public struct Data {{
    [FieldOffset(0)] public Key key;
    [FieldOffset(0)] public Mouse mouse;
}}
[StructLayout(LayoutKind.Sequential)]
public struct Input {{ public uint type; public Data data; }}
[DllImport("user32.dll", SetLastError=true)] public static extern uint SendInput(uint count, Input[] input, int size);
static Input KeyEvent(ushort vk, ushort scan = 0, uint flags = 0) {{
    return new Input {{ type = 1, data = new Data {{ key = new Key {{ vk = vk, scan = scan, flags = flags }} }} }};
}}
static bool Dispatch(Input[] events) {{
    return events.Length > 0 && SendInput((uint)events.Length, events, Marshal.SizeOf(typeof(Input))) == events.Length;
}}
public static bool Paste(bool shift) {{
    if (shift)
        return Dispatch(new[] {{ KeyEvent(17), KeyEvent(16), KeyEvent(86), KeyEvent(86, 0, 2),
                               KeyEvent(16, 0, 2), KeyEvent(17, 0, 2) }});
    return Dispatch(new[] {{ KeyEvent(17), KeyEvent(86), KeyEvent(86, 0, 2), KeyEvent(17, 0, 2) }});
}}
public static bool TypeText(string text) {{
    if (String.IsNullOrEmpty(text) || text.IndexOfAny(new[] {{ (char)10, (char)13, (char)9 }}) != -1)
        return false;
    var events = new Input[text.Length * 2];
    for (int i = 0; i < text.Length; i++) {{
        events[2*i] = KeyEvent(0, text[i], 4);
        events[2*i+1] = KeyEvent(0, text[i], 6);
    }}
    return Dispatch(events);
}}
'@
{setup}
foreach ($key in 0x11, 0x10, 0x12, 0x5B, 0x5C) {{
    if ([QuickKV.PasteGuard]::GetAsyncKeyState($key) -band 0x8000) {{ exit 2 }}
}}
$foreground = [QuickKV.PasteGuard]::GetForegroundWindow()
[uint32]$processId = 0
[void][QuickKV.PasteGuard]::GetWindowThreadProcessId($foreground, [ref]$processId)
if ($foreground.ToInt64() -ne {hwnd} -or $processId -ne {pid} -or
    [QuickKV.PasteGuard]::GetClipboardSequenceNumber() -ne {sequence}) {{ exit 2 }}
{send}
"""
        command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            return subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            log("无法启动落字进程。")
            return None

    def record_entry_usage(self, block):
        """仅记录真正完成输出的普通词条使用行为。"""
        if not block or block.get('is_clipboard'):
            return
        if not self.ranking_state:
            return

        entry_id = block.get('entry_id')
        if not entry_id:
            return

        self.ranking_state.record_use(entry_id)
        self.word_manager.refresh_ranking_metadata()

    def toggle_block_favorite(self, block):
        """切换普通词条的收藏状态，并刷新当前结果。"""
        if not block or block.get('is_clipboard'):
            return
        if not self.ranking_state:
            return

        entry_id = block.get('entry_id')
        if not entry_id:
            return

        new_state = self.ranking_state.toggle_favorite(entry_id)
        self.word_manager.refresh_ranking_metadata()
        state_text = "已收藏" if new_state else "已取消收藏"
        log(f"{state_text}: {block.get('parent', '')}")
        if self.popup.isVisible():
            self.popup.update_list(self.popup.search_box.text())

    def show_transcription_settings(self):
        from ui.transcription_dialog import TranscriptionDialog
        dialog = TranscriptionDialog(
            self.popup, self.settings, THEMES[self.settings.theme],
            lambda enabled, rules: self._update_settings(
                transcription_enabled=enabled, transcription_rules=rules),
        )
        dialog.exec()

    def render_template_output(self, content_to_paste):
        """在最终输出前执行中文模板渲染。"""
        if not self.template_renderer or not self.template_renderer.contains_template(content_to_paste):
            return content_to_paste

        try:
            prepared_template = self.template_renderer.prepare(content_to_paste)
        except TemplateRenderError as e:
            QMessageBox.warning(self.popup, "模板错误", str(e))
            return None

        input_values = {}
        if prepared_template.input_fields:
            dialog = TemplateInputDialog(
                self.popup,
                prepared_template.input_fields,
                THEMES[self.settings.theme],
                self.settings.font_size
            )
            if dialog.exec() != QDialog.Accepted:
                return None
            input_values = dialog.get_values()

        clipboard_text = ""
        if prepared_template.uses_clipboard:
            clipboard = self.app.clipboard()
            clipboard_text = clipboard.text() if clipboard.mimeData().hasText() else ""

        try:
            return self.template_renderer.render(
                prepared_template,
                input_values=input_values,
                clipboard_text=clipboard_text,
            )
        except TemplateRenderError as e:
            QMessageBox.warning(self.popup, "模板错误", str(e))
            return None

    @Slot(str, str)
    def add_entry(self, text, target_path=None):
        # 如果没有指定目标词库，则弹出选择框
        if target_path is None:
            writable_targets = self.get_writable_library_targets()
            if len(writable_targets) > 1:
                target_labels = [target['label'] for target in writable_targets]
                selected_label, ok = QInputDialog.getItem(self.popup, "选择词库", "请选择要添加到的词库:", target_labels, 0, False)
                if ok and selected_label:
                    target_path = next((target['path'] for target in writable_targets if target['label'] == selected_label), None)
                else:
                    return # 用户取消
            elif len(writable_targets) == 1:
                target_path = writable_targets[0]['path']
            else:
                QMessageBox.warning(self.popup, "错误", "没有可用的词库。请先添加一个。")
                return

        source = self.word_manager.get_source_by_path(target_path)
        if source:
            content = f"- {text}"
            if source.add_entry(content):
                self.schedule_full_reload()
                self.popup.search_box.clear()
            else:
                QMessageBox.warning(self.popup, "错误", f"向 {os.path.basename(target_path)} 添加词条失败！")
    
    @Slot(object)
    def edit_entry(self, selected_block):
        if not selected_block or not selected_block.get('full_content') or not selected_block.get('source_path'):
            QMessageBox.warning(self.popup, "错误", "找不到要编辑的词条。")
            return

        original_content = selected_block.get('full_content')
        source_path = selected_block.get('source_path')
        block_index = selected_block.get('block_index')
        source_revision = selected_block.get('source_revision')
        
        source = self.word_manager.get_source_by_path(source_path)

        if not source:
            QMessageBox.warning(self.popup, "错误", "找不到词条的来源文件对象。")
            return

        dialog = EditDialog(self.popup, original_content, THEMES[self.settings.theme], self.settings.font_size)
        if dialog.exec():
            new_content = dialog.get_text()
            if source.update_entry(original_content, new_content, block_index, source_revision):
                # 统一调用全量重载，它会处理缓存、快捷码和UI刷新
                self.schedule_full_reload()
                
                if self.popup.isVisible():
                    self.popup.update_list(self.popup.search_box.text())
            else:
                QMessageBox.warning(self.popup, "错误", f"更新 {os.path.basename(source.file_path)} 中的词条失败！")

    @Slot(object)
    def delete_entry(self, selected_block):
        if not selected_block or not selected_block.get('full_content') or not selected_block.get('source_path'):
            QMessageBox.warning(self.popup, "错误", "找不到要删除的词条。")
            return

        content = selected_block.get('full_content')
        source_path = selected_block.get('source_path')
        block_index = selected_block.get('block_index')
        source_revision = selected_block.get('source_revision')
 
        source = self.word_manager.get_source_by_path(source_path)

        if not source:
            QMessageBox.warning(self.popup, "错误", "找不到词条的来源文件对象。")
            return

        dialog = ScrollableMessageBox(
            parent=self.popup,
            title="确认删除",
            text=f"确定要从 <b>{os.path.basename(source.file_path)}</b> 中删除以下词条吗？<br><br>{content.replace(chr(10), '<br>')}",
            theme=THEMES[self.settings.theme],
            font_size=self.settings.font_size
        )
        
        if dialog.exec() == QDialog.Accepted:
            if source.delete_entry(content, block_index, source_revision):
                # 统一调用全量重载
                self.schedule_full_reload()
                
                if self.popup.isVisible():
                    self.popup.update_list(self.popup.search_box.text())
            else:
                QMessageBox.warning(self.popup, "错误", f"从 {os.path.basename(source.file_path)} 删除词条失败！")

    def move_clipboard_item_to_library(self, item_content, target_path):
        """将剪贴板条目移动到指定的词库"""
        # 1. 提取纯文本
        text_to_add = item_content.replace('- ', '', 1).strip()

        # 2. 添加到目标词库
        source = self.word_manager.get_source_by_path(target_path)
        if source and source.add_entry(f"- {text_to_add}"):
            log(f"已将 1 条剪贴板历史移至 {os.path.basename(target_path)}")
            self.schedule_full_reload() # 安排重载来更新所有状态

            # 3. 从剪贴板历史中删除
            if self.word_manager.clipboard_source.delete_entry(item_content):
                log("已从剪贴板历史中删除 1 条。")
                # 4. 刷新
                self.word_manager.load_clipboard_history()
                self.sync_clipboard_timestamps(current_time=time.time())
                if self.popup.isVisible():
                    self.popup.update_list(self.popup.search_box.text())
            else:
                log("从剪贴板历史删除条目失败。")
                QMessageBox.warning(self.popup, "警告", "条目已添加到新词库，但从剪贴板历史中删除失败。")
        else:
            QMessageBox.warning(self.popup, "错误", f"无法将条目添加到 {os.path.basename(target_path)}")

    def restart_file_observer(self):
        """在手动词库结构变化后重建目录监控。"""
        self.stop_file_observer()
        self.start_file_observer()

    def _manual_library_exists(self, path, kind):
        norm_path = normalize_library_path(path)
        for lib in self.settings.libraries:
            lib_kind = lib.get('kind', 'file')
            lib_path = lib.get('path')
            if lib_kind == kind and lib_path and normalize_library_path(lib_path) == norm_path:
                return True
        return False

    def get_writable_library_targets(self):
        """返回所有真实可写入的 md 文件目标。"""
        targets = []
        seen_paths = set()
        configured_libraries = self.settings.libraries + self.settings.auto_libraries

        for lib in configured_libraries:
            lib_path = lib.get('path')
            lib_kind = lib.get('kind', 'file')
            if lib_kind == 'folder':
                candidate_paths = list_eligible_md_files(lib_path)
            else:
                candidate_paths = [os.path.abspath(lib_path)] if is_eligible_library_file(lib_path) else []

            for candidate_path in candidate_paths:
                norm_path = normalize_library_path(candidate_path)
                if norm_path in seen_paths:
                    continue
                seen_paths.add(norm_path)
                parent_dir = os.path.dirname(candidate_path)
                targets.append({
                    'label': f"{os.path.basename(candidate_path)} [{parent_dir}]",
                    'path': candidate_path,
                })

        return sorted(targets, key=lambda item: item['label'].lower())

    @Slot()
    def add_library(self):
        file_path, _ = QFileDialog.getOpenFileName(self.popup, "选择一个词库文件", "", "Markdown 文件 (*.md)")
        if file_path:
            file_path = os.path.abspath(file_path)
            if not is_eligible_library_file(file_path):
                QMessageBox.warning(self.popup, "错误", "所选文件不是可用的 md 词库文件。")
                return

            if self._manual_library_exists(file_path, 'file'):
                QMessageBox.information(self.popup, "提示", "该词库已在列表中。")
                return
            
            if not self._update_settings(
                libraries=self.settings.libraries + [{"path": file_path, "enabled": True, "kind": "file"}]
            ):
                return
            self.restart_file_observer()
            self.perform_full_reload() # 立即执行重载，因为这是用户直接操作
            self.rebuild_library_menu()

    @Slot()
    def add_library_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self.popup, "选择一个词库文件夹")
        if folder_path:
            folder_path = os.path.abspath(folder_path)
            if self._manual_library_exists(folder_path, 'folder'):
                QMessageBox.information(self.popup, "提示", "该词库文件夹已在列表中。")
                return

            if not self._update_settings(
                libraries=self.settings.libraries + [{"path": folder_path, "enabled": True, "kind": "folder"}]
            ):
                return
            self.restart_file_observer()
            self.perform_full_reload()
            self.rebuild_library_menu()

            if not list_eligible_md_files(folder_path):
                QMessageBox.information(
                    self.popup,
                    "提示",
                    "该文件夹当前没有可加载的 .md 文件，已保留在配置中；后续新增 .md 文件会自动生效。"
                )

    @Slot(str)
    def remove_library(self, path):
        norm_path = normalize_library_path(path)
        libraries = [
            lib for lib in self.settings.libraries
            if not lib.get('path') or normalize_library_path(lib.get('path')) != norm_path
        ]
        if not self._update_settings(libraries=libraries):
            return
        self.restart_file_observer()
        self.perform_full_reload() # 立即执行重载
        self.rebuild_library_menu()

    @Slot(str)
    def toggle_library_enabled(self, path):
        norm_path = normalize_library_path(path)
        libraries = copy.deepcopy(self.settings.libraries)
        for lib in libraries:
            lib_path = lib.get('path')
            if lib_path and normalize_library_path(lib_path) == norm_path:
                lib['enabled'] = not lib.get('enabled', True)
                break
        if not self._update_settings(libraries=libraries):
            return
        self.restart_file_observer()
        self.perform_full_reload() # 立即执行重载
        self.rebuild_library_menu()

    @Slot(str)
    def toggle_auto_library_enabled(self, path):
        auto_libraries = copy.deepcopy(self.settings.auto_libraries)
        for lib in auto_libraries:
            if lib.get('path') == path:
                lib['enabled'] = not lib.get('enabled', True)
                break
        if not self._update_settings(auto_libraries=auto_libraries):
            return
        self.perform_full_reload() # 立即执行重载
        self.rebuild_auto_library_menu()

    def open_auto_load_dir(self):
        try:
            webbrowser.open(AUTO_LOAD_DIR)
            log(f"尝试打开自动加载文件夹: {AUTO_LOAD_DIR}")
        except Exception as e:
            log(f"打开自动加载文件夹失败: {e}")
            QMessageBox.warning(self.popup, "错误", f"无法打开文件夹路径：\n{AUTO_LOAD_DIR}\n\n错误: {e}")

    def rebuild_auto_library_menu(self):
        self.auto_library_menu.clear()
        
        open_dir_action = QAction("打开-md词库文件夹", self.auto_library_menu)
        open_dir_action.triggered.connect(self.open_auto_load_dir)
        self.auto_library_menu.addAction(open_dir_action)
        self.auto_library_menu.addSeparator()

        if not self.settings.auto_libraries:
            no_lib_action = QAction("无自动加载词库", self.auto_library_menu)
            no_lib_action.setEnabled(False)
            self.auto_library_menu.addAction(no_lib_action)
        else:
            for lib in self.settings.auto_libraries:
                lib_path = lib.get('path')
                lib_name = os.path.basename(lib_path)
                action = QAction(lib_name, self.auto_library_menu)
                action.setCheckable(True)
                action.setChecked(lib.get('enabled', True))
                action.triggered.connect(lambda _, p=lib_path: self.toggle_auto_library_enabled(p))
                self.auto_library_menu.addAction(action)

    @Slot()
    def schedule_auto_lib_scan(self):
        """安排一个延迟的自动目录扫描，以避免在文件写入完成前触发。"""
        log("检测到自动加载目录变化，安排扫描...")
        self.auto_scan_timer.start()

    def rebuild_library_menu(self):
        self.library_menu.clear()
        
        add_folder_action = QAction("添加MD词库文件夹", self.library_menu)
        add_folder_action.triggered.connect(self.add_library_folder)
        self.library_menu.addAction(add_folder_action)

        add_file_action = QAction("添加MD文件", self.library_menu)
        add_file_action.triggered.connect(self.add_library)
        self.library_menu.addAction(add_file_action)
        self.library_menu.addSeparator()

        for lib in self.settings.libraries:
            lib_path = lib.get('path')
            lib_kind = lib.get('kind', 'file')
            raw_name = os.path.basename(lib_path) or lib_path
            lib_name = f"[文件夹] {raw_name}" if lib_kind == 'folder' else raw_name
            
            # 主操作行
            widget = QWidget()
            layout = QHBoxLayout(widget)
            layout.setContentsMargins(5, 5, 5, 5)
            
            checkbox = QCheckBox(lib_name)
            checkbox.setChecked(lib.get('enabled', True))
            checkbox.toggled.connect(lambda _, p=lib_path: self.toggle_library_enabled(p))
            
            open_button = QPushButton("📂") # 打开文件夹图标
            open_button.setFixedSize(20, 20)
            open_button.setToolTip("打开词库文件夹" if lib_kind == 'folder' else "打开词库文件所在文件夹")
            open_button.clicked.connect(lambda _, p=lib_path, k=lib_kind: self.open_library_location(p, k))

            remove_button = QPushButton("❌") # 删除图标
            remove_button.setFixedSize(20, 20)
            remove_button.setToolTip("移除此词库")
            remove_button.clicked.connect(lambda _, p=lib_path: self.remove_library(p))
            
            layout.addWidget(checkbox)
            layout.addStretch()
            layout.addWidget(open_button)
            layout.addWidget(remove_button)
            
            action = QWidgetAction(self.library_menu)
            action.setDefaultWidget(widget)
            self.library_menu.addAction(action)

        # 这个逻辑不再需要，因为 auto_library_menu 现在是顶级菜单

    @Slot(str)
    def open_library_location(self, path, kind='file'):
        """在文件浏览器中打开指定词库的对应位置。"""
        try:
            target = path if kind == 'folder' else os.path.dirname(path)
            webbrowser.open(target)
            log(f"尝试打开词库位置: {target}")
        except Exception as e:
            log(f"打开词库位置失败: {e}")
            QMessageBox.warning(self.popup, "错误", f"无法打开文件路径：\n{path}\n\n错误: {e}")

    @Slot(str, str)
    def on_shortcut_matched(self, full_content, shortcut_code):
        """处理快捷码匹配成功的事件"""
        target_hwnd = self._capture_target_hwnd()
        if self.is_draft_foreground(target_hwnd):
            return
        log(f"主控制器收到快捷码匹配信号: {shortcut_code} | target_hwnd={target_hwnd}")
        QTimer.singleShot(
            self.shortcut_erase_grace_ms,
            lambda content=full_content, code=shortcut_code, hwnd=target_hwnd: self._commit_shortcut_match(content, code, hwnd)
        )

    def _commit_shortcut_match(self, full_content, shortcut_code, target_hwnd):
        if self.is_draft_foreground() or self.is_draft_foreground(target_hwnd):
            return
        for _ in range(len(shortcut_code)):
            self.shortcut_listener.keyboard_controller.press(keyboard.Key.backspace)
            self.shortcut_listener.keyboard_controller.release(keyboard.Key.backspace)

        self.on_suggestion_selected(full_content, target_hwnd=target_hwnd, origin='shortcut')

    @Slot()
    def cleanup_and_exit(self):
        log("开始执行程序清理...")
        self._cancel_pending_paste()
        self.composition_capture.stop()
        if self.composition_hotkey:
            self.composition_hotkey.stop()
        if self.popup.composition.flush():
            self.popup.composition.auto_backup()
        self.hotkey_manager.stop()
        if self.shortcut_listener:
            self.shortcut_listener.stop() # 退出时停止快捷码监听
        self.stop_file_observer() # 确保停止 watchdog
        if self.instance_manager:
            self.instance_manager.release()
        log("所有监听器已停止，程序准备退出。")

    @Slot()
    def confirm_exit(self):
        if not self.popup.composition.flush():
            return QMessageBox.question(
                self.popup, "模块正文尚未保存", "组合输入模块正文仍未写入磁盘。仍然退出？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) == QMessageBox.Yes
        if not self.popup.continuous_editor.has_draft():
            return True
        return QMessageBox.question(
            self.popup, "草稿尚未落字", "继续会丢弃本次运行中的草稿。要继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    @Slot()
    def set_paste_mode(self, mode):
        """设置新的粘贴模式并保存"""
        if mode not in SUPPORTED_PASTE_MODES:
            return
        if self.settings.paste_mode != mode:
            if self._update_settings(paste_mode=mode):
                log(f"粘贴模式已切换为: {mode}")

    @Slot()
    def toggle_hotkeys_enabled(self):
        enable_hotkeys = not self.settings.hotkeys_enabled
        old_hotkey = self.settings.hotkey
        was_enabled = self.settings.hotkeys_enabled
        if enable_hotkeys:
            success, normalized_hotkey, error_message = self.hotkey_manager.reregister(self.settings.hotkey)
            if not success:
                if hasattr(self, 'toggle_hotkeys_action'):
                    self.toggle_hotkeys_action.setChecked(False)
                QMessageBox.warning(self.popup, "热键注册失败", error_message)
                return

            restored = lambda: (
                self.hotkey_manager.reregister(old_hotkey)
                if was_enabled else self.hotkey_manager.unregister_all()
            )
            if self._update_settings(
                hotkeys_enabled=True,
                hotkey=normalized_hotkey,
                rollback_runtime=restored,
            ):
                if getattr(self, "composition_hotkey", None):
                    self.composition_hotkey.start()
                log(f"快捷键已启用: {normalized_hotkey}")
        else:
            if self._update_settings(hotkeys_enabled=False):
                self.hotkey_manager.unregister_all()
                if getattr(self, "composition_hotkey", None):
                    self.composition_hotkey.stop()
                log("快捷键已禁用。")
        
        if hasattr(self, 'toggle_hotkeys_action'):
            self.toggle_hotkeys_action.setChecked(self.settings.hotkeys_enabled)

    @Slot()
    def toggle_shortcut_code_enabled(self):
        """切换快捷码功能的启用状态"""
        enabled = not self.settings.shortcut_code_enabled
        if not self._update_settings(shortcut_code_enabled=enabled):
            if hasattr(self, 'toggle_shortcut_code_action'):
                self.toggle_shortcut_code_action.setChecked(self.settings.shortcut_code_enabled)
            return
        if self.settings.shortcut_code_enabled:
            self.shortcut_listener.start()
            log("快捷码功能已启用。")
        else:
            self.shortcut_listener.stop()
            log("快捷码功能已禁用。")
        
        if hasattr(self, 'toggle_shortcut_code_action'):
            self.toggle_shortcut_code_action.setChecked(self.settings.shortcut_code_enabled)

    @Slot()
    def toggle_theme(self):
        new_theme = "light" if self.settings.theme == "dark" else "dark"
        if not self._update_settings(theme=new_theme):
            return
        log(f"切换主题为: {new_theme}"); self.popup.apply_theme()
        if hasattr(self, 'toggle_theme_action'): self.toggle_theme_action.setText(f"切换到 {'夜间' if new_theme == 'light' else '日间'} 模式")

    @Slot()
    def toggle_multi_word_search(self):
        if not self._update_settings(multi_word_search=not self.settings.multi_word_search):
            if hasattr(self, 'multi_word_search_action'):
                self.multi_word_search_action.setChecked(self.settings.multi_word_search)
            return
        log(f"多词搜索模式: {'开启' if self.settings.multi_word_search else '关闭'}")
        if hasattr(self, 'multi_word_search_action'):
            self.multi_word_search_action.setChecked(self.settings.multi_word_search)
        
    @Slot()
    def set_font_size(self):
        selection = MainController.choose_font_settings(self)
        if selection and self._update_settings(font_size=selection[0], font_family=selection[1]):
            self.popup.apply_global_font()

    def choose_font_settings(self):
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QDialogButtonBox, QFontComboBox, QFormLayout, QSpinBox

        dialog = QDialog(self.popup)
        dialog.setWindowTitle("字体设置")
        layout = QFormLayout(dialog)
        family = QFontComboBox()
        family.setEditable(True)
        family.completer().setFilterMode(Qt.MatchContains)
        family.lineEdit().setPlaceholderText("输入字体名称搜索")
        size = QSpinBox()
        size.setRange(8, 72)
        size.setValue(self.settings.font_size)
        system_font = self.app.property("quickkvSystemFont") or self.app.font()
        family.setCurrentFont(QFont(self.settings.font_family) if self.settings.font_family else system_font)
        use_default = not bool(self.settings.font_family)
        preview = QLabel("中文：项目记录\nEnglish: QuickKV    0123456789\n组合词：优先+核对+完成")
        preview.setWordWrap(True)
        preview.setMinimumWidth(250)

        def update():
            font = family.currentFont()
            font.setPixelSize(size.value())
            preview.setFont(font)

        def changed():
            nonlocal use_default
            use_default = False
            update()

        def reset():
            nonlocal use_default
            family.setCurrentFont(system_font)
            size.setValue(14)
            use_default = True
            update()

        family.currentFontChanged.connect(changed)
        size.valueChanged.connect(update)
        layout.addRow("字体", family)
        layout.addRow("字号", size)
        layout.addRow(preview)
        actions = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        actions.button(QDialogButtonBox.Save).setText("保存")
        actions.button(QDialogButtonBox.Cancel).setText("取消")
        actions.addButton("恢复默认", QDialogButtonBox.ResetRole).clicked.connect(reset)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addRow(actions)
        update()
        if dialog.exec() == QDialog.Accepted:
            return size.value(), "" if use_default else family.currentFont().family()
        return None

    @Slot()
    def toggle_highlight_matches(self):
        """切换匹配高亮的启用状态"""
        if not self._update_settings(highlight_matches=not self.settings.highlight_matches):
            if hasattr(self, 'highlight_matches_action'):
                self.highlight_matches_action.setChecked(self.settings.highlight_matches)
            return
        log(f"匹配高亮: {'开启' if self.settings.highlight_matches else '关闭'}")
        if hasattr(self, 'highlight_matches_action'):
            self.highlight_matches_action.setChecked(self.settings.highlight_matches)
        # 强制刷新列表以立即看到效果
        if self.popup.isVisible():
            self.popup.update_list(self.popup.search_box.text())

    @Slot()
    def toggle_word_wrap(self):
        """切换自动换行的启用状态"""
        if not self._update_settings(word_wrap_enabled=not self.settings.word_wrap_enabled):
            if hasattr(self, 'word_wrap_action'):
                self.word_wrap_action.setChecked(self.settings.word_wrap_enabled)
            return
        log(f"自动换行: {'开启' if self.settings.word_wrap_enabled else '关闭'}")
        if hasattr(self, 'word_wrap_action'):
            self.word_wrap_action.setChecked(self.settings.word_wrap_enabled)
        if self.popup.isVisible():
            self.popup.update_list(self.popup.search_box.text())

    @Slot()
    def toggle_show_source(self):
        """切换归属显示的启用状态"""
        if not self._update_settings(show_source_enabled=not self.settings.show_source_enabled):
            if hasattr(self, 'show_source_action'):
                self.show_source_action.setChecked(self.settings.show_source_enabled)
            return
        log(f"词条归属显示: {'开启' if self.settings.show_source_enabled else '关闭'}")
        if hasattr(self, 'show_source_action'):
            self.show_source_action.setChecked(self.settings.show_source_enabled)
        if self.popup.isVisible():
            self.popup.update_list(self.popup.search_box.text())

    @Slot()
    def toggle_pinyin_initial_search(self):
        if not self._update_settings(pinyin_initial_search=not self.settings.pinyin_initial_search):
            if hasattr(self, 'pinyin_search_action'):
                self.pinyin_search_action.setChecked(self.settings.pinyin_initial_search)
            return
        log(f"拼音首字母匹配: {'开启' if self.settings.pinyin_initial_search else '关闭'}")
        if hasattr(self, 'pinyin_search_action'):
            self.pinyin_search_action.setChecked(self.settings.pinyin_initial_search)

    @Slot()
    def set_hotkey(self):
        """弹出对话框以设置新的快捷键或触发字符串"""
        dialog = HotkeyDialog(
            parent=self.popup,
            current_hotkey=self.settings.hotkey,
            current_hotkey_enabled=self.settings.hotkeys_enabled,
            current_string_trigger_enabled=self.settings.string_trigger_enabled,
            current_string_trigger_str=self.settings.string_trigger_str,
            theme=THEMES[self.settings.theme],
            font_size=self.settings.font_size
        )
        if dialog.exec():
            hot_en, hot_str, str_en, str_val = dialog.get_settings()
            old_hotkeys_enabled = self.settings.hotkeys_enabled
            old_hotkey = self.settings.hotkey
            old_string_enabled = self.settings.string_trigger_enabled
            old_string_value = self.settings.string_trigger_str

            normalized_hotkey = self.settings.hotkey
            if hot_str.strip():
                parsed_hotkey = self.hotkey_manager.validate_hotkey(hot_str)
                if not parsed_hotkey["valid"]:
                    QMessageBox.warning(self.popup, "设置错误", parsed_hotkey["error"])
                    return
                normalized_hotkey = parsed_hotkey["normalized"]
            elif not hot_en:
                normalized_hotkey = old_hotkey

            changed = (
                hot_en != old_hotkeys_enabled
                or normalized_hotkey != old_hotkey
                or str_en != old_string_enabled
                or str_val != old_string_value
            )

            if not changed:
                return

            if hot_en:
                success, normalized_hotkey, error_message = self.hotkey_manager.reregister(normalized_hotkey)
                if not success:
                    if old_hotkeys_enabled:
                        restore_success, restored_hotkey, restore_error = self.hotkey_manager.reregister(old_hotkey)
                        if restore_success:
                            log(f"新热键注册失败，已恢复旧热键: {restored_hotkey}")
                        else:
                            log(f"CRITICAL: 新热键注册失败且恢复旧热键失败: {restore_error}")
                    else:
                        self.hotkey_manager.unregister_all()

                    QMessageBox.warning(self.popup, "热键注册失败", error_message)
                    return
            else:
                self.hotkey_manager.unregister_all()

            self.hotkey_manager.update_string_trigger(str_en, str_val)

            def restore_previous_triggers():
                if old_hotkeys_enabled:
                    self.hotkey_manager.reregister(old_hotkey)
                else:
                    self.hotkey_manager.unregister_all()
                self.hotkey_manager.update_string_trigger(old_string_enabled, old_string_value)

            if not self._update_settings(
                hotkeys_enabled=hot_en,
                hotkey=normalized_hotkey,
                string_trigger_enabled=str_en,
                string_trigger_str=str_val,
                rollback_runtime=restore_previous_triggers,
            ):
                return
            log(f"触发配置已更新 | 组合键: {hot_en} '{normalized_hotkey}' | 连续字符串: {str_en} '{str_val}'")
            QMessageBox.information(None, "成功", "触发方式设置已更新！")

    # --- 新增：剪贴板菜单相关方法 ---
    @Slot()
    def toggle_clipboard_memory(self):
        if not self._update_settings(clipboard_memory_enabled=not self.settings.clipboard_memory_enabled):
            if hasattr(self, 'clipboard_memory_action'):
                self.clipboard_memory_action.setChecked(self.settings.clipboard_memory_enabled)
            return
        # self.update_clipboard_monitor_status()
        if hasattr(self, 'clipboard_memory_action'):
            self.clipboard_memory_action.setChecked(self.settings.clipboard_memory_enabled)
        # 刷新列表
        if self.popup.isVisible():
            self.popup.update_list(self.popup.search_box.text())

    @Slot()
    def set_clipboard_memory_count(self):
        current_count = self.settings.clipboard_memory_count
        new_count, ok = QInputDialog.getInt(None, "设置记忆次数",
                                             "请输入剪贴板记忆的最大条数:",
                                             current_count, 1, 100, 1)
        if ok and new_count != current_count:
            if not self._update_settings(clipboard_memory_count=new_count):
                return
            log(f"剪贴板记忆次数已更新为: {new_count}")
            QMessageBox.information(None, "成功", f"剪贴板记忆次数已设置为 {new_count} 条！")

    @Slot()
    def clear_clipboard_history_menu(self):
        reply = QMessageBox.question(None, "确认清空",
                                     "确定要清空所有剪贴板历史记录吗？此操作不可恢复。",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            if self.word_manager.clear_clipboard_history():
                self.sync_clipboard_timestamps()
                QMessageBox.information(None, "成功", "剪贴板历史已清空！")
                if self.popup.isVisible():
                    self.popup.update_list("")
            else:
                QMessageBox.warning(None, "错误", "清空剪贴板历史失败！")

    @Slot()
    def toggle_clipboard_auto_clear(self):
        if not self._update_settings(clipboard_auto_clear_enabled=not self.settings.clipboard_auto_clear_enabled):
            if hasattr(self, 'clipboard_auto_clear_action'):
                self.clipboard_auto_clear_action.setChecked(self.settings.clipboard_auto_clear_enabled)
            return
        if hasattr(self, 'clipboard_auto_clear_action'):
            self.clipboard_auto_clear_action.setChecked(self.settings.clipboard_auto_clear_enabled)
        
        if self.settings.clipboard_auto_clear_enabled:
            current_time = time.time()
            self.sync_clipboard_timestamps(current_time=current_time)
            self.check_clipboard_auto_clear()
        
        log(f"剪贴板定时清除功能: {'开启' if self.settings.clipboard_auto_clear_enabled else '关闭'}")

    @Slot()
    def set_clipboard_auto_clear_minutes(self):
        current_minutes = self.settings.clipboard_auto_clear_minutes
        new_minutes, ok = QInputDialog.getInt(None, "设置清除时间",
                                              "请输入经过多少分钟后清除:",
                                              current_minutes, 1, 1440, 1)
        if ok and new_minutes != current_minutes:
            if not self._update_settings(clipboard_auto_clear_minutes=new_minutes):
                return
            log(f"剪贴板清除时间已更新为: {new_minutes} 分钟")
            self.check_clipboard_auto_clear()
            QMessageBox.information(None, "成功", f"剪贴板清除时间已设置为 {new_minutes} 分钟！")

    @Slot()
    def check_clipboard_auto_clear(self):
        if not self.settings.clipboard_auto_clear_enabled:
            return
        
        current_time = time.time()
        threshold = self.settings.clipboard_auto_clear_minutes * 60
        items_to_delete = []
        
        for block in self.word_manager.clipboard_history:
            full_content = block['full_content']
            timestamp = self.clipboard_timestamps.get(content_key(full_content), current_time)
            if current_time - timestamp > threshold:
                items_to_delete.append(full_content)
                
        if items_to_delete:
            deleted_any = False
            for full_content in items_to_delete:
                if self.word_manager.clipboard_source and self.word_manager.clipboard_source.delete_entry(full_content):
                    deleted_any = True
            
            if deleted_any:
                log(f"已自动清除过期的剪贴板内容: {len(items_to_delete)} 条")
                self.word_manager.load_clipboard_history() # 重新加载以更新内部状态
                self.sync_clipboard_timestamps(current_time=current_time)
                if self.popup.isVisible():
                    self.popup.update_list(self.popup.search_box.text())

    # --- 新增：自动重启相关方法 ---
    @Slot()
    def _perform_auto_restart(self):
        if not self.popup.continuous_editor.has_draft():
            self.perform_restart()

    @Slot()
    def request_restart(self):
        if self.confirm_exit():
            self.perform_restart()

    @Slot()
    def perform_restart(self):
        """执行重启操作"""
        log("执行重启...")
        if not self._save_settings() or not self.popup.composition.flush():
            return
        self._cancel_pending_paste()
        self.composition_capture.stop()
        if self.composition_hotkey:
            self.composition_hotkey.stop()
        self.popup.hide()
        self.hotkey_manager.stop()
        if self.shortcut_listener:
            self.shortcut_listener.stop()
        self.stop_file_observer()
        if self.instance_manager:
            self.instance_manager.release()
        QTimer.singleShot(100, self._restart_process)

    def _restore_services_after_failed_restart(self):
        """重启派发失败后恢复本实例的运行态。"""
        hotkey_restore_failed = ""
        if self.settings.hotkeys_enabled:
            success, normalized_hotkey, error_message = self.hotkey_manager.reregister(self.settings.hotkey)
            if success:
                self.settings.hotkey = normalized_hotkey
            else:
                self._update_settings(hotkeys_enabled=False, notify=False)
                if hasattr(self, 'toggle_hotkeys_action'):
                    self.toggle_hotkeys_action.setChecked(False)
                hotkey_restore_failed = error_message

        if self.settings.hotkeys_enabled and self.composition_hotkey:
            self.composition_hotkey.start()

        self.hotkey_manager.update_string_trigger(
            self.settings.string_trigger_enabled,
            self.settings.string_trigger_str
        )

        if self.shortcut_listener and self.settings.shortcut_code_enabled:
            self.shortcut_listener.start()
        self.start_file_observer()

        if hotkey_restore_failed:
            QMessageBox.warning(
                self.popup,
                "热键恢复失败",
                f"重启派发失败后，当前实例的热键恢复也失败了：\n{hotkey_restore_failed}"
            )

    def _restore_ownership_after_failed_restart(self):
        """Return True only when this stopped instance safely becomes owner again."""
        if not self.instance_manager:
            return True
        from core.single_instance import SingleInstanceManager
        result = self.instance_manager.acquire()
        if result == SingleInstanceManager.OWNER:
            return True
        log("重启派发失败后未能重新取得单实例所有权，当前实例将退出。")
        return False

    def _restart_process(self):
        """实际的重启进程调用"""
        try:
            log(f"准备重启: sys.executable={sys.executable}, sys.argv={sys.argv}")
            restart_args = sys.argv[1:] if getattr(sys, 'frozen', False) else sys.argv
            success, pid = self._start_detached_process(sys.executable, restart_args)
            if not success:
                raise RuntimeError("QProcess.startDetached 返回 False。")

            log(f"重启新进程已成功启动。PID={pid}")
            self.app.quit()
        except Exception as e:
            log(f"重启失败: {e}")
            if self._restore_ownership_after_failed_restart():
                self._restore_services_after_failed_restart()
                QMessageBox.critical(None, "错误", f"应用程序重启失败: {e}")
            else:
                QMessageBox.critical(None, "错误", f"应用程序重启失败且所有权已移交：{e}")
                self.app.quit()

    def update_auto_restart_timer(self):
        """更新自动重启定时器的状态"""
        if self.settings.auto_restart_enabled and self.settings.auto_restart_interval > 0:
            interval_ms = self.settings.auto_restart_interval * 60 * 1000
            self.auto_restart_timer.start(interval_ms)
            log(f"自动重启定时器已启动，间隔: {self.settings.auto_restart_interval} 分钟。")
        else:
            self.auto_restart_timer.stop()
            log("自动重启定时器已停止。")

    @Slot()
    def toggle_auto_restart(self):
        """切换自动重启状态"""
        if not self._update_settings(auto_restart_enabled=not self.settings.auto_restart_enabled):
            if hasattr(self, 'auto_restart_action'):
                self.auto_restart_action.setChecked(self.settings.auto_restart_enabled)
            return
        self.update_auto_restart_timer()
        if hasattr(self, 'auto_restart_action'):
            self.auto_restart_action.setChecked(self.settings.auto_restart_enabled)

    @Slot()
    def set_auto_restart_interval(self):
        """设置自动重启间隔"""
        current_interval = self.settings.auto_restart_interval
        new_interval, ok = QInputDialog.getInt(None, "设置自动重启间隔",
                                               "请输入新的间隔分钟数 (0 表示禁用):",
                                               current_interval, 0, 1440, 1)
        if ok and new_interval != current_interval:
            if not self._update_settings(auto_restart_interval=new_interval):
                return
            self.update_auto_restart_timer()
            QMessageBox.information(None, "成功", f"自动重启间隔已设置为 {new_interval} 分钟！")

    def show_disclaimer(self):
        """显示重要声明与协议对话框"""
        dialog = DisclaimerDialog(self.popup, THEMES[self.settings.theme], self.settings.font_size)
        # 对于已经同意过的用户，只显示信息，不提供“同意/不同意”选项
        dialog.agree_checkbox.setChecked(True)
        dialog.agree_checkbox.setVisible(False)
        dialog.agree_button.setText("关闭")
        dialog.disagree_button.setVisible(False)
        dialog.exec()

    def open_help_docs(self):
        """打开项目主页与帮助"""
        target_url = "https://github.com/EllisMorrow/QuickKV"
        try:
            webbrowser.open(target_url)
            log(f"已打开项目主页: {target_url}")
        except Exception as e:
            log(f"打开项目主页失败: {e}")
            QMessageBox.warning(self.popup, "错误", f"无法打开链接：\n{target_url}\n\n错误: {e}")  

    def scan_and_update_auto_libraries(self):
        """
        扫描自动加载文件夹，同步词库列表并保存状态。
        返回一个布尔值，指示列表是否发生了变化。
        """
        log("开始扫描自动加载词库文件夹...")
        if not os.path.isdir(AUTO_LOAD_DIR):
            if self.settings.auto_libraries:
                log(f"自动加载目录不存在: {AUTO_LOAD_DIR}，清空配置。")
                return self._update_settings(auto_libraries=[], notify=False)
            return False

        try:
            found_files = {
                os.path.join(AUTO_LOAD_DIR, f)
                for f in os.listdir(AUTO_LOAD_DIR)
                if f.endswith('.md') and is_eligible_library_file(os.path.join(AUTO_LOAD_DIR, f))
            }
        except Exception as e:
            log(f"扫描自动加载目录时出错: {e}")
            return False

        existing_paths = {lib['path'] for lib in self.settings.auto_libraries}
        
        new_files = found_files - existing_paths
        removed_files = existing_paths - found_files
        
        if not new_files and not removed_files:
            return False # 无变化

        auto_libraries = [
            lib for lib in self.settings.auto_libraries
            if lib['path'] not in removed_files
        ]
        auto_libraries.extend(
            {"path": path, "enabled": True, "kind": "file"}
            for path in new_files
        )
        if not self._update_settings(auto_libraries=auto_libraries, notify=False):
            return False
        for path in new_files:
            log(f"发现并添加新自动词库: {os.path.basename(path)}")
        for path in removed_files:
            log(f"移除不存在的自动词库: {os.path.basename(path)}")
        return True
