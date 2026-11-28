import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.template_renderer import TemplateRenderer, TemplateRenderError


class TemplateRendererTests(unittest.TestCase):
    def setUp(self):
        self.renderer = TemplateRenderer()

    def test_date_time_and_ordered_filters_match_supported_output(self):
        renderer = self.renderer
        prepared = renderer.prepare("{{今天}} | {{现在}} | {{今天|格式化:%Y/%m}} | {{现在|格式化:%H:%M}}")
        with patch("core.template_renderer.date", type("FixedDate", (date,), {
            "today": classmethod(lambda cls: cls(2026, 4, 2)),
        })), patch("core.template_renderer.datetime", type("FixedDateTime", (datetime,), {
            "now": classmethod(lambda cls: cls(2026, 4, 2, 8, 9)),
        })):
            self.assertEqual(renderer.render(prepared), "2026-04-02 | 2026-04-02 08:09 | 2026/04 | 08:09")

    def test_repeated_form_fields_and_default_filter(self):
        prepared = self.renderer.prepare(
            "- 项目：{{输入:项目}}\r\n\t{{输入:备注|默认: note |大写|去空格}} / {{输入:项目|小写}}"
        )
        self.assertEqual([(f.label, f.default) for f in prepared.input_fields], [("项目", ""), ("备注", "note")])
        output = self.renderer.render(prepared, {"input_0": "Alpha"})
        self.assertEqual(output, "- 项目：Alpha\r\n\tNOTE / alpha")
        self.assertFalse(prepared.uses_clipboard)

    def test_only_generated_expressions_enter_engine(self):
        renderer = self.renderer
        text = "文档\t" + "{% documentation marker %}" + "\r\n{{剪贴板|去空格|小写}}\nplain"
        with patch.object(renderer.environment, "from_string", wraps=renderer.environment.from_string) as compiler:
            prepared = renderer.prepare(text)
            self.assertEqual([call.args[0] for call in compiler.call_args_list],
                             ["{{ clipboard | 去空格 | 小写 }}"])
            self.assertEqual(renderer.render(prepared, clipboard_text=" ABC "), text.replace(
                "{{剪贴板|去空格|小写}}", "abc"))
            self.assertEqual(compiler.call_count, 1)
        self.assertEqual(set(renderer.environment.tags), {"content", "output", "illegal"})
        self.assertEqual(set(renderer.environment.filters), renderer.SUPPORTED_FILTERS)

    def test_plain_and_provided_values_remain_data(self):
        renderer = self.renderer
        plain = "正文\r\n\t" + "{% documentation marker %}" + "\n" + "长" * 1_000_000
        self.assertEqual(renderer.render(renderer.prepare(plain)), plain)
        template = renderer.prepare("{{输入:测试}}/{{剪贴板}}")
        self.assertTrue(template.uses_clipboard)
        self.assertEqual(renderer.render(template, {"input_0": "{{今天}}"}, "{{现在}}"), "{{今天}}/{{现在}}")

    def test_resource_budget_excludes_surrounding_literal_text(self):
        renderer = self.renderer
        with patch.object(renderer, "MAX_DYNAMIC_OUTPUT", 4):
            self.assertEqual(renderer.render(renderer.prepare("literal:" + "{{剪贴板}}"), clipboard_text="a"),
                             "literal:a")
            with self.assertRaises(TemplateRenderError):
                renderer.render(renderer.prepare("{{剪贴板}}{{剪贴板}}"), clipboard_text="abc")
        with patch.object(renderer, "MAX_EXPRESSIONS", 2):
            with self.assertRaises(TemplateRenderError):
                renderer.prepare("{{今天}}" * 3)

    def test_filter_arguments_do_not_become_template_source(self):
        renderer = self.renderer
        template = renderer.prepare('{{剪贴板|默认:\'Quote "here" 100%\'}}')
        self.assertEqual(renderer.render(template), 'Quote "here" 100%')
        template = renderer.prepare(r'{{剪贴板|默认:C:\demo}}')
        self.assertEqual(renderer.render(template), r'C:\\demo')

    def test_markdown_word_output_expands_then_transcribes_or_cancels(self):
        from main_controller import MainController
        from core.word_source import WordSource
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.md"
            path.write_text("- Example {{剪贴板|大写}}\n- Invalid {{unknown_field}}\n", encoding="utf8")
            first, invalid = WordSource(str(path)).word_blocks
            clipboard = Mock()
            clipboard.mimeData.return_value.hasText.return_value = True
            clipboard.text.return_value = "fixture"
            controller = SimpleNamespace(
                template_renderer=self.renderer, app=Mock(), popup=None, pending_paste_request=None,
                settings=SimpleNamespace(transcription_enabled=True, transcription_rules='"FIXTURE" => "done"'),
                _find_block_by_full_content=lambda text: {b["full_content"]: b for b in (first, invalid)}[text],
                record_entry_usage=Mock(), ignore_next_clipboard_change=False, last_popup_target_hwnd=0,
                _verify_paste_clipboard=Mock(return_value=42), schedule_paste=Mock(),
            )
            controller.app.clipboard.return_value = clipboard
            for name in ("_build_output_content", "render_template_output", "_transcribe_output"):
                setattr(controller, name, types.MethodType(getattr(MainController, name), controller))
            with patch("main_controller.pyperclip.copy") as copy, patch("main_controller.QMessageBox.warning") as warning:
                MainController.on_suggestion_selected(controller, first["full_content"])
                copy.assert_called_once_with("Example done")
                MainController.on_suggestion_selected(controller, invalid["full_content"])
                copy.assert_called_once()
                warning.assert_called_once()
                self.assertNotIn("unknown_field", warning.call_args.args[-1])

    def test_invalid_variables_and_errors_keep_input_out_of_diagnostics(self):
        marker = "fictional_unknown"
        with self.assertRaises(TemplateRenderError) as error:
            self.renderer.prepare("{{" + marker + "}}")
        self.assertNotIn(marker, str(error.exception))

    def test_controller_reads_clipboard_only_after_successful_validation_and_consent(self):
        from main_controller import MainController
        clipboard = Mock()
        clipboard.mimeData.return_value.hasText.return_value = True
        clipboard.text.return_value = "fictional"
        controller = SimpleNamespace(template_renderer=self.renderer, app=Mock(), popup=None,
                                     settings=SimpleNamespace(theme="light", font_size=14))
        controller.app.clipboard.return_value = clipboard
        output = MainController.render_template_output
        self.assertEqual(output(controller, "{{今天|格式化:%Y}}"), str(date.today().year))
        controller.app.clipboard.assert_not_called()
        self.assertEqual(output(controller, "{{剪贴板|大写}}"), "FICTIONAL")
        clipboard.text.assert_called_once()
        controller.app.clipboard.reset_mock()
        with patch("main_controller.TemplateInputDialog") as dialog:
            dialog.return_value.exec.return_value = 0
            self.assertIsNone(output(controller, "{{输入:项目}} {{剪贴板}}"))
        controller.app.clipboard.assert_not_called()
