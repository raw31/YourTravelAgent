"""Health-aware pool for LLM keys, models and providers.

Two jobs, both aimed at never letting one key's quota (or one provider's bad
day) take the bot down:

  PARKING   a (provider, key[, model]) slot that hit a quota error or a 503 is
            parked -- skipped -- for 24 hours (or until the provider says the
            limit lifts, when it tells us sooner). Parking only changes
            PREFERENCE: if every slot is parked the parked ones are still
            tried last, so a wrongly-parked key can never take the bot fully
            offline. State is persisted (data/llm_parking.json) so a deploy or
            restart does not forget what was learned.

  MIXING    when several slots are healthy they are used round-robin, so no
            single key or provider burns through its daily quota while the
            others sit idle.

Only a short hash of each key is ever stored or logged -- never key material.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

DAY = 24 * 3600

_LOCK = threading.RLock()
_PARK: dict = {}          # slot id -> epoch seconds until which the slot is parked
_WHY: dict = {}           # slot id -> short reason (for status output)
_RR: dict = {}            # round-robin counter per group
_LOADED = False


def _park_file() -> Path:
    env = os.environ.get("YTA_LLM_PARK_FILE")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "data" / "llm_parking.json"


def slot_id(provider: str, key: str, model: str | None = None) -> str:
    h = hashlib.sha256((key or "").encode()).hexdigest()[:8]
    return f"{provider}:{h}" + (f":{model}" if model else "")


def _load() -> None:
    global _LOADED
    with _LOCK:
        if _LOADED:
            return
        _LOADED = True
        try:
            data = json.loads(_park_file().read_text())
        except Exception:  # noqa: BLE001 -- no file yet / unreadable: start clean
            data = {}
        now = time.time()
        for slot, val in (data or {}).items():
            if isinstance(val, dict):
                until, why = val.get("until"), val.get("why", "")
            else:
                until, why = val, ""
            if isinstance(until, (int, float)) and until > now:
                _PARK[slot], _WHY[slot] = float(until), why


def _save() -> None:
    try:
        f = _park_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        payload = {s: {"until": u, "why": _WHY.get(s, "")} for s, u in _PARK.items() if u > now}
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, f)
    except Exception:  # noqa: BLE001 -- persistence is best-effort, never fatal
        pass


def is_parked(slot: str) -> bool:
    _load()
    with _LOCK:
        return _PARK.get(slot, 0) > time.time()


def park(slot: str, seconds: float, why: str = "") -> None:
    _load()
    with _LOCK:
        until = time.time() + max(float(seconds), 1.0)
        if until <= _PARK.get(slot, 0):
            return                                   # already parked at least this long
        _PARK[slot], _WHY[slot] = until, why
        _save()
    hours = seconds / 3600
    span = f"{hours:.1f}h" if seconds >= 3600 else f"{seconds / 60:.0f}min" if seconds >= 120 else f"{seconds:.0f}s"
    print(f"[llm-pool] parked {slot} for {span} ({why})", flush=True)


def unpark(slot: str) -> None:
    _load()
    with _LOCK:
        if _PARK.pop(slot, None) is not None:
            _WHY.pop(slot, None)
            _save()


def order(group: str, items: list, slot_of) -> list:
    """Healthy items first, rotated round-robin so the load is shared; parked
    items last (last resort -- never dropped)."""
    _load()
    healthy = [i for i in items if not is_parked(slot_of(i))]
    parked = [i for i in items if is_parked(slot_of(i))]
    if len(healthy) > 1:
        with _LOCK:
            n = _RR.get(group, 0)
            _RR[group] = n + 1
        r = n % len(healthy)
        healthy = healthy[r:] + healthy[:r]
    return healthy + parked


def status() -> list:
    """[(slot, seconds_remaining, why)] for every currently parked slot."""
    _load()
    now = time.time()
    with _LOCK:
        return sorted((s, round(u - now), _WHY.get(s, "")) for s, u in _PARK.items() if u > now)


def reset(clear_file: bool = False) -> None:
    """Forget all state (tests / manual recovery)."""
    global _LOADED
    with _LOCK:
        _PARK.clear()
        _WHY.clear()
        _RR.clear()
        _LOADED = True if not clear_file else False
        if clear_file:
            try:
                _park_file().unlink()
            except OSError:
                pass
            _LOADED = True


if __name__ == "__main__":        # python -m yta.llm_pool  -> what is parked right now
    rows = status()
    if not rows:
        print("nothing is parked")
    for slot, left, why in rows:
        print(f"{slot:40} {left / 3600:6.1f}h left   {why}")
