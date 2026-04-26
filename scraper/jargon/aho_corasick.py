"""
Minimal Aho-Corasick automaton for multi-pattern case-insensitive matching.

Used to annotate rendered text with glossary terms in one linear pass.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class _Node:
    children: Dict[str, "_Node"] = field(default_factory=dict)
    fail: Optional["_Node"] = None
    outputs: List[str] = field(default_factory=list)


class AhoCorasick:
    """Case-insensitive matcher over a fixed set of patterns."""

    def __init__(self, patterns: Iterable[str]):
        self.root = _Node()
        self._build(patterns)

    def _build(self, patterns: Iterable[str]) -> None:
        for p in patterns:
            if not p:
                continue
            node = self.root
            for ch in p.lower():
                node = node.children.setdefault(ch, _Node())
            node.outputs.append(p)

        # BFS fail links
        queue: deque[_Node] = deque()
        for child in self.root.children.values():
            child.fail = self.root
            queue.append(child)
        while queue:
            u = queue.popleft()
            for ch, v in u.children.items():
                queue.append(v)
                f = u.fail
                while f is not None and ch not in f.children:
                    f = f.fail
                v.fail = f.children[ch] if f and ch in f.children else self.root
                if v.fail and v.fail.outputs:
                    v.outputs.extend(v.fail.outputs)

    def find(self, text: str) -> List[Tuple[int, int, str]]:
        """Return (start, end_exclusive, matched_pattern) tuples.

        Only returns whole-word matches for alphanumeric patterns so "OBV" in
        "KNOB VALVE" isn't flagged.
        """
        results: List[Tuple[int, int, str]] = []
        if not text:
            return results
        lower = text.lower()
        node = self.root
        for i, ch in enumerate(lower):
            while node is not None and ch not in node.children:
                node = node.fail
            if node is None:
                node = self.root
                continue
            node = node.children[ch]
            for pat in node.outputs:
                end = i + 1
                start = end - len(pat)
                if self._is_word_boundary(text, start, end, pat):
                    results.append((start, end, pat))
        # de-duplicate overlapping matches, keeping the longest starting at each position
        results.sort(key=lambda r: (r[0], -(r[1] - r[0])))
        picked: List[Tuple[int, int, str]] = []
        last_end = -1
        for s, e, pat in results:
            if s >= last_end:
                picked.append((s, e, pat))
                last_end = e
        return picked

    @staticmethod
    def _is_word_boundary(text: str, start: int, end: int, pat: str) -> bool:
        # For patterns that begin/end with alphanumerics, enforce word boundaries.
        left_alpha = pat[0].isalnum()
        right_alpha = pat[-1].isalnum()
        if left_alpha:
            if start > 0 and text[start - 1].isalnum():
                return False
        if right_alpha:
            if end < len(text) and text[end].isalnum():
                return False
        return True
