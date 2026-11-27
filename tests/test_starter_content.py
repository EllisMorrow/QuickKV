import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.composition.groups import Catalog
from core.composition.store import CompositionStore
from core.starter_content import initialize_starter_content
from core.word_source import WordSource


class StarterContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = root / "data/config.ini"
        self.words = root / "library/词库.md"
        self.database = root / "data/组合输入.sqlite3"
        self.special = root / "library/组合输入.md"
        self.paths = (self.config, self.words, self.database, self.special)

    def test_empty_installation_has_real_words_and_structured_examples_once(self):
        self.assertTrue(initialize_starter_content(*self.paths))
        source = WordSource(str(self.special))
        source.load()
        self.assertEqual(len(source.word_blocks), 216)
        self.assertEqual(source.word_blocks[0]["parent"], "收到，谢谢！")
        store = CompositionStore(self.database, self.special)
        catalog = Catalog.load(store.get("catalog"))
        self.assertEqual(catalog.module().name, "日常速记")
        self.assertEqual(len(store.load_draft(catalog.selected_module)[1].tokens), 2)
        self.assertEqual(len(store.get("templates")), 14)
        self.assertEqual(len(catalog.modules), 8)
        self.assertEqual(len(catalog.groups), 29)
        self.assertEqual(store.get("starter_state"), "complete")
        original = self.database.read_bytes()
        self.special.unlink()
        self.assertFalse(initialize_starter_content(*self.paths))
        self.assertFalse(self.special.exists())
        self.words.write_text("- 用户编辑", encoding="utf8")
        self.assertFalse(initialize_starter_content(*self.paths))
        self.assertEqual(self.words.read_text(encoding="utf8"), "- 用户编辑")
        self.assertEqual(self.database.read_bytes(), original)

    def test_interrupted_first_run_resumes_only_marked_database(self):
        with patch.object(CompositionStore, "publish_library", side_effect=OSError("isolated write failure")):
            with self.assertRaises(OSError):
                initialize_starter_content(*self.paths)
        self.assertFalse(self.special.exists())
        self.assertTrue(initialize_starter_content(*self.paths))
        store = CompositionStore(self.database, self.special)
        self.assertEqual(store.get("starter_state"), "complete")

    def test_existing_database_without_marker_is_left_byte_identical(self):
        store = CompositionStore(self.database, self.special)
        store.set("custom", "preserve this record")
        before = self.database.read_bytes()
        self.assertFalse(initialize_starter_content(*self.paths))
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse(self.special.exists())

    def test_existing_config_or_other_markdown_skips_startup_seeding(self):
        self.config.parent.mkdir()
        self.config.write_bytes(b"[General]")
        self.assertFalse(initialize_starter_content(*self.paths))
        self.config.unlink()
        self.words.parent.mkdir()
        custom = self.words.parent / "现有词库.md"
        custom.write_bytes(b"- user")
        self.assertFalse(initialize_starter_content(*self.paths))
        self.assertFalse(self.database.exists())
        self.assertEqual(custom.read_bytes(), b"- user")
