"""LLM plumbing.

One entry point: `complete(system, user, max_tokens, media=None, provider=None)`
-> (text, provider, model). Text goes to an OpenAI-compatible host (Groq by
default); images/PDF need a vision model (Gemini). Keys come from the
environment; a local `.env` next to the repo is auto-loaded for dev.

Text-provider priority (LLM_PROVIDER override, else): groq > grok > openai
> gemini > anthropic. Vision: gemini > anthropic.

Groq's free tier caps requests at 8k tokens/min and returns HTTP 413 for a
bigger one — surfaced here as `SizeLimitError` so the pipeline can retry on
another provider.

Note: Groq (fast-inference host, gsk_… keys) and Grok (xAI, xai-… keys)
are different providers that share the OpenAI API shape.
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from yta import llm_pool

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


_OPENAI_COMPAT_BASE = {
    "groq": "https://api.groq.com/openai/v1",
    "grok": "https://api.x.ai/v1",
    "openai": None,
}
_DEFAULT_MODEL = {
    "groq": "openai/gpt-oss-120b",
    "grok": "grok-4-fast",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-flash-latest",
    "anthropic": "claude-sonnet-4-5",
}
_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "grok": "GROK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_TEXT_PRIORITY = ["groq", "grok", "openai", "gemini", "anthropic"]
_VISION_PRIORITY = ["gemini", "anthropic"]
_GEMINI_FALLBACK = "gemini-flash-lite-latest"
_SHORT_PARK_SEC = 300              # a hang / 5xx other than 503: skip the slot briefly
_GEMINI_TOTAL_BUDGET_SEC = 50      # stop trying further slots once a call has taken this long


class LLMUnavailable(RuntimeError):
    pass


class SizeLimitError(RuntimeError):
    """Provider rejected the request as too large (e.g. Groq free-tier TPM)."""


class GenerationFailed(RuntimeError):
    """The model call itself came back a hard 400 that isn't a size problem —
    e.g. Groq's json_object mode occasionally returns
    `json_validate_failed` with an empty `failed_generation`. Not this
    provider's fault to retry into; the pipeline should just try the next
    provider in the chain, same as SizeLimitError."""


# -- provider resolution ------------------------------------------

def _key(provider: str) -> str:
    return os.environ.get(_KEY_ENV.get(provider, ""), "").strip()


def _model(provider: str) -> str:
    return os.environ.get(f"{provider.upper()}_MODEL", "").strip() \
        or _DEFAULT_MODEL[provider]


def _resolve(order):
    forced = os.environ.get("LLM_PROVIDER", "").strip().lower()
    for provider in ([forced] if forced and forced in order else order):
        if _key(provider):
            return provider, _key(provider), _model(provider)
    raise LLMUnavailable(
        "No LLM key configured. Set one of: " + ", ".join(_KEY_ENV.values())
        + " (in the environment or YourTravelAgent/.env)."
    )


def resolve():
    return _resolve(_TEXT_PRIORITY)


def resolve_vision():
    try:
        return _resolve(_VISION_PRIORITY)
    except LLMUnavailable:
        raise LLMUnavailable(
            "Image/PDF extraction needs a vision model. Set GOOGLE_API_KEY "
            "(Gemini) or ANTHROPIC_API_KEY in YourTravelAgent/.env.")


import re as _re

_MINUTE_MARKERS = ("per minute", "perminute", "tokens per minute", "(tpm)", "(rpm)", "requests per minute")
_DAILY_MARKERS = ("per day", "perday", "daily", "(tpd)", "(rpd)", "requests per day", "tokens per day")


def _retry_after_seconds(msg: str):
    """Seconds the provider says to wait ("try again in 12m51.4s", "retryDelay: '37s'"), else None."""
    m = _re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", msg)
    if m and any(m.groups()):
        h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + sec
    m = _re.search(r"retrydelay['\"]?\s*[:=]\s*['\"]?([\d.]+)s", msg)
    return float(m.group(1)) if m else None


def classify_error(exc) -> tuple | None:
    """(park_seconds, reason) when this failure means "stop using this slot for
    a while", None when it does not (a bad generation, a request that was just
    too large, a client-side mistake).

      quota (429 / RESOURCE_EXHAUSTED)  per-day or unclear -> 24h, or until the
                                        provider says the limit lifts if it
                                        tells us sooner; per-minute -> briefly
      503 / unavailable / overloaded    24h (per owner policy; see llm_pool: a
                                        parked slot is still tried last)
      hang / 500 / 502 / 504 / network  5 min
    """
    msg = str(exc).lower()
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__
    is_quota = (code == 429 or "resource_exhausted" in msg or "rate limit" in msg
                or "quota" in msg or "tokens per" in msg)
    if is_quota:
        if "too large" in msg and not any(m in msg for m in _DAILY_MARKERS + _MINUTE_MARKERS):
            return None                                # about THIS request's size, not the key
        retry = _retry_after_seconds(msg)
        if any(m in msg for m in _MINUTE_MARKERS) and not any(m in msg for m in _DAILY_MARKERS):
            return (min(max(retry or 90, 30) + 15, 900), "rate limit (per minute)")
        if retry and retry < llm_pool.DAY and any(m in msg for m in _DAILY_MARKERS):
            return (retry + 30, "daily quota, resets sooner than 24h")    # e.g. Groq's rolling TPD window
        return (llm_pool.DAY, "quota exhausted")
    if code == 503 or "unavailable" in msg or "overloaded" in msg or "over capacity" in msg:
        return (llm_pool.DAY, "503 unavailable")
    if code in (500, 502, 504) or name in ("ServerError", "ReadTimeout", "ConnectTimeout", "Timeout",
                                          "ConnectionError", "APITimeoutError", "APIConnectionError",
                                          "DeadlineExceeded") \
            or "deadline" in msg or "timed out" in msg:
        return (_SHORT_PARK_SEC, "server error / timeout")
    return None


def _provider_slots(provider: str) -> list:
    """Every slot id that backs this provider (one per key, x model for Gemini)."""
    if provider == "gemini":
        return [llm_pool.slot_id("gemini", k, m) for k in gemini_keys(_key("gemini"))
                for m in (_model("gemini"), _GEMINI_FALLBACK)]
    return [llm_pool.slot_id(provider, k) for k in api_keys(_KEY_ENV[provider], _key(provider))]


def provider_healthy(provider: str) -> bool:
    slots = _provider_slots(provider)
    return any(not llm_pool.is_parked(s) for s in slots) if slots else True


# --------------------------------------------------------------------------
# Per-stage defaults: every LLM call site names its STAGE, and each stage uses
# whatever is best suited for that job (owner, 2026-10-07: "decide what model does
# best for what work and make that the default for that stage"). Keys rotate
# INSIDE a provider/model (same behaviour every time); the other provider is the
# fallback in both directions; a parked provider/slot moves to the end but is
# still tried last.
#
#   vision        screenshot / PDF -> fields. Only Gemini (and Anthropic, if keyed)
#                 can read images. Gemini's accurate model first, the lighter one
#                 only as a last resort.
#   text_extract  typed message / page text -> fields. Groq: fast, JSON mode,
#                 validated for months. Gemini when every Groq key failed -- its
#                 FAST model first (the thinking model can take 15-50s).
#   clarify       parsing a short reply ("2 adults in each room"). Same as above.
#   room_judge    "are these the same room?" -- low volume, accuracy matters (a
#                 lenient judge confirmed a wrong room in QA): Groq's larger
#                 reasoning model first; Gemini's accurate model as the fallback.
# --------------------------------------------------------------------------
STAGES = {
    "vision":       {"providers": ["gemini", "anthropic"], "gemini_models": ["flash", "lite"]},
    "text_extract": {"providers": ["groq", "gemini", "grok", "openai", "anthropic"], "gemini_models": ["lite", "flash"]},
    "clarify":      {"providers": ["groq", "gemini", "grok", "openai", "anthropic"], "gemini_models": ["lite", "flash"]},
    "room_judge":   {"providers": ["groq", "gemini", "grok", "openai", "anthropic"], "gemini_models": ["flash", "lite"]},
}


def gemini_model_order(stage: str | None, media: bool = False) -> list:
    """Concrete Gemini model names for this stage, best first."""
    st = STAGES.get(stage or ("vision" if media else "text_extract"), STAGES["text_extract"])
    names = {"flash": _model("gemini"), "lite": _GEMINI_FALLBACK}
    out = []
    for k in st["gemini_models"]:
        if names[k] not in out:
            out.append(names[k])
    return out


def provider_chain(media: bool = False, stage: str | None = None) -> list[str]:
    """Providers to try, in order, for this stage -- the stage's best-suited
    provider first, then the others that actually have a key (the other one is
    the fallback in both directions). Vision requests only ever chain through
    vision-capable providers."""
    stage = stage or ("vision" if media else "text_extract")
    order = list(STAGES.get(stage, STAGES["text_extract"])["providers"])
    forced = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if forced and forced in order and _key(forced):
        order = [forced] + [p for p in order if p != forced]
    chain = [p for p in order if _key(p)]
    if media:
        chain = [p for p in chain if p in _VISION_PRIORITY]
    if not chain:
        (resolve_vision if media else resolve)()   # raises the helpful message
    healthy = [p for p in chain if provider_healthy(p)]
    parked = [p for p in chain if p not in healthy]
    return healthy + parked


def active_label() -> str:
    try:
        p, _, m = resolve()
        return f"{p}:{m}"
    except LLMUnavailable:
        return "none"


# -- unified completion ------------------------------------------

def complete(system: str, user: str, max_tokens: int = 1500, *,
             media: list | None = None, provider: str | None = None,
             retries: int = 2, stage: str | None = None) -> tuple[str, str, str]:
    """Returns (raw_text, provider, model).

    `provider=None` (the default) means "get me an answer": every provider in
    the chain for this kind of request is tried in order -- text: Groq, then
    Gemini (which itself fails over across every configured key and model);
    vision: Gemini, then Anthropic -- and only when ALL of them failed is the
    last error raised. An explicit `provider` is used as-is (callers such as
    the extraction pipeline run their own chain loop and must not double up)."""
    if provider is not None:
        return _complete_one(system, user, max_tokens, media, provider, retries, stage)
    chain = provider_chain(media=bool(media), stage=stage)
    last = None
    for prov in chain:
        try:
            return _complete_one(system, user, max_tokens, media, prov, retries, stage)
        except Exception as e:  # noqa: BLE001 -- any provider failure -> next provider
            last = e
            print(f"[llm] {prov} failed ({type(e).__name__}: {str(e)[:100]}) -- "
                  f"{'trying the next provider' if prov != chain[-1] else 'no providers left'}",
                  flush=True)
    raise last if last is not None else LLMUnavailable("no LLM provider is configured")


def _complete_one(system, user, max_tokens, media, provider, retries, stage=None):
    if not _key(provider):
        raise LLMUnavailable(f"No key for provider {provider!r}")
    model = _model(provider)

    if provider == "gemini":
        parts = [user] + (media or [])
        return _gemini(system, parts, model, _key(provider), max_tokens,
                       models=gemini_model_order(stage, bool(media))), provider, model

    if provider == "anthropic":
        return _anthropic(system, user, media, model, _key(provider), max_tokens), \
            provider, model

    # OpenAI-compatible (groq / grok / openai) — text only
    if media:
        raise LLMUnavailable(f"{provider} has no vision support here")
    return _openai_compat(provider, system, user, model, _key(provider),
                          max_tokens, retries), provider, model


_QUOTA_MARKERS = ("tokens per", "tpd", "tpm", "rate limit reached", "requests per day")


def _openai_compat(provider, system, user, model, key, max_tokens, retries):
    """One request against an OpenAI-compatible provider, across every
    configured key for it (e.g. GROQ_API_KEY, GROQ_API_KEY_2). Healthy keys are
    used round-robin (so no key's daily quota burns alone); a key that hit a
    quota error or a 503 is parked (see classify_error / llm_pool) and tried
    only as a last resort. Only when every key failed does the error propagate
    -- and complete() then fails over to the next provider."""
    keys = llm_pool.order(f"keys:{provider}", api_keys(_KEY_ENV[provider], key),
                          lambda k: llm_pool.slot_id(provider, k))
    last = None
    for k in keys:
        try:
            return _openai_compat_one(provider, system, user, model, k, max_tokens, retries)
        except Exception as e:  # noqa: BLE001 -- bad generation, quota, 5xx, network: next key
            last = e
            c = classify_error(e)
            if c:
                llm_pool.park(llm_pool.slot_id(provider, k), c[0], f"{provider} {c[1]}")
    if last is None:
        last = LLMUnavailable(f"no {provider} key configured")
    raise last


def _openai_compat_one(provider, system, user, model, key, max_tokens, retries):
    from openai import OpenAI, RateLimitError, BadRequestError, APIStatusError
    client = OpenAI(api_key=key, base_url=_OPENAI_COMPAT_BASE.get(provider))
    kwargs = {"response_format": {"type": "json_object"}} \
        if provider in ("groq", "openai") else {}
    for attempt in range(retries + 1):
        try:
            r = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=0,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}], **kwargs)
            return r.choices[0].message.content
        except RateLimitError as e:                    # subclass of APIStatusError
            msg = str(e).lower()
            # per-request size cap, per-minute cap, or per-day cap -> next provider
            if any(s in msg for s in ("too large", "tokens per", "tpd", "tpm",
                                      "rate limit reached", "requests per day")):
                raise SizeLimitError(str(e)) from e
            if attempt == retries:
                raise
            time.sleep(20 * (attempt + 1))
        except BadRequestError as e:                    # subclass of APIStatusError
            msg = str(e).lower()
            if getattr(e, "status_code", None) == 413 or "too large" in msg:
                raise SizeLimitError(str(e)) from e
            # e.g. groq json_object mode: "Failed to validate JSON. Please
            # adjust your prompt." with an empty failed_generation — a bad
            # generation, not a bad key/request shape. Try the next provider.
            raise GenerationFailed(str(e)) from e
        except APIStatusError as e:
            if getattr(e, "status_code", None) == 413 or "too large" in str(e).lower():
                raise SizeLimitError(str(e)) from e
            raise


def _anthropic(system, user, media, model, key, max_tokens):
    import anthropic
    if media:
        blocks = [{"type": "text", "text": user}]
        for mime, data in media:
            kind = "document" if mime == "application/pdf" else "image"
            blocks.append({"type": kind, "source": {
                "type": "base64", "media_type": mime,
                "data": base64.standard_b64encode(data).decode()}})
        content = blocks
    else:
        content = user
    r = anthropic.Anthropic(api_key=key).messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": content}])
    return r.content[0].text


def api_keys(env_name: str, primary: str | None = None) -> list:
    """Every configured key for a provider, primary first, de-duplicated:
    <ENV>, then <ENV>S (comma-separated, e.g. GOOGLE_API_KEYS), then
    <ENV>_2 .. <ENV>_9. A key hitting its quota (429) or hanging must not take
    every request down with it."""
    keys: list = []

    def add(k):
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)
    add(primary)
    add(os.environ.get(env_name))
    base = env_name[:-4] if env_name.endswith("_KEY") else env_name
    for k in (os.environ.get(f"{base}_KEYS") or os.environ.get(f"{env_name}S") or "").split(","):
        add(k)
    for n in range(2, 10):
        add(os.environ.get(f"{env_name}_{n}"))
    return keys


def gemini_keys(primary: str | None = None) -> list:
    """GOOGLE_API_KEY, GOOGLE_API_KEYS (comma-separated), GOOGLE_API_KEY_2..9.
    Vision is Gemini-only, so these fallbacks are the only safety net for images."""
    return api_keys("GOOGLE_API_KEY", primary)


def _gemini(system, parts, model, key, max_tokens, retries=1, models=None):
    from google import genai
    from google.genai import types
    from google.genai.errors import ServerError

    contents = []
    for part in parts:
        if isinstance(part, tuple):
            contents.append(types.Part.from_bytes(data=part[1], mime_type=part[0]))
        else:
            contents.append(part)
    cfg = types.GenerateContentConfig(
        system_instruction=system, temperature=0,
        max_output_tokens=max_tokens, response_mime_type="application/json")
    try:                                     # cap thinking where the model allows it
        cfg.thinking_config = types.ThinkingConfig(thinking_budget=256)
    except Exception:
        pass

    def _client(k):
        return genai.Client(api_key=k, http_options=types.HttpOptions(
            timeout=25_000,             # ms (a healthy call is 2-10s)
            # The SDK's own default retry policy retries a 429 (rate limit /
            # daily quota exhausted) up to 5x with exponential backoff -- ~30s
            # of pure waiting on a call that cannot succeed until the quota
            # resets. attempts=1 disables that; the loop below fails over.
            retry_options=types.HttpRetryOptions(attempts=1)))

    # Order: the PRIMARY model on every key first (quality), only then the
    # weaker fallback model on every key. Each (key, model) slot has its own
    # circuit breaker, so a quota-dead or hanging slot is skipped for a few
    # minutes instead of making every guest wait out its timeout (live
    # 2026-10-07: gemini-flash-latest 504'd after 60s while flash-lite
    # answered in 1s -- each screenshot cost ~50s). Keys are only ever
    # identified by their last 6 chars here, never logged in full.
    # The stage's best model on every key first, then its next model on every key; healthy slots before parked ones (parked = last resort).
    # Keys rotate round-robin per call so the daily quota is shared. Each
    # (key, model) is its own slot -- a model's quota is per model.
    keys = llm_pool.order("keys:gemini", gemini_keys(key), lambda k: llm_pool.slot_id("gemini", k))
    model_order = list(models) if models else [model, _GEMINI_FALLBACK]
    slots = [(k, m) for m in model_order for k in keys]
    slots.sort(key=lambda sm: llm_pool.is_parked(llm_pool.slot_id("gemini", sm[0], sm[1])))   # stable
    started, last = time.time(), None
    for k, mdl in slots:
        sid = llm_pool.slot_id("gemini", k, mdl)
        if last is not None and time.time() - started > _GEMINI_TOTAL_BUDGET_SEC:
            break                              # a guest has waited long enough; report the failure
        for attempt in range(retries):
            try:
                # Keep a reference for the whole call: a temporary Client is
                # garbage-collected (and its HTTP client CLOSED) before the
                # request is sent -> "Cannot send a request, as the client has
                # been closed".
                client = _client(k)
                return client.models.generate_content(
                    model=mdl, contents=contents, config=cfg).text
            except Exception as e:  # noqa: BLE001
                last = e
                c = classify_error(e)
                llm_pool.park(sid, c[0] if c else _SHORT_PARK_SEC, f"gemini {c[1] if c else type(e).__name__}")
                break
    if last is None:
        last = RuntimeError("no Gemini key configured")
    raise last


# -- back-compat shims (older call sites) ------------------------

def chat_json(system: str, user: str, max_tokens: int = 1500, retries: int = 2) -> str:
    return complete(system, user, max_tokens, retries=retries)[0]


def chat_multimodal(system: str, user: str, media: list, max_tokens: int = 1800) -> tuple:
    return complete(system, user, max_tokens, media=media)
