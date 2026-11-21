from html import escape
from itertools import groupby


def match_colors(groups, theme):
    palette = (theme["highlight_color"], "#e5c07b", "#c678dd", "#98c379", "#e06c75")
    return {
        index: palette[group % len(palette)]
        for group, indices in reversed(list(groups.items()))
        for index in indices
    }


def highlighted_html(text, colors, base_color, *, offset=0, anchor=False):
    parts = [f'<div style="color:{base_color}; white-space:pre-wrap;">']
    for color, run in groupby(enumerate(text, offset), key=lambda pair: colors.get(pair[0])):
        content = escape("".join(char for _, char in run)).replace("\n", "<br>")
        if color:
            if anchor:
                parts.append('<a name="match"></a>')
                anchor = False
            parts.append(f'<span style="color:{color};">{content}</span>')
        else:
            parts.append(content)
    parts.append("</div>")
    return "".join(parts)


def match_excerpt(text, hits, width, measure):
    """Keep the visible match near the middle, using pixel width for mixed scripts."""
    if measure(text) <= width:
        return text, 0
    hits = sorted(i for i in hits if 0 <= i < len(text))
    pivot = hits[0] if hits else 0
    if hits and measure(text[hits[0]:hits[-1] + 1]) + measure("……") <= width:
        pivot = (hits[0] + hits[-1]) // 2

    def excerpt(length):
        start = max(0, min(pivot - length // 2, len(text) - length))
        end = min(len(text), start + length)
        prefix = "…" if start else ""
        suffix = "…" if end < len(text) else ""
        return prefix + text[start:end] + suffix, start - len(prefix)

    low, high = 1, len(text) - 1
    best = ("…", -1)
    while low <= high:
        size = (low + high) // 2
        candidate = excerpt(size)
        if measure(candidate[0]) <= width:
            best = candidate
            low = size + 1
        else:
            high = size - 1
    return best
