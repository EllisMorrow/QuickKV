
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.create_composition_demo import seed
from core.composition.backups import Backups
from core.composition.groups import Catalog, TIMESTAMP_ID
from core.composition.store import CompositionStore, CompositionStorageError
from core.composition.structure import dimension_status


class CompositionDemoTests(unittest.TestCase):
    def test_portable_fixture_has_editable_drafts_templates_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            groups, words = seed(root)
            self.assertGreaterEqual(groups, 25)
            self.assertGreaterEqual(words, 150)
            store = CompositionStore(root / "用户数据/组合输入.sqlite3",
                                     root / "MD词库-需自动载入的请放入/组合输入.md")
            catalog = Catalog.load(store.get("catalog"))
            self.assertEqual(len(catalog.modules), 5)
            self.assertFalse(store.load_draft("round4")[1].tokens[0].main.group_id)
            self.assertEqual(sum(g.name == "校准标签" for g in catalog.groups), 2)
            self.assertEqual(len(catalog.group(TIMESTAMP_ID).options), 10)
            self.assertEqual(len(store.get("templates")), 6)
            states = set()
            frames = store.get("frameworks")
            for identity in ("project", "study", "daily"):
                for draft in (identity, f"reload-{identity}-example"):
                    _, model, _, _ = store.load_draft(draft)
                    token, = model.tokens
                    self.assertEqual(model.text[token.start:token.end], token.render())
                    self.assertGreater(len(catalog.group(token.main.group_id).prefixes), 5)
                    self.assertTrue(model.nodes[6].observation)
                    states.update(value for _, value, _ in
                                  dimension_status(model, 0, frames[model.nodes[0].observation]["paths"]))
            self.assertEqual(states, {"recorded", "pending", "unknown", "na"})
            service = Backups(store, root / "用户数据/组合输入备份")
            backup, = service.entries()
            self.assertTrue(backup[1]["includes_drafts"])
            bad, = (root / "异常示例").iterdir()
            original = store.library_path.read_bytes()
            with self.assertRaises(CompositionStorageError):
                service.restore(bad)
            self.assertEqual(store.library_path.read_bytes(), original)

    def test_write_denial_leaves_a_recoverable_intent_and_the_old_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library.md"
            library.write_bytes(b"- museum\n")

            def denied(*_):
                raise PermissionError("simulated overwrite denial")

            store = CompositionStore(root / "demo.sqlite3", library, publish=denied)
            with self.assertRaises(PermissionError):
                store.add_word("archive", {"settings": {"updated": True}})
            self.assertEqual(library.read_bytes(), b"- museum\n")
            self.assertTrue(store.library_pending())
            self.assertEqual(store.recover_library(), "rolled_back")
            self.assertIsNone(store.get("settings"))

    def test_future_schema_remains_intact_after_rejected_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "future.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA user_version = 999")
                db.execute("CREATE TABLE future_data (value TEXT)")
                db.execute("INSERT INTO future_data VALUES ('example')")
                db.commit()
            original = path.read_bytes()
            with self.assertRaises(CompositionStorageError):
                CompositionStore(path, root / "library.md")
            self.assertEqual(path.read_bytes(), original)
