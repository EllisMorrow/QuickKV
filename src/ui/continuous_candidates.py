from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QTextDocument, QTextOption
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from core.config import THEMES
from ui.candidate_text import highlighted_html, match_colors, match_excerpt


def presentation(block, settings):
    text = block["full_content"]
    leading = 2 if text.startswith("- ") else 0
    groups = block.get("highlight_groups", {})
    colors = match_colors(groups, THEMES[settings.theme]) if getattr(settings, "highlight_matches", True) else {}
    return text[leading:], leading, colors


class ContinuousCandidateDelegate(QStyledItemDelegate):
    def __init__(self, settings, parent):
        super().__init__(parent)
        self.settings = settings

    def sizeHint(self, option, index):
        return QSize(1, option.fontMetrics.height() + 12)

    def paint(self, painter, option, index):
        block = index.data(Qt.UserRole)
        if not block:
            return
        theme = THEMES[self.settings.theme]
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        base = theme["item_selected_text" if selected else "text_color"]
        background = theme["item_selected_bg" if selected else "item_hover_bg" if hovered else "bg_color"]
        rect = option.rect
        painter.save()
        painter.setClipRect(rect)
        painter.fillRect(rect, QColor(background))
        width = max(0, rect.width() - 16)
        if getattr(self.settings, "show_source_enabled", False) and block.get("source_path"):
            source = Path(block["source_path"]).stem
            if block.get("is_favorite"):
                source = "★ " + source
            badge_font = option.font
            if badge_font.pixelSize() > 0:
                badge_font.setPixelSize(max(9, int(badge_font.pixelSize() * .83)))
            elif badge_font.pointSizeF() > 0:
                badge_font.setPointSizeF(max(7, badge_font.pointSizeF() * .83))
            metrics = QFontMetrics(badge_font)
            source = metrics.elidedText(source, Qt.ElideRight, int(width * .31))
            badge_width = metrics.horizontalAdvance(source) + 10
            badge = QRectF(rect.right() - badge_width - 5, rect.center().y() - (metrics.height() + 4) / 2,
                           badge_width, metrics.height() + 4)
            painter.setFont(badge_font)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme["input_bg_color"]))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor(base))
            painter.drawText(badge, Qt.AlignCenter, source)
            width = max(0, width - badge_width - 5)

        text, leading, colors = presentation(block, self.settings)
        # Preserve source offsets while flattening line breaks for the compact row.
        line = text.partition("\n")[0].replace("\t", " ")
        hits = [position - leading for group in block.get("highlight_groups", {}).values() for position in group]
        excerpt, offset = match_excerpt(line, hits, width, option.fontMetrics.horizontalAdvance)
        start = leading + offset
        visible = {
            i: color for i, color in colors.items()
            if start <= i < start + len(excerpt) and excerpt[i - start] != "…"
        }
        doc = QTextDocument()
        doc.setDefaultFont(option.font)
        doc.setDocumentMargin(0)
        layout = QTextOption()
        layout.setWrapMode(QTextOption.NoWrap)
        doc.setDefaultTextOption(layout)
        doc.setHtml(highlighted_html(excerpt, visible, base, offset=start))
        painter.translate(rect.left() + 8, rect.top() + (rect.height() - doc.size().height()) / 2)
        doc.drawContents(painter, QRectF(0, 0, width, doc.size().height()))
        painter.restore()
