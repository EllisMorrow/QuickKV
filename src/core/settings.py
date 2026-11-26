# -*- coding: utf-8 -*-
import sys
import os
import webbrowser
import configparser
import copy
import hashlib
import json
import tempfile
import stat
import subprocess
import re
import itertools
import threading
import ctypes
from ctypes import wintypes
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
                             QListWidget, QListWidgetItem, QSystemTrayIcon, QMenu, QSizeGrip,
                             QGraphicsDropShadowEffect, QPushButton,
                             QInputDialog, QMessageBox, QStyledItemDelegate, QStyle, QFileDialog,
                             QCheckBox, QWidgetAction, QScrollArea, QLabel, QFrame)
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


from core.config import *

# --- 设置管理器 ---
class SettingsManager:
    def __init__(self, file_path):
        self.config = configparser.ConfigParser()
        self.file_path = file_path
        self.load()

    def _normalize_library_items(self, libraries, allow_folders=False):
        normalized_items = []
        seen = set()

        for lib in libraries:
            if not isinstance(lib, dict):
                continue

            raw_path = lib.get('path')
            if not raw_path:
                continue

            kind = str(lib.get('kind', 'file')).lower()
            if kind != 'folder' or not allow_folders:
                kind = 'file'

            abs_path = os.path.abspath(raw_path)
            norm_path = normalize_library_path(abs_path)

            if kind == 'folder':
                if not os.path.isdir(abs_path):
                    continue
            else:
                if not is_eligible_library_file(abs_path):
                    continue

            identity = (kind, norm_path)
            if identity in seen:
                continue

            seen.add(identity)
            normalized_items.append({
                'path': abs_path,
                'enabled': bool(lib.get('enabled', True)),
                'kind': kind,
            })

        return normalized_items

    def load(self):
        """加载配置文件；坏字段只回退自身默认值。"""
        self._set_defaults()
        self.config = configparser.ConfigParser()
        try:
            self.config.read(self.file_path, encoding='utf-8')
        except (OSError, UnicodeError, configparser.Error) as e:
            log(f"CRITICAL: 配置文件 '{self.file_path}' 解析失败: {e}。将使用默认设置。")
            self.config = configparser.ConfigParser()

        self._ensure_sections()
        self.hotkeys_enabled = self._getboolean('General', 'hotkeys_enabled', self.hotkeys_enabled)
        self.shortcut_code_enabled = self._getboolean('General', 'shortcut_code_enabled', self.shortcut_code_enabled)
        self.hotkey = self._get('General', 'hotkey', self.hotkey)
        self.composition_hotkey = self._get('General', 'composition_hotkey', self.composition_hotkey)
        self.string_trigger_enabled = self._getboolean('General', 'string_trigger_enabled', self.string_trigger_enabled)
        self.string_trigger_str = self._get('General', 'string_trigger_str', self.string_trigger_str)
        self.paste_mode = self._get('Paste', 'mode', self.paste_mode)
        if self.paste_mode == 'smart_compat' or self.paste_mode not in SUPPORTED_PASTE_MODES:
            self.paste_mode = PASTE_MODE_CTRL_V
        self.width = self._getint('Window', 'width', self.width)
        self.height = self._getint('Window', 'height', self.height)
        self.theme = self._get('Theme', 'mode', self.theme)
        self.font_size = self._getint('Font', 'size', self.font_size)
        self.font_family = self._get('Font', 'family', self.font_family)
        self.multi_word_search = self._getboolean('Search', 'multi_word_search', self.multi_word_search)
        self.pinyin_initial_search = self._getboolean('Search', 'pinyin_initial_search', self.pinyin_initial_search)
        self.highlight_matches = self._getboolean('Search', 'highlight_matches', self.highlight_matches)
        self.word_wrap_enabled = self._getboolean('UI', 'word_wrap_enabled', self.word_wrap_enabled)
        self.show_source_enabled = self._getboolean('UI', 'show_source_enabled', self.show_source_enabled)
        self.clipboard_memory_enabled = self._getboolean('Clipboard', 'enabled', self.clipboard_memory_enabled)
        self.clipboard_memory_count = self._getint('Clipboard', 'count', self.clipboard_memory_count)
        self.clipboard_auto_clear_enabled = self._getboolean('Clipboard', 'auto_clear_enabled', self.clipboard_auto_clear_enabled)
        self.clipboard_auto_clear_minutes = self._getint('Clipboard', 'auto_clear_minutes', self.clipboard_auto_clear_minutes)
        self.auto_restart_enabled = self._getboolean('Restart', 'enabled', self.auto_restart_enabled)
        self.auto_restart_interval = self._getint('Restart', 'interval_minutes', self.auto_restart_interval)
        self.accepted_disclaimer_info = self._get_json('General', 'accepted_disclaimer_info', self.accepted_disclaimer_info, dict)
        self.libraries = self._get_json('General', 'libraries', self.libraries, list)
        self.transcription_enabled = self._getboolean('General', 'transcription_enabled', True)
        self.transcription_rules = self._get_json('General', 'transcription_rules', '', str)
        self.auto_libraries = self._get_json('General', 'auto_libraries', self.auto_libraries, list)

        # 迁移和验证逻辑保持不变
        if not self.libraries and os.path.exists(WORD_FILE):
            self.libraries.append({"path": os.path.abspath(WORD_FILE), "enabled": True, "kind": "file"})
            log("已将旧的单一词库配置迁移到新的多词库系统。")

        self.libraries = self._normalize_library_items(self.libraries, allow_folders=True)
        self.auto_libraries = self._normalize_library_items(self.auto_libraries, allow_folders=False)

    def _set_defaults(self):
        self.hotkeys_enabled = True
        self.shortcut_code_enabled = True
        self.hotkey = 'ctrl+space'
        self.composition_hotkey = ''
        self.string_trigger_enabled = False
        self.string_trigger_str = '//'
        self.paste_mode = PASTE_MODE_CTRL_V
        self.width = 450
        self.height = 300
        self.theme = 'dark'
        self.font_size = 14
        self.font_family = ""
        self.multi_word_search = True
        self.pinyin_initial_search = True
        self.highlight_matches = True
        self.word_wrap_enabled = True
        self.show_source_enabled = True
        self.clipboard_memory_enabled = False
        self.clipboard_memory_count = 10
        self.clipboard_auto_clear_enabled = False
        self.clipboard_auto_clear_minutes = 10
        self.auto_restart_enabled = False
        self.auto_restart_interval = 3
        self.accepted_disclaimer_info = {'revision': ''}
        self.libraries = []
        self.auto_libraries = []
        self.transcription_enabled = True
        self.transcription_rules = ""

    def _ensure_sections(self, config=None):
        config = self.config if config is None else config
        for section in ('Window', 'Theme', 'Font', 'Search', 'Data', 'General', 'UI', 'Clipboard', 'Restart', 'Paste'):
            if not config.has_section(section):
                config.add_section(section)

    def _get(self, section, option, default):
        try:
            return self.config.get(section, option, fallback=default)
        except (configparser.Error, ValueError):
            return default

    def _getint(self, section, option, default):
        try:
            return self.config.getint(section, option, fallback=default)
        except (configparser.Error, ValueError):
            return default

    def _getboolean(self, section, option, default):
        try:
            return self.config.getboolean(section, option, fallback=default)
        except (configparser.Error, ValueError):
            return default

    def _get_json(self, section, option, default, expected_type):
        try:
            value = json.loads(self._get(section, option, ''))
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return value if isinstance(value, expected_type) else default

    def save(self):
        candidate = copy.deepcopy(self.config)
        self._ensure_sections(candidate)
        candidate['General']['hotkeys_enabled'] = str(self.hotkeys_enabled)
        candidate['General']['shortcut_code_enabled'] = str(self.shortcut_code_enabled) # 新增：保存快捷码功能开关
        candidate['General']['transcription_enabled'] = str(self.transcription_enabled)
        candidate['General']['transcription_rules'] = json.dumps(self.transcription_rules, ensure_ascii=False).replace('%', '%%')
        candidate['General']['hotkey'] = self.hotkey
        candidate['General']['composition_hotkey'] = self.composition_hotkey
        candidate['Window']['width'] = str(self.width)
        candidate['Window']['height'] = str(self.height)
        candidate['Theme']['mode'] = self.theme
        candidate['Font']['size'] = str(self.font_size)
        candidate['Font']['family'] = self.font_family
        candidate['Search']['multi_word_search'] = str(self.multi_word_search)
        candidate['Search']['pinyin_initial_search'] = str(self.pinyin_initial_search)
        candidate['Search']['highlight_matches'] = str(self.highlight_matches) # 新增
        candidate['UI']['word_wrap_enabled'] = str(self.word_wrap_enabled)
        candidate['UI']['show_source_enabled'] = str(self.show_source_enabled)
        candidate['General']['libraries'] = json.dumps(self.libraries, ensure_ascii=False)
        candidate['General']['auto_libraries'] = json.dumps(self.auto_libraries, ensure_ascii=False)
        candidate['Clipboard']['enabled'] = str(self.clipboard_memory_enabled)
        candidate['Clipboard']['count'] = str(self.clipboard_memory_count)
        candidate['Clipboard']['auto_clear_enabled'] = str(self.clipboard_auto_clear_enabled)
        candidate['Clipboard']['auto_clear_minutes'] = str(self.clipboard_auto_clear_minutes)
        candidate['Restart']['enabled'] = str(self.auto_restart_enabled)
        candidate['Restart']['interval_minutes'] = str(self.auto_restart_interval)
        candidate['Paste']['mode'] = self.paste_mode
        candidate['General']['accepted_disclaimer_info'] = json.dumps(self.accepted_disclaimer_info, ensure_ascii=False)
        
        # --- 新增：连续字符串触发器 ---
        candidate['General']['string_trigger_enabled'] = str(self.string_trigger_enabled)
        candidate['General']['string_trigger_str'] = self.string_trigger_str
        
        directory = os.path.dirname(os.path.abspath(self.file_path))
        temp_path = None
        try:
            existing_mode = None
            if os.path.exists(self.file_path):
                existing_mode = stat.S_IMODE(os.stat(self.file_path).st_mode)
            fd, temp_path = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=directory)
            if existing_mode is not None:
                os.chmod(temp_path, existing_mode)
            with os.fdopen(fd, 'w', encoding='utf-8') as configfile:
                candidate.write(configfile)
                configfile.flush()
                os.fsync(configfile.fileno())
            os.replace(temp_path, self.file_path)
            temp_path = None
        except (OSError, TypeError, ValueError) as e:
            log(f"配置保存失败: {e}")
            return False
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        self.config = candidate
        log(f"配置已保存到 {self.file_path}")
        return True
