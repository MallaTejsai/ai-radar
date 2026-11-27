"""URL canonicalisation and canonical-key derivation."""

from __future__ import annotations

from ai_radar.processing.canonical import canonical_key, canonical_url, same_site


def test_canonical_url_drops_tracking_parameters():
    raw = "https://example.com/post?id=7&utm_source=rss&utm_medium=feed"
    assert canonical_url(raw) == "https://example.com/post?id=7"


def test_canonical_url_is_insensitive_to_case_and_trailing_slash():
    assert canonical_url("HTTPS://Example.COM/Path/") == canonical_url("https://example.com/Path")


def test_canonical_url_removes_fragments_and_default_ports():
    assert canonical_url("https://example.com:443/a#section") == "https://example.com/a"


def test_canonical_url_sorts_query_parameters():
    left = canonical_url("https://example.com/a?b=2&a=1")
    right = canonical_url("https://example.com/a?a=1&b=2")
    assert left == right


def test_canonical_url_returns_empty_for_garbage():
    assert canonical_url("") == ""
    assert canonical_url("mailto:someone@example.com") == ""


def test_canonical_key_is_stable_for_equivalent_urls():
    left = canonical_key("https://example.com/post/?utm_campaign=x")
    right = canonical_key("https://example.com/post")
    assert left == right
    assert left.startswith("url:")


def test_canonical_key_uses_upstream_identity_when_available():
    key = canonical_key(
        "https://github.com/acme/tool/releases/tag/v1.2.3",
        kind="github_releases",
        identity="acme/tool@v1.2.3",
    )
    assert key.startswith("gh:")
    # Case and whitespace in the identity must not produce a different key.
    other = canonical_key(
        "https://github.com/acme/tool/releases/tag/v1.2.3",
        kind="github_releases",
        identity="  ACME/Tool@V1.2.3 ",
    )
    assert key == other


def test_canonical_key_separates_huggingface_models():
    key = canonical_key(
        "https://huggingface.co/acme/model", kind="huggingface_models", identity="acme/model"
    )
    assert key.startswith("hf:")


def test_same_site_ignores_www_prefix():
    assert same_site("https://www.example.com/a", "https://example.com/b")
    assert not same_site("https://example.com/a", "https://other.com/b")
