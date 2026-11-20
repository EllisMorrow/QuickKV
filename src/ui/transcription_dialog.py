from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPlainTextEdit, QCheckBox, QPushButton, QMessageBox)
from core.transcription import parse_rules, transcribe, TranscriptionError


class TranscriptionDialog(QDialog):
    def __init__(self, parent, settings, theme, save_settings):
        super().__init__(parent)
        self.setWindowTitle("转写功能")
        self.resize(680, 650)
        self.save_settings = save_settings
        self.saved_state = (settings.transcription_enabled, settings.transcription_rules)
        self.setStyleSheet(
            f"QDialog, QLabel, QCheckBox {{ background-color: {theme['bg_color']}; color: {theme['text_color']}; }}"
            f"QPlainTextEdit {{ background-color: {theme['input_bg_color']}; color: {theme['text_color']}; }}")
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox("启用转写（搜索选词和快捷码的最终输出）")
        self.enabled.setChecked(settings.transcription_enabled)
        layout.addWidget(self.enabled)
        help_label = QLabel(
            '每行一条："原字符或词组" => "目标字符或词组"\n'
            '例如："，" => ","    或    "您好" => "你好"\n'
            '目标写 "" 表示删除；空行和 # 开头的注释忽略。\n'
            '双引号内支持 JSON 转义：\\n 换行、\\t 制表符、\\" 双引号、\\\\ 反斜杠。\n'
            '区分大小写，按原文从左到右匹配，长词优先，替换结果不重复转写。\n'
            '模板展开后转写；不修改词库或历史原文。修改后点击“应用”立即生效，清空后应用即移除全部规则。')
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.rules = QPlainTextEdit(settings.transcription_rules)
        self.rules.setPlaceholderText('"，" => ","\n"。" => "."')
        layout.addWidget(self.rules, 2)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(QLabel("测试原文（仅预览，不会发送或修改剪贴板）："))
        self.sample = QPlainTextEdit()
        self.sample.setPlaceholderText("在此输入文本，实时查看转写结果")
        layout.addWidget(self.sample, 1)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        buttons = QHBoxLayout()
        self.saved_label = QLabel()
        buttons.addWidget(self.saved_label, 1)
        self.apply_button = QPushButton("应用")
        self.apply_button.clicked.connect(self.apply)
        buttons.addWidget(self.apply_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.rules.textChanged.connect(self.refresh)
        self.sample.textChanged.connect(self.refresh)
        self.enabled.toggled.connect(self.refresh)
        self.refresh()

    def state(self):
        return self.enabled.isChecked(), self.rules.toPlainText()

    def refresh(self):
        enabled, source = self.state()
        self.saved_label.setText("有未应用的修改" if self.state() != self.saved_state else "已应用")
        try:
            rules = parse_rules(source)
            self.status.setText(f"规则有效：{len(rules)} 条" + ("（已停用）" if not enabled else ""))
            self.preview.setPlainText(transcribe(self.sample.toPlainText(), source) if enabled else self.sample.toPlainText())
            self.apply_button.setEnabled(True)
        except TranscriptionError as error:
            self.status.setText(str(error))
            self.preview.clear()
            self.apply_button.setEnabled(False)

    def apply(self):
        enabled, source = self.state()
        try:
            parse_rules(source)
        except TranscriptionError as error:
            self.status.setText(str(error))
            return False
        if not self.save_settings(enabled, source):
            return False
        self.saved_state = self.state()
        self.refresh()
        return True

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self.state() != self.saved_state:
            answer = QMessageBox.question(
                self, "未应用的修改", "是否应用修改？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Cancel)
            if answer == QMessageBox.Cancel or (answer == QMessageBox.Save and not self.apply()):
                event.ignore()
                return
        event.accept()
