"""Shared Qt menu styling, including top-level tray submenus."""


def menu_style(theme, font_size):
    return f"""
    QMenu {{
        background-color: {theme['bg_color']};
        border: 1px solid {theme['border_color']};
        border-radius: 4px;
        color: {theme['text_color']};
        font-size: {font_size}px;
        padding: 4px;
    }}
    QMenu::item {{
        padding: 6px 24px 6px 24px;
        border-radius: 4px;
        background-color: transparent;
        color: {theme['text_color']};
    }}
    QMenu::item:selected {{
        background-color: {theme['item_selected_bg']};
        color: {theme['item_selected_text']};
    }}
    QMenu::item:disabled {{
        color: #888;
    }}
    QMenu::separator {{
        height: 1px;
        background-color: {theme['border_color']};
        margin: 4px 0px;
    }}
    QMenu::indicator {{
        width: 13px;
        height: 13px;
        left: 6px;
    }}
    """
