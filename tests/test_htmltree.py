"""Tests for the small HTML element tree shared by the page-reading scrapers."""

from __future__ import annotations

from ats_scrapers.scrapers._htmltree import Node, collapse, parse_html


def _only(root: Node, tag: str) -> Node:
    found = [node for node in root.walk() if node.tag == tag]
    assert len(found) == 1
    return found[0]


def test_elements_come_in_document_order_with_their_attributes() -> None:
    root = parse_html('<div id="a" class="x  y"><p>One</p><span data-k="v">Two</span></div>')

    assert [node.tag for node in root.walk()] == ["div", "p", "span"]
    div = _only(root, "div")
    assert div.attrs == {"id": "a", "class": "x  y"}
    assert div.classes == ["x", "y"]
    assert _only(root, "span").attrs["data-k"] == "v"
    assert root.find("p") is _only(root, "p")
    assert root.find("table") is None


def test_text_is_one_line_per_block_without_blank_lines() -> None:
    root = parse_html(
        "<div> First&nbsp;line <br> second <b>bold</b>\n text"
        "<ul><li>One</li><li> </li><li>Two &amp; three</li></ul></div>"
    )

    assert root.lines() == "First line\nsecond bold text\nOne\nTwo & three"
    assert root.text() == "First line second bold text One Two & three"


def test_line_breaks_are_kept_in_preformatted_text() -> None:
    root = parse_html("<p>One\ntwo</p><pre>Mon  08:00\nTue  <b>09:00</b>\n\nWed</pre><p>End</p>")

    assert root.lines() == "One two\nMon 08:00\nTue 09:00\nWed\nEnd"


def test_scripts_and_styles_are_not_text() -> None:
    root = parse_html(
        "<p>Shown</p><script>var a = '<p>not shown</p>';</script>"
        "<style>p { color: red; }</style><noscript>Neither</noscript>"
    )

    assert root.text() == "Shown"
    assert [node.tag for node in root.walk()] == ["p", "script", "style", "noscript"]


def test_void_elements_do_not_hold_what_follows() -> None:
    root = parse_html('<p>Before<img src="a.png">after<input name="q"/>end</p>')

    assert _only(root, "img").children == []
    assert _only(root, "input").children == []
    assert _only(root, "p").text() == "Beforeafterend"


def test_a_missing_end_tag_is_closed_by_its_parent() -> None:
    root = parse_html("<ul><li>One<li>Two</ul><p>After</p>")

    assert _only(root, "ul").lines() == "One\nTwo"
    # The paragraph is beside the list, not inside the unclosed item.
    assert [node.tag for node in root.children if isinstance(node, Node)] == ["ul", "p"]


def test_a_stray_end_tag_inside_a_cell_closes_nothing_outside_it() -> None:
    root = parse_html(
        "<div><table><tr><td>Pasted</div></section> text</td><td>Next</td></tr></table></div>"
    )

    cells = [node for node in root.walk() if node.tag == "td"]
    assert [cell.text() for cell in cells] == ["Pasted text", "Next"]


def test_table_end_tags_close_through_unclosed_markup_in_a_cell() -> None:
    root = parse_html("<table><tr><td><p>One<b>bold</td></tr><tr><td>Two</td></tr></table>")

    rows = [node for node in root.walk() if node.tag == "tr"]
    assert [row.text() for row in rows] == ["Onebold", "Two"]


def test_markup_nested_thousands_of_levels_deep_is_read_like_any_other() -> None:
    depth = 5000
    root = parse_html(
        "<span>" * depth + 'Deep <p id="field">Value</p> tail' + "</span>" * depth + "<p>After</p>"
    )

    spans = [node for node in root.walk() if node.tag == "span"]
    assert len(spans) == depth
    # Each element holds what it holds in the page, however deep it is.
    assert spans[-1].lines() == "Deep\nValue\ntail"
    field = next(node for node in root.walk() if node.attrs.get("id") == "field")
    assert field.text() == "Value"
    assert root.lines() == "Deep\nValue\ntail\nAfter"


def test_an_unknown_end_tag_is_ignored() -> None:
    root = parse_html("<p>One</section>Two</p>")

    assert _only(root, "p").text() == "OneTwo"


def test_collapse() -> None:
    assert collapse("  a  b\n\tc  ") == "a b c"
    assert collapse("") == ""
