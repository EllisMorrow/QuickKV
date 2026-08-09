"""Durable, content-addressed timestamps for clipboard history entries."""

import hashlib
import json
import math
import os
import tempfile


TIMESTAMP_FILE_VERSION = 1


def content_key(full_content):
    return hashlib.sha256(full_content.encode("utf-8")).hexdigest()


def utf8_size_at_most(text, limit, chunk_size=8192):
    """Return ``(size, exceeded)`` without allocating a bytes copy of all text."""
    size = 0
    for start in range(0, len(text), chunk_size):
        size += len(text[start:start + chunk_size].encode("utf-8"))
        if size > limit:
            return size, True
    return size, False


class ClipboardTimestampStore:
    """Stores first-seen timestamps keyed by a non-reversible content hash."""

    def __init__(self, file_path, logger=None):
        self.file_path = file_path
        self._log = logger or (lambda message: None)
        self.timestamps = self._load()

    def _load(self):
        try:
            with open(self.file_path, "r", encoding="utf-8") as source:
                payload = json.load(source)
            values = payload.get("timestamps", {})
            if payload.get("version") != TIMESTAMP_FILE_VERSION or not isinstance(values, dict):
                raise ValueError("invalid timestamp format")
            return {
                key: value
                for key, value in values.items()
                if isinstance(key, str)
                and isinstance(value, (int, float))
                and math.isfinite(value)
            }
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            self._log(f"剪贴板时间戳元数据不可用，将安全迁移: {error}")
            return {}

    def sync(self, full_contents, current_time):
        """Retain current entries and assign ``current_time`` to newly seen ones."""
        synced = {}
        for full_content in full_contents:
            key = content_key(full_content)
            synced[key] = self.timestamps.get(key, current_time)
        self.timestamps = synced
        self.save()
        return dict(self.timestamps)

    def save(self):
        directory = os.path.dirname(os.path.abspath(self.file_path))
        temp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            descriptor, temp_path = tempfile.mkstemp(
                prefix=".clipboard-timestamps-", suffix=".tmp", dir=directory
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(
                    {"version": TIMESTAMP_FILE_VERSION, "timestamps": self.timestamps},
                    target,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, self.file_path)
            temp_path = None
            return True
        except OSError as error:
            self._log(f"剪贴板时间戳元数据保存失败: {error}")
            return False
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
