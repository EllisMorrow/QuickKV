
"""Local composition sessions and recoverable publication of their Markdown library."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import RLock
import sys

from core.composition.document import Document, new_id


SCHEMA_VERSION = 1


class CompositionStorageError(RuntimeError):
    pass


class LibraryConflict(CompositionStorageError):
    pass


STORAGE_ERRORS = (OSError, sqlite3.Error, CompositionStorageError, ValueError, TypeError, KeyError)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_library(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def displaced_path(path):
    return path.with_name(".composition-previous.composition-recovery")


def restore_displaced(path, recovery, proposal):
    from core.word_source import WordSource
    competing = path.with_name(f".composition-{new_id()}.composition-recovery")
    WordSource._replace_file_windows(str(path), str(recovery), str(competing))
    if read_library(competing) == proposal:
        competing.unlink()


def replace_library(path, expected, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".composition-", delete=False) as temporary:
        staging = Path(temporary.name)
        try:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            if expected is None:
                # On Windows rename fails if the target exists, including on FAT volumes.
                # POSIX rename can replace a concurrent creator, so use a hard link there.
                if sys.platform == "win32":
                    os.rename(staging, path)
                else:
                    os.link(staging, path)
                return
            if read_library(path) != expected:
                raise LibraryConflict("专用词库已在写入前变更")
            if sys.platform != "win32":
                os.replace(staging, path)
                return
            from core.word_source import WordSource
            displaced = displaced_path(path)
            if displaced.exists():
                raise LibraryConflict("有尚未核对的词库替换前版本")
            WordSource._replace_file_windows(str(path), str(staging), str(displaced))
            if read_library(displaced) != expected:
                restore_displaced(path, displaced, contents)
                raise LibraryConflict("专用词库在替换时发生竞争")
            displaced.unlink()
        finally:
            staging.unlink(missing_ok=True)


class CompositionStore:
    def __init__(self, path, library_path, *, publish=replace_library):
        self.path = Path(path)
        self.library_path = Path(library_path)
        self._publish = publish
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                tables = connection.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall()
                if version or tables:
                    raise CompositionStorageError("组合输入数据库版本无法读取")
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE records (
                        key TEXT PRIMARY KEY, data TEXT NOT NULL
                    );
                    CREATE TABLE drafts (
                        id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL
                    );
                    CREATE TABLE library_intent (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        before BLOB, after BLOB NOT NULL, records TEXT NOT NULL
                    );
                    PRAGMA user_version = 1;
                    COMMIT;
                """)

    def _open(self):
        connection = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA foreign_keys=ON")
        except Exception:
            connection.close()
            raise
        return closing(connection)

    def get(self, key, default=None):
        with self._open() as connection:
            row = connection.execute("SELECT data FROM records WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    @staticmethod
    def _write_records(connection, records):
        restore = records.get("__restore__")
        if restore is not None:
            connection.execute("DELETE FROM records WHERE key != 'active_session' AND key NOT LIKE 'recovery:%'")
            modules = set(restore.get("modules", ()))
            # A protected snapshot already contains the pre-restore drafts.
            connection.executemany("DELETE FROM drafts WHERE id = ?", ((key,) for key in modules))
            connection.executemany("INSERT INTO drafts VALUES(?, ?, ?)", (
                (identity if identity in modules else "reload-" + new_id(), revision, encode(data))
                for identity, revision, data in restore["drafts"]
            ))
        draft = records.get("__draft__")
        if draft:
            identity, revision, data = draft
            connection.execute("""
                INSERT INTO drafts VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, data=excluded.data
                WHERE excluded.revision > drafts.revision
            """, (identity, revision, encode(data)))
        connection.executemany(
            "INSERT INTO records VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            ((key, encode(value)) for key, value in records.items() if key not in ("__restore__", "__draft__")),
        )

    def set(self, key, value):
        self.set_records({key: value})

    def set_records(self, records):
        with self._lock, self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_no_intent(connection)
            self._verify_draft_revision(connection, records)
            self._write_records(connection, records)
            connection.commit()

    def save_draft(self, identity, revision, document, anchor, cursor, *, records=None):
        document.validate()
        if not (0 <= anchor <= len(document.text) and 0 <= cursor <= len(document.text)):
            raise ValueError("Invalid saved selection")
        record = {"document": document.to_record(), "anchor": anchor, "cursor": cursor}
        with self._lock, self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if records is not None:
                self._require_no_intent(connection)
            pending = connection.execute("SELECT records FROM library_intent").fetchone()
            draft = json.loads(pending[0]).get("__draft__") if pending else None
            if draft and draft[0] == identity:
                raise CompositionStorageError("草稿有待恢复的词库事务")
            connection.execute("""
                INSERT INTO drafts VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, data=excluded.data
                WHERE excluded.revision > drafts.revision
            """, (identity, revision, encode(record)))
            if records is not None:
                self._write_records(connection, records)
            connection.commit()

    def draft_ids(self):
        with self._open() as connection:
            return tuple(row[0] for row in connection.execute("SELECT id FROM drafts ORDER BY rowid DESC"))

    def load_draft(self, identity):
        with self._open() as connection:
            row = connection.execute("SELECT revision, data FROM drafts WHERE id=?", (identity,)).fetchone()
        if row is None:
            return 0, Document(), 0, 0
        revision, record = row
        stored = json.loads(record)
        document = Document.load(stored["document"])
        anchor, cursor = stored["anchor"], stored["cursor"]
        if not (0 <= anchor <= len(document.text) and 0 <= cursor <= len(document.text)):
            raise CompositionStorageError("无法恢复无效的草稿选区")
        return revision, document, anchor, cursor

    @staticmethod
    def _require_no_intent(connection):
        if connection.execute("SELECT 1 FROM library_intent").fetchone():
            raise LibraryConflict("存在待恢复的组合输入词库操作")

    def library_pending(self):
        with self._open() as connection:
            return connection.execute("SELECT 1 FROM library_intent").fetchone() is not None

    def publish_library(self, expected, after, records):
        after.decode("utf-8")
        with self._lock, self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_no_intent(connection)
            if read_library(self.library_path) != expected:
                raise LibraryConflict("专用词库在操作期间被修改")
            self._verify_draft_revision(connection, records)
            if expected == after:
                self._write_records(connection, records)
                connection.commit()
                return
            connection.execute("INSERT INTO library_intent VALUES (1, ?, ?, ?)",
                               (expected, after, encode(records)))
            connection.commit()
            self._publish(self.library_path, expected, after)
            if read_library(self.library_path) != after:
                raise LibraryConflict("词库写入后出现外部版本")
            self._complete(connection, records)

    @staticmethod
    def _verify_draft_revision(connection, records):
        draft = records.get("__draft__")
        if draft:
            current = connection.execute("SELECT revision FROM drafts WHERE id = ?", (draft[0],)).fetchone()
            if current and current[0] >= draft[1]:
                raise CompositionStorageError("草稿已被其他操作更新")

    @classmethod
    def _complete(cls, connection, records):
        connection.execute("BEGIN IMMEDIATE")
        cls._verify_draft_revision(connection, records)
        cls._write_records(connection, records)
        connection.execute("DELETE FROM library_intent")
        connection.commit()

    def recover_library(self):
        with self._lock, self._open() as connection:
            entry = connection.execute("SELECT before, after, records FROM library_intent").fetchone()
            if entry is None:
                return "clean"
            before, after, records = entry
            current = read_library(self.library_path)
            displaced = displaced_path(self.library_path)
            saved = read_library(displaced)
            if saved is not None:
                if saved != before:
                    if sys.platform == "win32" and current == after:
                        restore_displaced(self.library_path, displaced, after)
                    raise LibraryConflict("检测到替换时发生的外部写入，原版本已保留")
                displaced.unlink()
            if current == after:
                self._complete(connection, json.loads(records))
                return "completed"
            if current == before:
                connection.execute("DELETE FROM library_intent")
                return "rolled_back"
            raise LibraryConflict("专用词库存在外部变更，待恢复记录已保留")

    def retain_external_library(self):
        """Archive an unresolved intent before releasing the current external version."""
        with self._lock, self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT before, after, records FROM library_intent").fetchone()
            if row is None:
                return False
            before, proposed, records = row
            archive = {
                "before_hex": before.hex() if before is not None else None,
                "proposed_hex": proposed.hex(), "records": json.loads(records),
            }
            displaced = displaced_path(self.library_path)
            if displaced.exists():
                archived = displaced.with_name(f".composition-{new_id()}.composition-recovery")
                displaced.rename(archived)
            self._write_records(connection, {"recovery:" + new_id(): archive})
            connection.execute("DELETE FROM library_intent")
            connection.commit()
            return True

    def add_word(self, text, records):
        self.add_words((text,), records)

    def add_words(self, words, records):
        entries = []
        for text in dict.fromkeys(words):
            if (not text or text.strip() != text or any(char in text for char in "\r\n\u2029")
                    or "\x60\x60" in text):
                raise ValueError("普通词汇须为单行，并符合 Markdown 词库格式")
            entries.append("- " + text)
        with self._lock:
            before = read_library(self.library_path)
            current = (before or b"").decode("utf-8")
            present = {line.rstrip() for line in current.splitlines()}
            missing = [entry.encode("utf-8") for entry in entries if entry not in present]
            after = before
            if missing:
                newline = b"\r\n" if b"\r\n" in (before or b"") else b"\n"
                prefix = newline if before and not before.endswith((b"\r", b"\n")) else b""
                after = (before or b"") + prefix + newline.join(missing) + newline
            self.publish_library(before, after, records)
