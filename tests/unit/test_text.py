"""Text cleaning and deterministic identity helpers."""

from __future__ import annotations

from ai_radar.utils.text import (
    clean_text,
    domain_of,
    normalize_title,
    slugify,
    stable_sha256,
    strip_html,
    title_fingerprint,
    truncate,
)


def test_strip_html_removes_scripts_and_markup():
    html = "<p>Hello <b>world</b></p><script>alert('x')</script>"
    assert strip_html(html).strip() == "Hello world"


def test_clean_text_collapses_whitespace_and_entities():
    raw = "  <p>Free&nbsp;tier   now\n available</p>  "
    assert clean_text(raw) == "Free tier now available"


def test_clean_text_bounds_length_on_a_word_boundary():
    text = "word " * 500
    cleaned = clean_text(text, max_length=50)
    assert len(cleaned) <= 51
    assert cleaned.endswith("…")
    assert "word" in cleaned


def test_normalize_title_is_case_and_punctuation_insensitive():
    assert normalize_title("Introducing Model-X!") == normalize_title("introducing   model x")


def test_normalize_title_only_strips_a_leading_version_token():
    # A glued "v" prefix is normalised anywhere ("v2.1" == "2.1"), but the
    # words "version" and "release" are only dropped at the start.
    left = normalize_title("Acme v2.1 release notes")
    right = normalize_title("Acme 2.1 release notes")
    assert left == right
    assert normalize_title("v2.1 Foo") == normalize_title("Foo")
    # "release" in the middle is part of the headline and must survive.
    assert "release" in normalize_title("Acme release notes")


def test_title_fingerprint_is_stable_and_deterministic():
    first = title_fingerprint("Introducing Model X")
    second = title_fingerprint("introducing model x")
    assert first == second
    assert len(first) == 32
    assert all(char in "0123456789abcdef" for char in first)


def test_title_fingerprint_differs_for_different_titles():
    assert title_fingerprint("Model X released") != title_fingerprint("Model Y released")


def test_stable_sha256_handles_non_ascii():
    assert len(stable_sha256("日本語のタイトル")) == 64


def test_truncate_keeps_short_values_intact():
    assert truncate("short", 10) == "short"
    assert truncate("a" * 20, 10).endswith("…")


def test_domain_of_strips_port_and_userinfo():
    assert domain_of("https://example.com:8443/path") == "example.com"
    assert domain_of("not a url") == ""


def test_slugify_produces_filesystem_safe_tokens():
    assert slugify("AI Radar: V1.0!") == "ai-radar-v1-0"
