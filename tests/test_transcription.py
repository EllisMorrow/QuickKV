import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from core.transcription import parse_rules, transcribe, TranscriptionError


class TranscriptionTests(unittest.TestCase):
    def test_longest_literal_single_pass(self):
        rules = '\n'.join(['"a" => "b"', '"ab" => "X"', '"b" => "c"', '".*" => "!"'])
        self.assertEqual('Xb c !', transcribe('aba b .*', rules))

    def test_deletion_escapes_comments_and_empty_rules(self):
        rules = r'"\n" => "\t"' + '\n# 注释\n"删" => ""\n'
        self.assertEqual('a\tb', transcribe('a\n删b', rules))
        self.assertEqual('原文', transcribe('原文', ''))

    def test_invalid_rules_report_line(self):
        for source in ['"" => "a"', '"a" => 1', 'a => b', '"a" -> "b"', '"a" => "b" extra', '"a" => "b"\n"a" => "c"']:
            with self.subTest(source=source), self.assertRaisesRegex(TranscriptionError, '第 [12] 行'):
                parse_rules(source)

    def test_removed_rule_stops_applying(self):
        self.assertEqual('b', transcribe('a', '"a" => "b"'))
        self.assertEqual('a', transcribe('a', ''))
