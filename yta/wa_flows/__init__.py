"""Pluggable WhatsApp conversation flows.

Each flow module exposes one function — handle_batch(frm, items) — and
owns its own session state. Which one actually runs is picked per
process by the YTA_WA_FLOW env var (default: "v0", today's behavior —
switching flows is opt-in, never silent). Add a new flow by dropping a
module in this package with the same handle_batch(frm, items) shape and
registering it in FLOWS below; nothing in yta/web.py needs to change.
"""
from __future__ import annotations

import os

from yta.wa_flows import v0, v1

FLOWS = {
    "v0": v0.handle_batch,
    "v1": v1.handle_batch,
}
DEFAULT_FLOW = "v0"


def active_flow_name() -> str:
    return os.environ.get("YTA_WA_FLOW", DEFAULT_FLOW)


def handle_batch(frm: str, items: list) -> None:
    name = active_flow_name()
    fn = FLOWS.get(name)
    if fn is None:
        print(f"[wa] YTA_WA_FLOW={name!r} is not a known flow "
              f"({list(FLOWS)}) — falling back to {DEFAULT_FLOW!r}", flush=True)
        fn = FLOWS[DEFAULT_FLOW]
    fn(frm, items)
