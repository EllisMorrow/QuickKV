
"""Validated portable backup archives for composition records and their Markdown."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from core.composition.document import Document, new_id
from core.composition.groups import Catalog
from core.composition.store import CompositionStorageError, digest, encode, read_library


FORMAT_VERSION = 1
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MEMBERS = ("manifest.json", "configuration.json", "组合输入.md")
KINDS = {"automatic", "manual", "protected", "pinned"}


class Backups:
    def __init__(self, store, directory):
        self.store = store
        self.directory = Path(directory)

    def _snapshot(self, include_drafts):
        store = self.store
        with store._lock, store._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            store._require_no_intent(connection)
            library = read_library(store.library_path)
            records = {key: json.loads(value) for key, value in connection.execute("SELECT key, data FROM records")}
            drafts = []
            if include_drafts:
                drafts = [[key, revision, json.loads(value)] for key, revision, value in
                          connection.execute("SELECT id, revision, data FROM drafts ORDER BY id")]
            records.pop("active_session", None)
            records = {key: value for key, value in records.items() if not key.startswith("recovery:")}
            if read_library(store.library_path) != library:
                raise CompositionStorageError("词库在备份时已发生变化")
        return {"records": records, "drafts": drafts}, library or b""

    def create(self, name, kind="manual", *, include_drafts=False, keep=5, minimum_interval=900):
        if kind not in KINDS:
            raise ValueError("Invalid backup type")
        configuration, library = self._snapshot(include_drafts)
        now = datetime.now(timezone.utc)
        payload = (encode(configuration).encode("utf8"), library)
        metadata = {"version": FORMAT_VERSION, "name": name, "kind": kind,
                    "created": now.isoformat(), "includes_drafts": include_drafts,
                    "sha256": {key: digest(data) for key, data in zip(MEMBERS[1:], payload)}}
        previous = self.entries()
        if kind == "automatic":
            automatic = [(path, entry) for path, entry in previous if entry["kind"] == "automatic"]
            if any(entry["sha256"] == metadata["sha256"] for _, entry in automatic):
                return None
            if automatic and (now - datetime.fromisoformat(automatic[0][1]["created"])).total_seconds() < minimum_interval:
                return None
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / (now.strftime("%Y%m%d-%H%M%S") + "-" + new_id()[:8] + ".quickkv-backup")
        self._write(destination, metadata, payload)
        if kind == "automatic":
            valid = [(path, entry) for path, entry in self.entries() if entry["kind"] == "automatic"]
            for path, _ in valid[max(1, int(keep)):]:
                path.unlink()
        return destination

    @staticmethod
    def _write(destination, metadata, payload):
        destination = Path(destination)
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".backup-", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
                archive.writestr(MEMBERS[0], encode(metadata).encode("utf8"))
                for key, data in zip(MEMBERS[1:], payload):
                    archive.writestr(key, data)
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            Backups.read(temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def read(path):
        try:
            with ZipFile(path) as archive:
                items = archive.infolist()
                if (sorted(x.filename for x in items) != sorted(MEMBERS)
                        or any(x.file_size > MAX_MEMBER_BYTES for x in items)):
                    raise ValueError("Invalid contents")
                meta, raw, library = (archive.read(name) for name in MEMBERS)
            metadata = json.loads(meta)
            if metadata["version"] != FORMAT_VERSION or metadata["kind"] not in KINDS:
                raise ValueError("Invalid backup format")
            if datetime.fromisoformat(metadata["created"]).tzinfo is None:
                raise ValueError("Missing timezone")
            if any(digest(data) != metadata["sha256"][key] for key, data in zip(MEMBERS[1:], (raw, library))):
                raise ValueError("Incorrect backup checksum")
            configuration = json.loads(raw)
            records = configuration["records"]
            if not isinstance(records, dict) or {"__restore__", "__draft__"}.intersection(records):
                raise ValueError("Invalid backup settings")
            Catalog.load(records.get("catalog"))
            ignored = records.get("ignored_candidates", [])
            if not isinstance(ignored, list) or any(
                    not isinstance(item, list) or len(item) != 4
                    or not isinstance(item[0], str) or not isinstance(item[1], str)
                    or type(item[2]) is not int or item[2] < 0 or not isinstance(item[3], str)
                    for item in ignored):
                raise ValueError("Invalid ignored candidates")
            favorites = records.get("group_favorites", {})
            if not isinstance(favorites, dict) or any(
                    role not in ("main", "prefixes", "suffixes") or not isinstance(values, list)
                    or not all(isinstance(identity, str) for identity in values)
                    for role, values in favorites.items()):
                raise ValueError("Invalid group favorites")
            for key in ("recognition", "observations", "backup_drafts", "hover",
                        "transcribe_new_drafts", "continuous_suggestions"):
                if key in records and type(records[key]) is not bool:
                    raise ValueError("Invalid preference")
            keep = records.get("backup_keep", 5)
            if type(keep) is not int or not 1 <= keep <= 100:
                raise ValueError("Invalid backup retention")
            for template in records.get("templates", {}).values():
                if not isinstance(template["name"], str) or not isinstance(template["module"], str):
                    raise ValueError("Invalid template")
                Document.load(template["fragment"])
            for frame in records.get("frameworks", {}).values():
                if (not isinstance(frame["name"], str) or not isinstance(frame["paths"], list)
                        or not all(isinstance(path, str) for path in frame["paths"])):
                    raise ValueError("Invalid observation framework")
            identities = set()
            for identity, revision, record in configuration["drafts"]:
                if not isinstance(identity, str) or identity in identities or not isinstance(revision, int):
                    raise ValueError("Invalid draft identity")
                identities.add(identity)
                document = Document.load(record["document"])
                if not all(isinstance(record[key], int) and 0 <= record[key] <= len(document.text)
                           for key in ("anchor", "cursor")):
                    raise ValueError("Invalid draft caret")
            library.decode("utf8")
            return metadata, configuration, library
        except (BadZipFile, ValueError, UnicodeError, KeyError, TypeError, RuntimeError, AttributeError) as error:
            raise CompositionStorageError("备份格式、校验或数据完整性检查失败") from error

    def entries(self):
        entries = []
        if self.directory.is_dir():
            for candidate in self.directory.glob("*.quickkv-backup"):
                try:
                    metadata, _, _ = self.read(candidate)
                except (CompositionStorageError, OSError):
                    continue
                entries.append((candidate, metadata))
        return sorted(entries, key=lambda value: value[1]["created"], reverse=True)

    def pin(self, path, name=None):
        metadata, config, library = self.read(path)
        metadata["kind"] = "pinned"
        if name:
            metadata["name"] = name
        payload = (encode(config).encode("utf8"), library)
        metadata["sha256"] = {key: digest(data) for key, data in zip(MEMBERS[1:], payload)}
        self._write(path, metadata, payload)

    def restore(self, path, *, include_drafts=False):
        metadata, config, library = self.read(path)
        self.create("恢复前", "protected", include_drafts=True)
        store = self.store
        with store._lock:
            records = dict(config["records"])
            records.pop("active_session", None)
            modules = Catalog.load(records.get("catalog"))
            module_ids = {module.id for module in modules.modules}
            if include_drafts:
                records["active_session"] = modules.selected_module
            records["__restore__"] = {
                "drafts": config["drafts"] if include_drafts else [],
                "modules": sorted(module_ids) if include_drafts else [],
            }
            store.publish_library(read_library(store.library_path), library, records)
        return metadata
