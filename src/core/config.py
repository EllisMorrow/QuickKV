# -*- coding: utf-8 -*-
import sys
import os
import webbrowser
import configparser
import json
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


from utils.paths import get_base_path, resource_path

BASE_PATH = get_base_path()
USER_DATA_DIR = os.path.join(BASE_PATH, "用户数据")
AUTO_LOAD_DIR = os.path.join(BASE_PATH, "MD词库-需自动载入的请放入")

WORD_FILE = os.path.join(AUTO_LOAD_DIR, "词库.md")
CONFIG_FILE = os.path.join(USER_DATA_DIR, "config.ini")
CLIPBOARD_HISTORY_FILE = os.path.join(AUTO_LOAD_DIR, "剪贴板词库-勿删.md")
CLIPBOARD_TIMESTAMP_FILE = os.path.join(USER_DATA_DIR, "clipboard_timestamps.json")
CLIPBOARD_TEXT_MAX_BYTES = 1 * 1024 * 1024
CACHE_FILE = os.path.join(USER_DATA_DIR, "cache.json")
RANKING_STATE_FILE = os.path.join(USER_DATA_DIR, "ranking_state.json")

ICON_PATH = resource_path("icon.png")

DEBUG_MODE = True
VERSION = "1.3.9"
DISCLAIMER_REVISION = "3"

PASTE_MODE_CTRL_V = "ctrl_v"
PASTE_MODE_CTRL_SHIFT_V = "ctrl_shift_v"
PASTE_MODE_TYPING = "typing"
SUPPORTED_PASTE_MODES = {
    PASTE_MODE_CTRL_V,
    PASTE_MODE_CTRL_SHIFT_V,
    PASTE_MODE_TYPING,
}

def log(message):
    if DEBUG_MODE:
        print(f"[LOG] {message}")

def normalize_library_path(path):
    """统一词库路径格式，避免大小写和相对路径导致的重复判断失真。"""
    return os.path.normcase(os.path.abspath(path))

def get_internal_library_paths():
    """返回所有不应混入普通词库池的内部专用文件路径。"""
    return {
        normalize_library_path(CLIPBOARD_HISTORY_FILE),
    }

def is_internal_library_file(path):
    if not path:
        return False
    return normalize_library_path(path) in get_internal_library_paths()

def is_eligible_library_file(path):
    """判断一个文件是否可以作为普通词库文件参与加载。"""
    return (
        bool(path)
        and os.path.isfile(path)
        and path.lower().endswith(".md")
        and not is_internal_library_file(path)
    )

def list_eligible_md_files(folder_path):
    """列出目录首层可参与普通词库加载的 md 文件。"""
    files, _ = list_eligible_md_files_with_status(folder_path)
    return files


def list_eligible_md_files_with_status(folder_path):
    """列出目录首层可参与普通词库加载的 md 文件及枚举是否成功。"""
    if not folder_path or not os.path.isdir(folder_path):
        return [], False

    files = []
    try:
        for name in os.listdir(folder_path):
            full_path = os.path.join(folder_path, name)
            if is_eligible_library_file(full_path):
                files.append(os.path.abspath(full_path))
    except Exception as e:
        log(f"扫描目录 {folder_path} 时列出 md 文件失败: {e}")
        return [], False

    return sorted(files, key=lambda path: os.path.basename(path).lower()), True

# --- 主题颜色定义 ---
THEMES = {
    "dark": {
        "bg_color": "#21252b", "border_color": "#3c424b", "text_color": "#d1d5db",
        "title_color": "#8c929c",
        "input_bg_color": "#2c313a", "item_hover_bg": "#3a3f4b",
        "item_selected_bg": "#405061", "item_selected_text": "#d1d5db",
        "highlight_color": "#5294e2" # 新增：高亮颜色
    },
    "light": {
        "bg_color": "#fdfdfd", "border_color": "#cccccc", "text_color": "#202020",
        "title_color": "#a0a0a0",
        "input_bg_color": "#ffffff", "item_hover_bg": "#f0f0f0",
        "item_selected_bg": "#dbe4ee", "item_selected_text": "#202020",
        "highlight_color": "#007acc" # 新增：高亮颜色
    }
}

# --- 自定义列表项绘制代理 ---
