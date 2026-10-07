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
_GEMINI_SKIP_UNTIL: dict = {}      # (key-suffix, model) -> epoch seconds until which the slot is skipped
_GEMINI_BREAKER_SEC = 300
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


def provider_chain(media: bool = False) -> list[str]:
    """Providers to try, in order, for this kind of request — primary first
    then the fallbacks that actually have a key."""
    order = _VISION_PRIORITY if media else _TEXT_PRIORITY
    forced = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if forced and forced in order and _key(forced):
        order = [forced] + [p for p in order if p != forced]
    chain = [p for p in order if _key(p)]
    if media:
        chain = [p for p in chain if p in _VISION_PRIORITY]
    if not chain:
        (resolve_vision if media else resolve)()   # raises the helpful message
    return chain


def active_label() -> str:
    try:
        p, _, m = resolve()
        return f"{p}:{m}"
    except LLMUnavailable:
        return "none"


# -- unified completion ------------------------------------------

def complete(system: str, user: str, max_tokens: int = 1500, *,
             media: list | None = None, provider: str | None = None,
             retries: int = 2) -> tuple[str, str, str]:
    """Returns (raw_text, provider, model). `provider=None` picks the primary
    for the request kind. Raises SizeLimitError / LLMUnavailable / the
    provider's own error."""
    if provider is None:
        provider = provider_chain(media=bool(media))[0]
    if not _key(provider):
        raise LLMUnavailable(f"No key for provider {provider!r}")
    model = _model(provider)

    if provider == "gemini":
        parts = [user] + (media or [])
        return _gemini(system, parts, model, _key(provider), max_tokens), provider, model

    if provider == "anthropic":
        return _anthropic(system, user, media, model, _key(provider), max_tokens), \
            provider, model

    # OpenAI-compatible (groq / grok / openai) — text only
    if media:
        raise LLMUnavailable(f"{provider} has no vision support here")
    return _openai_compat(provider, system, user, model, _key(provider),
                          max_tokens, retries), provider, model


def _openai_compat(provider, system, user, model, key, max_tokens, retries):
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


def gemini_keys(primary: str | None = None) -> list:
    """Every configured Gemini key, primary first, de-duplicated:
    GOOGLE_API_KEY, then GOOGLE_API_KEYS (comma-separated), then
    GOOGLE_API_KEY_2 .. GOOGLE_API_KEY_9. A key hitting its free-tier quota
    (429) or hanging must not take every screenshot down with it -- vision is
    Gemini-only, so these fallbacks are the only safety net for images."""
    keys: list = []

    def add(k):
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)
    add(primary)
    add(os.environ.get("GOOGLE_API_KEY"))
    for k in (os.environ.get("GOOGLE_API_KEYS") or "").split(","):
        add(k)
    for n in range(2, 10):
        add(os.environ.get(f"GOOGLE_API_KEY_{n}"))
    return keys


def _gemini(system, parts, model, key, max_tokens, retries=1):
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
    keys = gemini_keys(key)
    slots = [(k, model) for k in keys] + [(k, _GEMINI_FALLBACK) for k in keys]
    started, last = time.time(), None
    for k, mdl in slots:
        slot = (k[-6:], mdl)
        if time.time() < _GEMINI_SKIP_UNTIL.get(slot, 0):
            continue
        if last is not None and time.time() - started > _GEMINI_TOTAL_BUDGET_SEC:
            break                              # a guest has waited long enough; report the failure
        for attempt in range(retries):
            try:
                return _client(k).models.generate_content(
                    model=mdl, contents=contents, config=cfg).text
            except ServerError as e:
                last = e
                _GEMINI_SKIP_UNTIL[slot] = time.time() + _GEMINI_BREAKER_SEC
            except Exception as e:
                last = e
                _GEMINI_SKIP_UNTIL[slot] = time.time() + _GEMINI_BREAKER_SEC
                break
    if last is None:
        last = RuntimeError("every Gemini key/model is cooling down after recent failures")
    raise last


# -- back-compat shims (older call sites) ------------------------

def chat_json(system: str, user: str, max_tokens: int = 1500, retries: int = 2) -> str:
    return complete(system, user, max_tokens, retries=retries)[0]


def chat_multimodal(system: str, user: str, media: list, max_tokens: int = 1800) -> tuple:
    return complete(system, user, max_tokens, media=media)
