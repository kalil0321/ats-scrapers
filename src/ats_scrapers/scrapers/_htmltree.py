"""A small element tree for scrapers that read HTML pages.

Built on the standard-library parser, so a scraper that reads pages needs
no extra dependency. It keeps what such scrapers use: elements with their
attributes, in document order, and their text with one line per block.

Two rules make it safe on pages that carry rich text typed or pasted by
people: the end tag of anything but a table part closes nothing outside the
table cell it is in, and nesting stops at a fixed depth.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "address", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2",
        "h3", "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
)
_HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript"})
# The end tag of anything but a table part closes nothing outside the table
# cell it is in.
_CELL_TAGS = frozenset({"td", "th"})
_TABLE_TAGS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "td", "th"})
# Elements nested deeper than this are kept as leaves of the last level, so
# that markup pasted into a field cannot make the tree arbitrarily deep.
_MAX_DEPTH = 200


class Node:
    """An element of a parsed page."""

    __slots__ = ("attrs", "children", "tag")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def walk(self) -> Iterator[Node]:
        """Every element below this one, in document order."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def find(self, tag: str) -> Node | None:
        return next((node for node in self.walk() if node.tag == tag), None)

    def _pieces(self, out: list[str]) -> None:
        for child in self.children:
            if isinstance(child, str):
                # A line break in the source is white space, not a new line.
                out.append(child.replace("\n", " ").replace("\r", " "))
            elif child.tag not in _HIDDEN_TAGS:
                block = child.tag in _BLOCK_TAGS
                if block:
                    out.append("\n")
                child._pieces(out)
                if block:
                    out.append("\n")

    def lines(self) -> str:
        """The text, one line per block, without blank lines."""
        pieces: list[str] = []
        self._pieces(pieces)
        lines = (collapse(line) for line in "".join(pieces).split("\n"))
        return "\n".join(line for line in lines if line)

    def text(self) -> str:
        """The text on one line."""
        return collapse(self.lines())


class _TreeBuilder(HTMLParser):
    """Builds the element tree of a page with the standard-library parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("", {})
        self._open: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {name: value or "" for name, value in attrs})
        self._open[-1].children.append(node)
        if tag not in _VOID_TAGS and len(self._open) < _MAX_DEPTH:
            self._open.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        node = Node(tag, {name: value or "" for name, value in attrs})
        self._open[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, 0, -1):
            current = self._open[index].tag
            if current == tag:
                del self._open[index:]
                return
            if current in _CELL_TAGS and tag not in _TABLE_TAGS:
                # A stray end tag inside a cell (rich text pasted into a
                # field) must not close the cell or what is around it.
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)


def parse_html(page: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(page)
    builder.close()
    return builder.root


def collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
