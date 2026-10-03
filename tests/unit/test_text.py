from __future__ import annotations

import pytest

from jobbot.normalize.text import html_to_text, truncate


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_input(value):
    assert html_to_text(value) == ""


def test_plain_text_passes_through():
    assert html_to_text("Python backend role") == "Python backend role"


def test_escaped_html_is_unescaped_then_stripped():
    # Greenhouse's `content` field is HTML that has itself been entity-escaped.
    raw = "&lt;p&gt;Build &lt;strong&gt;APIs&lt;/strong&gt; in Python &amp;amp; Go&lt;/p&gt;"
    assert html_to_text(raw) == "Build APIs in Python & Go"


def test_block_structure_and_bullets():
    raw = (
        "<h2>What you'll do</h2><ul><li>Design REST APIs</li><li><p>Own Kafka consumers</p></li>"
        "</ul><p>Experience:<br>0-2 years</p>"
    )
    assert html_to_text(raw) == (
        "What you'll do\n\n• Design REST APIs\n\n• Own Kafka consumers\n\nExperience:\n0-2 years"
    )


def test_scripts_and_styles_are_dropped():
    raw = "<style>p{color:red}</style><p>Hello</p><script>alert(1)</script>"
    assert html_to_text(raw) == "Hello"


def test_entities_and_nbsp_are_normalised():
    assert html_to_text("<p>₹12&nbsp;LPA &ndash; base</p>") == "₹12 LPA – base"


def test_literal_ampersand_lt_in_real_html_is_not_double_unescaped():
    assert html_to_text("<p>Use a &lt;Queue&gt;</p>") == "Use a <Queue>"


def test_unclosed_tags_do_not_crash():
    assert html_to_text("<div><p>Unclosed <b>bold") == "Unclosed bold"


def test_truncate():
    assert truncate("short", 10) == "short"
    assert truncate("one two three four", 12) == "one two …"
