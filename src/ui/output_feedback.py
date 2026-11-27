"""Shared non-layout output controls and feedback."""

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QLabel, QToolTip


class OutputFeedback(QLabel):
    def __init__(self, footer):
        super().__init__(footer)
        self.setTextFormat(Qt.PlainText)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setMargin(3)
        self.timeout = QTimer(self, singleShot=True)
        self.timeout.timeout.connect(self.hide)
        footer.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() in (QEvent.LayoutRequest, QEvent.Resize):
            self.reposition()
        return super().eventFilter(watched, event)

    def reposition(self):
        footer = self.parentWidget()
        layout = footer.layout()
        if layout and layout.count():
            space = next((layout.itemAt(i).geometry() for i in range(layout.count())
                          if layout.itemAt(i).spacerItem()), footer.rect())
            self.setGeometry(space)

    def display(self, text, persistent=False):
        self.setText(text)
        if not self.parentWidget().isVisible() and text == "已发送落字":
            QToolTip.showText(self.parentWidget().mapToGlobal(self.parentWidget().rect().topLeft()),
                              text, None, msecShowTime=1600)
            return
        self.setToolTip(text)
        self.reposition()
        self.raise_()
        self.show()
        self.timeout.stop()
        if not persistent:
            self.timeout.start(2200)
