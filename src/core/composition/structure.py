
"""Templates and observation dimensions derived from outline text."""

from dataclasses import replace
import re

from core.composition.document import Document, project


KEY_START = re.compile(r"(?:^|[，,；;])\s*([^，,；;:：\n]{1,36})[:：]\s*")
MARKER = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")


def field_spans(text):
    marker = MARKER.match(text)
    offset = marker.end() if marker else 0
    content = text[offset:]
    found = [m for m in KEY_START.finditer(content) if not m[1].strip().isdigit()]
    for index, field in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(content)
        end = field.end() + len(content[field.end():end].rstrip())
        yield field[1].strip(), offset + field.end(), offset + end


def fields(text):
    return tuple((name, text[start:end].strip()) for name, start, end in field_spans(text))


def outline_tree(document, root):
    end = document.branch_bounds(root)[1]
    hierarchy = []
    for index in range(root + 1, len(document.nodes)):
        node = document.nodes[index]
        if node.start >= end:
            break
        item = document.list_item(index)
        if not item:
            continue
        depth = len(item["indent"].expandtabs(4))
        while hierarchy and hierarchy[-1][0] >= depth:
            hierarchy.pop()
        if any(entry[3] for entry in hierarchy):
            continue
        start, stop = document.line_bounds(index)
        entries = fields(document.text[start:stop].rstrip("\n"))
        primary = entries[0][0] if entries else ""
        parents = tuple(entry[1] for entry in hierarchy if entry[1])
        independent = bool(node.observation)
        hierarchy.append((depth, primary, index, independent))
        if not independent:
            yield index, parents, entries


def dimension_status(document, root, paths):
    rows = list(outline_tree(document, root))
    statuses = dict(document.nodes[root].statuses)
    for path in paths:
        segments = tuple(part.strip() for part in path.split("→") if part.strip())
        if not segments:
            continue
        key = segments[-1]
        matching = []
        for index, parents, entries in rows:
            if len(segments) > 1 and segments[:-1] != parents:
                continue
            for name, value in entries:
                if key == name:
                    matching.append((index, bool(value)))
        state = "recorded" if any(filled for _, filled in matching) else statuses.get(path, "pending")
        yield path, state, tuple(index for index, _ in matching)


def template_fragment(document, index, clear_ranges=()):
    start, end = document.branch_bounds(index)
    text, breaks, tokens, nodes = document.copy_fragment(start, end)
    fragment = Document(text, breaks, tokens, (*nodes, *(
        document.from_raw("").nodes if not nodes else ()
    )))
    if fragment.text.endswith("\n"):
        from core.composition.document import Node
        fragment = replace(fragment, nodes=(*fragment.nodes, Node(len(fragment.text))))
    removals = []
    for item in clear_ranges:
        if isinstance(item, str):
            token = next((token for token in fragment.tokens if token.id == item), None)
            if token:
                removals.append((token.start, token.end, token))
        else:
            first, last = item
            token = next((token for token in fragment.tokens if first <= token.start and token.end <= last), None)
            if first < last:
                removals.append((first, last, token))
    for first, last, token in sorted(removals, key=lambda value: value[0], reverse=True):
        line = fragment.line_index(first)
        node_id = fragment.nodes[line].id
        fragment = fragment.splice(first, last, "")
        slot = (first - fragment.nodes[line].start, token.main.group_id if token else "",
                tuple(c.group_id for c in token.prefixes) if token else (),
                tuple(c.group_id for c in token.suffixes) if token else ())
        fragment = replace(fragment, nodes=tuple(
            replace(node, slots=(*node.slots, slot)) if node.id == node_id else node for node in fragment.nodes
        ))
    fragment.validate()
    return fragment


def clean_template_output(document):
    """Keep original empty headings. A leaf created solely as a template input may be omitted."""
    if not any(node.slots for node in document.nodes):
        return document.raw
    pieces = []
    for index, node in enumerate(document.nodes):
        start, end = document.line_bounds(index)
        raw = document.slice(start, end)
        values = fields(project(raw).rstrip("\n"))
        if node.slots and values and all(not value for _, value in values) and document.branch_bounds(index)[1] == end:
            continue
        pieces.append(raw)
    return "".join(pieces)
