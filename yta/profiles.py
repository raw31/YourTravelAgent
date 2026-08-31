"""No per-OTA code.

`route(url)` returns a single generic Profile whose only OTA-specific value
is `name` — the second-level domain, used purely as a label in the packet
and logs. Rendering and extraction are identical for every site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# generic render settings — one set for every site
RENDER_WAIT = "domcontentloaded"
SETTLE_MS = 9000
# tried in order; falls back to <body>. Covers the usual SPA roots.
CONTENT_SELECTOR = ("main, [role=main], #root, #app, #__next, #contents, "
                    "#basiclayout, .b-registration__container, body")

_LABEL = re.compile(r"([a-z0-9-]+)\.(?:com|net|org|co|in|ae|sa|io)(?:\.[a-z]{2})?$")


@dataclass
class Profile:
    name: str = "generic"
    page_type: str = "unknown"
    render_wait: str = RENDER_WAIT
    settle_ms: int = SETTLE_MS
    content_selector: str = CONTENT_SELECTOR


GENERIC = Profile()


def route(url: str) -> Profile:
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        raise ValueError(f"Not an absolute URL: {url!r}")
    m = _LABEL.search(p.netloc.lower())
    return Profile(name=m.group(1) if m else "generic")
