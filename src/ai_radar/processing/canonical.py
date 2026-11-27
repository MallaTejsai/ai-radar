"""Deterministic identity: URL canonicalisation and canonical keys.

The same release shows up on a company blog, GitHub and Hugging Face. Identity
must therefore be computable without any model, stable across processes, and
explainable when someone asks "why were these two merged?".
"""

from __future__ import annotations

import posixpath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..utils.text import stable_sha256

#: Query parameters that never change the identity of a page.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "yclid",
        "s",
        "si",
        "trk",
        "_hsenc",
        "_hsmi",
    }
)
TRACKING_PREFIXES = ("utm_", "hs_", "pk_")

#: Namespaces used in canonical keys, one per collection method.
NAMESPACE_BY_KIND = {
    "github_releases": "gh",
    "huggingface_models": "hf",
}


def canonical_url(url: str) -> str:
    """Normalise a URL for comparison.

    * scheme and host lower-cased, default port removed
    * path dot-segments resolved, trailing slash removed (except root)
    * fragment dropped
    * tracking parameters dropped, remaining parameters sorted
    """
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""

    port = parts.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = posixpath.normpath(parts.path or "/")
    if parts.path.endswith("/") and not path.endswith("/"):
        path = path + "/" if path != "/" else "/"
    path = path.rstrip("/") or "/"
    # normpath collapses a leading "//" - restore the single slash form.
    if not path.startswith("/"):
        path = "/" + path

    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES):
            continue
        kept.append((key, value))
    kept.sort()

    return urlunsplit((scheme, netloc, path, urlencode(kept, doseq=True), ""))


def canonical_key(
    url: str,
    *,
    kind: str | None = None,
    identity: str | None = None,
) -> str:
    """Compute the deterministic dedupe key for an entry.

    ``identity`` wins when a collector can supply a stable upstream identifier
    (a GitHub ``owner/repo@tag`` or a Hugging Face model id), because the release
    page URL can change while the artefact identity does not.
    """
    if identity:
        namespace = NAMESPACE_BY_KIND.get(kind or "", "id")
        normalised = " ".join(identity.strip().lower().split())
        return f"{namespace}:{stable_sha256(normalised)[:32]}"

    normalised_url = canonical_url(url)
    if not normalised_url:
        # Fall back to a hash of the raw URL rather than raising: the entry will
        # be rejected by normalisation anyway, but we keep the pipeline total.
        return "url:" + stable_sha256(url or "")[:32]
    return "url:" + stable_sha256(normalised_url)[:32]


def same_site(left: str, right: str) -> bool:
    """Whether two URLs share a host, ignoring a leading ``www.``."""
    try:
        left_host = (urlsplit(left).hostname or "").lower()
        right_host = (urlsplit(right).hostname or "").lower()
    except ValueError:
        return False
    strip = lambda host: host[4:] if host.startswith("www.") else host  # noqa: E731
    return bool(left_host) and strip(left_host) == strip(right_host)
