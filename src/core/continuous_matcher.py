"""Shared ranking and cursor-fragment matching for text entry modes."""

import bisect
import re


_LATIN = re.compile(r"[A-Za-z0-9_]+$")
_LATIN_PHRASE = re.compile(r"[A-Za-z0-9_]+(?: +[A-Za-z0-9_]+)+$")
_LATIN_CHAR = re.compile(r"[A-Za-z0-9_]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]{2,8}$")



class ContinuousMatcher:
    def __init__(self, word_manager, settings):
        self.word_manager = word_manager
        self.settings = settings
        self._source_blocks = None
        self._prefix_keys = []
        self._prefix_orders = []
        self._shortcuts = {}

    def _index_entries(self):
        blocks = self.word_manager.word_blocks
        if blocks is self._source_blocks:
            return
        prefixes = []
        shortcuts = {}
        for index, block in enumerate(blocks):
            code = (block.get("shortcut_code") or "").casefold()
            if code:
                shortcuts.setdefault(code, []).append(block)
            terms = {block["parent"], *block.get("aliases", []), code}
            prefixes.extend((key.casefold(), index) for key in terms if key)
        prefixes.sort()
        self._prefix_keys = [key for key, _ in prefixes]
        self._prefix_orders = [index for _, index in prefixes]
        self._shortcuts = shortcuts
        self._source_blocks = blocks

    def _prefix_matches(self, query):
        key = query.casefold()
        index = bisect.bisect_left(self._prefix_keys, key)
        orders = set()
        while index < len(self._prefix_keys) and self._prefix_keys[index].startswith(key):
            orders.add(self._prefix_orders[index])
            index += 1
        if not orders:
            return []
        candidates = [self._source_blocks[order] for order in sorted(orders)]
        matches = self.word_manager.find_matches(
            query, self.settings.multi_word_search, self.settings.pinyin_initial_search,
            candidates=candidates,
        )
        matched = {id(block) for block in matches}
        # A shortcut prefix can match even when the text scorer finds no title or alias hit.
        remaining = [block for block in candidates if id(block) not in matched]
        return matches + [
            {**block, "highlight_groups": {}}
            for block in self.word_manager.rank_equal_matches(remaining)
        ]

    def automatic_match(self, before, after):
        following_latin = bool(_LATIN_CHAR.fullmatch(after))

        self._index_entries()
        if self._shortcuts:
            for length in range(min(64, len(before)), 0, -1):
                fragment = before[-length:]
                matches = self._shortcuts.get(fragment.casefold())
                inside_word_left = (
                    _LATIN_CHAR.fullmatch(fragment[0]) is not None and length < len(before)
                    and _LATIN_CHAR.fullmatch(before[-length - 1]) is not None
                )
                inside_word_right = following_latin and _LATIN_CHAR.fullmatch(fragment[-1]) is not None
                if matches and not (inside_word_left or inside_word_right):
                    return fragment, self._rank_shortcut_matches(matches, fragment)
        latin = _LATIN.search(before)
        if latin and not following_latin and len(latin[0]) <= 64:
            # Only search the unfinished text on this line, never the whole draft.
            phrase = _LATIN_PHRASE.search(before) if self.settings.multi_word_search else None
            if phrase and len(phrase[0]) <= 64:
                matches = self._find_matches(phrase[0])
                if matches:
                    return phrase[0], matches
            fragment = latin[0]
            matches = self._find_matches(fragment)
            return fragment, matches or self._prefix_matches(fragment)
        han = _HAN.search(before)
        if han:
            for index in range(len(han[0]) - 1):
                fragment = han[0][index:]
                matches = self._prefix_matches(fragment)
                if matches:
                    return fragment, matches
        return "", []

    def _find_matches(self, query):
        return self.word_manager.find_matches(
            query, self.settings.multi_word_search, self.settings.pinyin_initial_search
        )

    def _rank_shortcut_matches(self, blocks, fragment):
        return [
            self._highlight_shortcut(block, fragment)
            for block in self.word_manager.rank_equal_matches(blocks)
        ]

    @staticmethod
    def _highlight_shortcut(block, fragment):
        title = block["full_content"].partition("\n")[0]
        annotation = re.search(r"``\s*k\s*[:：]\s*(" + re.escape(fragment) + r")\s*``", title, re.I)
        indices = set(range(*annotation.span(1))) if annotation else set()
        return {**block, "highlight_groups": {0: indices}}

    def lookup(self, query):
        self._index_entries()
        matches = self._find_matches(query)
        exact = self._shortcuts.get(query.casefold(), [])
        if exact:
            seen = {id(block) for block in exact}
            return self._rank_shortcut_matches(exact, query) + [block for block in matches if id(block) not in seen]
        return matches
