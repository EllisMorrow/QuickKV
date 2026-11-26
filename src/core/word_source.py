# -*- coding: utf-8 -*-
import sys
import os
import tempfile
import webbrowser
import configparser
import hashlib
import json
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

SHORTCUT_COMMAND_RE = re.compile(r'^k\s*[:：](?![:：])\s*(.+)$', re.IGNORECASE)
ALIAS_COMMAND_RE = re.compile(r'^bm\s*[:：](?![:：])\s*(.+)$', re.IGNORECASE)

# --- 词库数据源 ---
class WordSource:
    def __init__(self, file_path):
        self.file_path = file_path
        self.publisher = None
        self.word_blocks = []
        self.source_revision = None
        self.load()

    def load(self):
        log(f"开始从 {self.file_path} 加载词库...")
        self.word_blocks = []
        self.source_revision = None
        try:
            with open(self.file_path, 'rb') as source_file:
                source_bytes = source_file.read()
            self.source_revision = hashlib.md5(source_bytes).hexdigest()
            lines = source_bytes.decode('utf-8').splitlines(keepends=True)

            current_block = None
            for line in lines:
                if line.startswith('- '):
                    if current_block:
                        current_block['full_content'] = '\n'.join(current_block['raw_lines'])
                        current_block['block_index'] = len(self.word_blocks)
                        self.word_blocks.append(current_block)
                    
                    parent_text = line.strip()[2:].strip()
                    
                    # --- 新的元命令解析逻辑 ---
                    # 匹配所有 ``...`` 形式的元命令
                    meta_commands_pattern = r'``(.*?)``'
                    meta_commands = re.findall(meta_commands_pattern, parent_text)
                    
                    # 从原始文本中移除所有元命令，得到纯净的 parent_text
                    clean_parent_text = re.sub(meta_commands_pattern, '', parent_text).strip()

                    should_exclude = False
                    shortcut_code = None
                    aliases = []

                    # 遍历找到的所有元命令并进行处理
                    for command in meta_commands:
                        normalized_command = command.strip()
                        if normalized_command == '不出现':
                            should_exclude = True
                            continue

                        shortcut_match = SHORTCUT_COMMAND_RE.match(normalized_command)
                        if shortcut_match:
                            # 提取 k/K + :/： 后面的内容作为快捷码
                            shortcut_code = shortcut_match.group(1).strip()
                            continue

                        alias_match = ALIAS_COMMAND_RE.match(normalized_command)
                        if alias_match:
                            alias_text = alias_match.group(1).strip()
                            if alias_text:
                                aliases.extend(re.split(r'[、，]', alias_text))
                    # --- 新逻辑结束 ---

                    current_block = {
                        'parent': clean_parent_text, # 使用纯净文本
                        'raw_lines': [line.rstrip()],
                        'exclude_parent': should_exclude,
                        'shortcut_code': shortcut_code, # 新增：快捷码
                        'aliases': aliases,
                        'source_path': self.file_path, # 标记来源
                        'source_revision': self.source_revision,
                        'is_clipboard': False # 默认非剪贴板
                    }
                elif current_block:
                    current_block['raw_lines'].append(line.rstrip())

            if current_block:
                current_block['full_content'] = '\n'.join(current_block['raw_lines'])
                current_block['block_index'] = len(self.word_blocks)
                self.word_blocks.append(current_block)
            
            log(f"成功从 {os.path.basename(self.file_path)} 加载 {len(self.word_blocks)} 个词条。")
        except FileNotFoundError:
            log(f"词库文件不存在: {self.file_path}")
        except Exception as e:
            log(f"加载 {self.file_path} 时发生错误: {e}")

    def add_entry(self, content):
        try:
            if self.publisher is not None:
                with open(self.file_path, "rb") as source:
                    previous = source.read()
                newline = "\r\n" if b"\r\n" in previous else "\n"
                separator = "" if not previous or previous.endswith((b"\r", b"\n")) else newline
                return self._atomic_write(previous.decode("utf8") + separator + content + newline, previous)
            with open(self.file_path, 'a', encoding='utf-8') as f:
                f.write('\n' + content)
            return True
        except Exception as e:
            log(f"向 {self.file_path} 添加词条时发生错误: {e}")
            return False

    @staticmethod
    def _parse_entry_spans(file_content):
        """Return exact character spans and normalized content for top-level entries."""
        starts = [match.start() for match in re.finditer(r'(?m)^- ', file_content)]
        spans = []
        for block_index, start in enumerate(starts):
            end = starts[block_index + 1] if block_index + 1 < len(starts) else len(file_content)
            raw_block = file_content[start:end]
            normalized_content = '\n'.join(line.rstrip() for line in raw_block.splitlines(keepends=True))
            spans.append((start, end, normalized_content))
        return spans

    def _locate_entry(self, file_content, expected_content, block_index=None):
        spans = self._parse_entry_spans(file_content)

        if block_index is not None:
            if not isinstance(block_index, int) or not 0 <= block_index < len(spans):
                return None
            span = spans[block_index]
            return span if span[2] == expected_content else None

        # Compatibility for non-UI callers that do not yet carry a block index.
        return next((span for span in spans if span[2] == expected_content), None)

    @staticmethod
    def _replace_file_windows(target_path, replacement_path, backup_path):
        """Atomically replace target while preserving its replacement-time contents."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        replace_file = kernel32.ReplaceFileW
        replace_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        replace_file.restype = wintypes.BOOL
        if not replace_file(
            os.fspath(target_path),
            os.fspath(replacement_path),
            os.fspath(backup_path),
            0x00000001,  # REPLACEFILE_WRITE_THROUGH
            None,
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _reserve_absent_path(directory, prefix, suffix):
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
        os.close(fd)
        os.remove(path)
        return path

    def _atomic_write(self, content, expected_source_bytes):
        if getattr(self, "publisher", None) is not None:
            return self.publisher(expected_source_bytes, content.encode("utf8"))
        directory = os.path.dirname(os.path.abspath(self.file_path))
        prefix = f".{os.path.basename(self.file_path)}."
        fd = None
        temp_path = None
        backup_path = None
        preserve_backup = False
        recovery_path = None
        preserve_recovery = False
        try:
            fd, temp_path = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=directory)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as temp_file:
                fd = None
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.chmod(temp_path, os.stat(self.file_path).st_mode)

            # The source may change while the replacement content is being prepared.
            # This final check closes that long window on every platform.
            with open(self.file_path, 'rb') as source_file:
                if source_file.read() != expected_source_bytes:
                    log(f"写入冲突: {self.file_path} 在最终替换前发生变化")
                    return False

            if sys.platform != 'win32':
                # os.replace has no compare-and-swap primitive. A writer in the tiny
                # interval after the check can still be overwritten on non-Windows.
                os.replace(temp_path, self.file_path)
                temp_path = None
                return True

            backup_path = self._reserve_absent_path(directory, prefix, '.conflict.bak')
            try:
                self._replace_file_windows(self.file_path, temp_path, backup_path)
                temp_path = None
                # Until the backup is verified or restored, it may be the only copy
                # of a concurrent external version and must survive any later error.
                preserve_backup = True
            except Exception:
                preserve_backup = bool(backup_path and os.path.exists(backup_path))
                raise

            with open(backup_path, 'rb') as replaced_source:
                replaced_source_bytes = replaced_source.read()

            if replaced_source_bytes == expected_source_bytes:
                os.remove(backup_path)
                backup_path = None
                preserve_backup = False
                return True

            # ReplaceFileW captured a different target at the replacement instant.
            # Restore it atomically. A second writer during restoration is retained
            # in recovery_path instead of being silently discarded.
            recovery_path = self._reserve_absent_path(directory, prefix, '.recovery.bak')
            try:
                self._replace_file_windows(self.file_path, backup_path, recovery_path)
                backup_path = None
                preserve_backup = False
                with open(recovery_path, 'rb') as replaced_during_restore:
                    recovery_bytes = replaced_during_restore.read()
                if recovery_bytes == content.encode('utf-8'):
                    os.remove(recovery_path)
                    recovery_path = None
                else:
                    preserve_recovery = True
                    log(f"写入冲突恢复期间目标再次变化，版本保存在: {recovery_path}")
                log(f"写入冲突: {self.file_path} 的外部版本已恢复")
            except Exception as restore_error:
                preserve_backup = bool(backup_path and os.path.exists(backup_path))
                preserve_recovery = bool(recovery_path and os.path.exists(recovery_path))
                recoverable_paths = [
                    path
                    for path in (backup_path, recovery_path)
                    if path and os.path.exists(path)
                ]
                log(
                    "写入冲突恢复失败"
                    f"，可恢复版本: {', '.join(recoverable_paths) or '无独立备份'}"
                    f"；错误: {restore_error}"
                )
            return False
        except Exception as e:
            log(f"原子写入 {self.file_path} 时发生错误: {e}")
            return False
        finally:
            if fd is not None:
                os.close(fd)
            if temp_path is not None:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass
                except Exception as cleanup_error:
                    log(f"清理临时文件 {temp_path} 时发生错误: {cleanup_error}")
            if backup_path is not None and not preserve_backup:
                try:
                    os.remove(backup_path)
                except FileNotFoundError:
                    pass
                except Exception as cleanup_error:
                    log(f"清理备份文件 {backup_path} 时发生错误: {cleanup_error}")
            if recovery_path is not None and not preserve_recovery:
                try:
                    os.remove(recovery_path)
                except FileNotFoundError:
                    pass
                except Exception as cleanup_error:
                    log(f"清理恢复文件 {recovery_path} 时发生错误: {cleanup_error}")

    def update_entry(self, original_content, new_content, block_index=None, source_revision=None):
        try:
            with open(self.file_path, 'rb') as source_file:
                source_bytes = source_file.read()

            current_revision = hashlib.md5(source_bytes).hexdigest()
            if source_revision is not None and current_revision != source_revision:
                log(f"update_entry: {self.file_path} 已在选择词条后发生变化")
                return False
            file_content = source_bytes.decode('utf-8')

            target = self._locate_entry(file_content, original_content, block_index)
            if target is None:
                log(f"update_entry: 在 {self.file_path} 中未找到要更新的内容")
                return False

            start, end, _ = target
            if new_content == original_content:
                return True

            raw_block = file_content[start:end]
            trailing_whitespace_match = re.search(r'[ \t\r\n]*\Z', raw_block)
            trailing_whitespace = trailing_whitespace_match.group(0)
            normalized_trailing_newlines = len(original_content) - len(original_content.rstrip('\n'))
            replacement = new_content
            while normalized_trailing_newlines and replacement.endswith('\n'):
                replacement = replacement[:-1]
                normalized_trailing_newlines -= 1
            replacement += trailing_whitespace

            new_file_content = file_content[:start] + replacement + file_content[end:]
            return self._atomic_write(new_file_content, source_bytes)
        except Exception as e:
            log(f"更新 {self.file_path} 时发生错误: {e}")
            return False

    def delete_entry(self, content_to_delete, block_index=None, source_revision=None):
        try:
            with open(self.file_path, 'rb') as source_file:
                source_bytes = source_file.read()

            current_revision = hashlib.md5(source_bytes).hexdigest()
            if source_revision is not None and current_revision != source_revision:
                log(f"delete_entry: {self.file_path} 已在选择词条后发生变化")
                return False
            file_content = source_bytes.decode('utf-8')

            target = self._locate_entry(file_content, content_to_delete, block_index)
            if target is None:
                log(f"delete_entry: 在 {self.file_path} 中未找到要删除的内容")
                return False

            start, end, _ = target
            new_file_content = file_content[:start] + file_content[end:]
            return self._atomic_write(new_file_content, source_bytes)
        except Exception as e:
            log(f"删除 {self.file_path} 的词条时发生错误: {e}")
            return False
