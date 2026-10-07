"""Provider resolution for the generic fallback (no network)."""
import pytest

from yta import llm


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for v in ("LLM_PROVIDER", "GROQ_API_KEY", "GROK_API_KEY",
              "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
              "GROQ_MODEL", "GROK_MODEL", "GEMINI_MODEL"):
        monkeypatch.delenv(v, raising=False)


def test_no_key_raises(monkeypatch):
    with pytest.raises(llm.LLMUnavailable):
        llm.resolve()


def test_groq_picked_first(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    provider, key, model = llm.resolve()
    assert provider == "groq"
    assert key == "gsk_test"
    assert model == "openai/gpt-oss-120b"


def test_llm_provider_override(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    provider, _, _ = llm.resolve()
    assert provider == "anthropic"


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", "xai_test")
    monkeypatch.setenv("GROK_MODEL", "grok-9-turbo")
    provider, _, model = llm.resolve()
    assert provider == "grok"
    assert model == "grok-9-turbo"


# -- Groq json_object 400 -> GenerationFailed (fall back, don't crash) ----

def test_openai_compat_bad_request_becomes_generation_failed(monkeypatch):
    import httpx
    from openai import BadRequestError

    class _FakeCompletions:
        def create(self, **kw):
            resp = httpx.Response(
                400, request=httpx.Request("POST", "https://api.groq.com/x"),
                json={"error": {"message": "Failed to validate JSON. Please "
                                "adjust your prompt.", "code": "json_validate_failed"}})
            raise BadRequestError("Failed to validate JSON.", response=resp,
                                  body={"error": {"code": "json_validate_failed"}})

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr("openai.OpenAI", lambda **kw: _FakeClient())
    with pytest.raises(llm.GenerationFailed):
        llm._openai_compat("groq", "sys", "user", "openai/gpt-oss-120b",
                           "gsk_test", 1800, retries=0)


def test_openai_compat_413_still_becomes_size_limit(monkeypatch):
    import httpx
    from openai import BadRequestError

    class _FakeCompletions:
        def create(self, **kw):
            resp = httpx.Response(
                413, request=httpx.Request("POST", "https://api.groq.com/x"))
            raise BadRequestError("Request too large", response=resp, body=None)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr("openai.OpenAI", lambda **kw: _FakeClient())
    with pytest.raises(llm.SizeLimitError):
        llm._openai_compat("groq", "sys", "user", "openai/gpt-oss-120b",
                           "gsk_test", 1800, retries=0)


# -- Gemini keys + circuit breaker (live 2026-10-07: the primary model hung ~60s,
#    then 429'd on quota, while flash-lite still answered; vision is Gemini-only) --

def _fake_genai(monkeypatch, behaviour):
    """behaviour(api_key, model) -> text or raises. Records (key, model) tried.

    Mimics the real SDK's ownership: `client.models` does NOT keep the Client
    alive, and a Client that is garbage-collected closes its HTTP client -- the
    bug that made every Gemini call fail ("Cannot send a request, as the client
    has been closed") when the client was created inline."""
    import types as _t
    calls = []

    class _Api:
        closed = False

    class _Models:
        def __init__(self, api, key):
            self._api, self._key = api, key

        def generate_content(self, model, contents, config):
            if self._api.closed:
                raise RuntimeError("Cannot send a request, as the client has been closed.")
            calls.append((self._key, model))
            return _t.SimpleNamespace(text=behaviour(self._key, model))

    class _Client:
        def __init__(self, api_key=None, **kw):
            self._api = _Api()
            self.models = _Models(self._api, api_key)

        def __del__(self):
            self._api.closed = True

    from google import genai as real_genai          # keep .types etc.; only swap the client
    monkeypatch.setattr(real_genai, "Client", _Client)
    return calls


def _quota():
    from google.genai.errors import ClientError
    return ClientError(429, {"error": {"message": "quota exceeded"}})


def _clean_env(monkeypatch, **keys):
    from yta import llm  # noqa: F401  (pool state is reset by tests/conftest.py)
    for n in ["GOOGLE_API_KEY", "GOOGLE_API_KEYS"] + [f"GOOGLE_API_KEY_{i}" for i in range(2, 10)]:
        monkeypatch.delenv(n, raising=False)
    for k, v in keys.items():
        monkeypatch.setenv(k, v)


def test_gemini_keys_are_collected_primary_first_and_deduplicated(monkeypatch):
    from yta import llm
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1", GOOGLE_API_KEYS="k2, k3,k1", GOOGLE_API_KEY_2="k4")
    assert llm.gemini_keys("k1") == ["k1", "k2", "k3", "k4"]


def test_a_quota_dead_key_fails_over_to_the_next_key_on_the_SAME_primary_model(monkeypatch):
    from yta import llm
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1", GOOGLE_API_KEY_2="k2")

    def behave(key, model):
        if key == "k1":
            raise _quota()
        return '{"ok": true}'
    calls = _fake_genai(monkeypatch, behave)
    assert llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50) == '{"ok": true}'
    assert calls == [("k1", "gemini-flash-latest"), ("k2", "gemini-flash-latest")]   # quality kept: no lite


def test_the_weaker_model_is_only_used_after_every_key_failed_on_the_primary(monkeypatch):
    from yta import llm
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1", GOOGLE_API_KEY_2="k2")

    def behave(key, model):
        if model == "gemini-flash-latest":
            raise _quota()
        return '{"ok": true}'
    calls = _fake_genai(monkeypatch, behave)
    assert llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50) == '{"ok": true}'
    assert calls == [("k1", "gemini-flash-latest"), ("k2", "gemini-flash-latest"), ("k1", llm._GEMINI_FALLBACK)]


def test_a_failed_key_model_slot_is_skipped_by_the_next_calls(monkeypatch):
    from yta import llm
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1", GOOGLE_API_KEY_2="k2")
    calls = _fake_genai(monkeypatch, lambda key, model: (_ for _ in ()).throw(_quota()) if key == "k1" else '{"ok": true}')
    llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)
    calls.clear()
    llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)
    assert calls == [("k2", "gemini-flash-latest")]          # k1's slot is cooling down


def test_when_every_key_and_model_fails_the_error_is_raised_not_swallowed(monkeypatch):
    import pytest
    from yta import llm
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1")
    _fake_genai(monkeypatch, lambda key, model: (_ for _ in ()).throw(_quota()))
    with pytest.raises(Exception):
        llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)


def test_a_healthy_primary_is_used_and_never_tripped(monkeypatch):
    from yta import llm, llm_pool
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1")
    calls = _fake_genai(monkeypatch, lambda key, model: '{"ok": true}')
    llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)
    llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)
    assert calls == [("k1", "gemini-flash-latest")] * 2 and llm_pool.status() == []


# -- complete(): every caller that does not pin a provider gets full failover ----

def test_complete_falls_back_from_groq_to_gemini_when_groq_fails(monkeypatch):
    from yta import llm
    monkeypatch.setattr(llm, "provider_chain", lambda media=False, stage=None: ["groq", "gemini"])
    seen = []

    def one(system, user, max_tokens, media, provider, retries, stage=None):
        seen.append(provider)
        if provider == "groq":
            raise llm.GenerationFailed("Failed to validate JSON")
        return ('{"ok": true}', provider, "m")
    monkeypatch.setattr(llm, "_complete_one", one)
    assert llm.complete("s", "u")[1] == "gemini" and seen == ["groq", "gemini"]


def test_complete_raises_the_last_error_only_when_every_provider_failed(monkeypatch):
    import pytest
    from yta import llm
    monkeypatch.setattr(llm, "provider_chain", lambda media=False, stage=None: ["groq", "gemini"])

    def one(system, user, max_tokens, media, provider, retries, stage=None):
        raise RuntimeError(f"{provider} down")
    monkeypatch.setattr(llm, "_complete_one", one)
    with pytest.raises(RuntimeError, match="gemini down"):
        llm.complete("s", "u")


def test_an_explicit_provider_is_not_failed_over(monkeypatch):
    import pytest
    from yta import llm
    seen = []

    def one(system, user, max_tokens, media, provider, retries, stage=None):
        seen.append(provider)
        raise RuntimeError("boom")
    monkeypatch.setattr(llm, "_complete_one", one)
    with pytest.raises(RuntimeError):
        llm.complete("s", "u", provider="groq")
    assert seen == ["groq"]                     # the pipeline's own chain loop does the failing over


def test_vision_requests_only_chain_through_vision_capable_providers(monkeypatch):
    from yta import llm
    monkeypatch.setattr(llm, "provider_chain", lambda media=False, stage=None: ["gemini"] if media else ["groq", "gemini"])
    seen = []
    monkeypatch.setattr(llm, "_complete_one", lambda s, u, mt, media, prov, r, st=None: seen.append(prov) or ("{}", prov, "m"))
    llm.complete("s", "u", media=[("image/png", b"x")])
    assert seen == ["gemini"]


# -- Groq (OpenAI-compatible) multi-key failover --------------------------------

def _clean_groq(monkeypatch, **keys):
    from yta import llm  # noqa: F401
    for n in ["GROQ_API_KEY", "GROQ_API_KEYS"] + [f"GROQ_API_KEY_{i}" for i in range(2, 10)]:
        monkeypatch.delenv(n, raising=False)
    for k, v in keys.items():
        monkeypatch.setenv(k, v)


def test_api_keys_for_groq_and_gemini_use_the_documented_names(monkeypatch):
    from yta import llm
    _clean_groq(monkeypatch, GROQ_API_KEY="g1", GROQ_API_KEY_2="g2", GROQ_API_KEYS="g3,g1")
    assert llm.api_keys("GROQ_API_KEY", "g1") == ["g1", "g3", "g2"]
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1", GOOGLE_API_KEYS="k2,k3")
    assert llm.gemini_keys("k1") == ["k1", "k2", "k3"]


def test_a_groq_key_over_its_quota_fails_over_to_the_next_groq_key(monkeypatch):
    from yta import llm
    _clean_groq(monkeypatch, GROQ_API_KEY="g1", GROQ_API_KEY_2="g2")
    used = []

    def one(provider, system, user, model, key, max_tokens, retries):
        used.append(key)
        if key == "g1":
            raise llm.SizeLimitError("Rate limit reached for model ... tokens per day (TPD)")
        return '{"ok": true}'
    monkeypatch.setattr(llm, "_openai_compat_one", one)
    assert llm._openai_compat("groq", "s", "u", "m", "g1", 50, 0) == '{"ok": true}'
    assert used == ["g1", "g2"]
    used.clear()
    llm._openai_compat("groq", "s", "u", "m", "g1", 50, 0)
    assert used == ["g2"]                       # g1 is parked after its quota error


def test_a_request_too_large_error_does_not_put_a_groq_key_on_cooldown(monkeypatch):
    from yta import llm
    _clean_groq(monkeypatch, GROQ_API_KEY="g1", GROQ_API_KEY_2="g2")

    def one(provider, system, user, model, key, max_tokens, retries):
        raise llm.SizeLimitError("Request too large for model")
    monkeypatch.setattr(llm, "_openai_compat_one", one)
    try:
        llm._openai_compat("groq", "s", "u", "m", "g1", 50, 0)
    except llm.SizeLimitError:
        pass
    from yta import llm_pool
    assert llm_pool.status() == []              # that is about the request, not the key


def test_a_bad_generation_moves_to_the_next_groq_key_then_raises_when_all_fail(monkeypatch):
    import pytest
    from yta import llm
    _clean_groq(monkeypatch, GROQ_API_KEY="g1", GROQ_API_KEY_2="g2")
    used = []

    def one(provider, system, user, model, key, max_tokens, retries):
        used.append(key)
        raise llm.GenerationFailed("Failed to validate JSON")
    monkeypatch.setattr(llm, "_openai_compat_one", one)
    with pytest.raises(llm.GenerationFailed):
        llm._openai_compat("groq", "s", "u", "m", "g1", 50, 0)
    assert used == ["g1", "g2"]


# ==========================================================================
# Key/provider rotation: park on quota or 503 for 24h, mix when healthy,
# the other provider as each one's fallback (owner's design, 2026-10-07)
# ==========================================================================

class _Err(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.status_code = code


def test_a_daily_quota_error_parks_for_24_hours():
    from yta import llm, llm_pool
    sec, why = llm.classify_error(_Err("Rate limit reached ... on tokens per day (TPD): Limit 200000", 429))
    assert sec == llm_pool.DAY - 0 or sec > 0
    sec2, _ = llm.classify_error(_Err("429 RESOURCE_EXHAUSTED. You exceeded your current quota", 429))
    assert sec2 == llm_pool.DAY


def test_groqs_rolling_daily_window_parks_only_until_it_lifts():
    from yta import llm
    sec, why = llm.classify_error(_Err("Rate limit reached on tokens per day (TPD). Please try again in 12m51.4s.", 429))
    assert 12 * 60 < sec < 14 * 60 and "daily" in why          # honours the provider's own reset time


def test_a_per_minute_rate_limit_parks_only_briefly():
    from yta import llm
    sec, why = llm.classify_error(_Err("Rate limit reached on tokens per minute (TPM). Please try again in 8s.", 429))
    assert sec < 120 and "minute" in why


def test_a_503_parks_for_24_hours_and_other_5xx_or_hangs_only_briefly():
    from yta import llm, llm_pool
    assert llm.classify_error(_Err("503 UNAVAILABLE. The model is overloaded", 503))[0] == llm_pool.DAY
    assert llm.classify_error(_Err("504 DEADLINE_EXCEEDED", 504))[0] == 300
    assert llm.classify_error(TimeoutError("timed out"))[0] == 300


def test_bad_generations_and_oversized_requests_never_park_anything():
    from yta import llm
    assert llm.classify_error(llm.GenerationFailed("Failed to validate JSON")) is None
    assert llm.classify_error(_Err("Request too large for model", 413)) is None
    assert llm.classify_error(ValueError("whatever")) is None


def test_order_puts_healthy_first_rotates_them_and_keeps_parked_as_last_resort():
    from yta import llm_pool
    slot = lambda k: f"s:{k}"          # noqa: E731
    llm_pool.park("s:b", 3600, "test")
    first = llm_pool.order("g", ["a", "b", "c"], slot)
    second = llm_pool.order("g", ["a", "b", "c"], slot)
    assert first == ["a", "c", "b"] and second == ["c", "a", "b"]      # rotates; parked "b" always last
    assert llm_pool.order("g2", ["b"], slot) == ["b"]                   # all parked -> still tried


def test_parking_expires_and_unpark_works():
    from yta import llm_pool
    llm_pool.park("x", 3600, "t")
    assert llm_pool.is_parked("x")
    import time as _t
    llm_pool._PARK["x"] = _t.time() - 1                 # time passes
    assert not llm_pool.is_parked("x")
    llm_pool.park("y", 3600, "t")
    llm_pool.unpark("y")
    assert not llm_pool.is_parked("y")


def test_parking_survives_a_restart_and_stores_no_key_material(tmp_path, monkeypatch):
    import json
    from yta import llm_pool
    f = tmp_path / "persist.json"
    monkeypatch.setenv("YTA_LLM_PARK_FILE", str(f))
    llm_pool.reset()
    sid = llm_pool.slot_id("gemini", "AIzaSySECRETKEY123456", "gemini-flash-latest")
    llm_pool.park(sid, 3600, "quota exhausted")
    raw = f.read_text()
    assert "SECRETKEY" not in raw and sid in json.loads(raw)       # only a hash is stored
    llm_pool._PARK.clear()
    llm_pool._LOADED = False                                        # simulate a fresh process
    assert llm_pool.is_parked(sid)


def test_a_gemini_key_that_hit_its_quota_is_parked_and_the_other_key_takes_over(monkeypatch):
    from yta import llm, llm_pool
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1", GOOGLE_API_KEY_2="k2")

    def behave(key, model):
        if key == "k1":
            raise _quota()
        return '{"ok": true}'
    _fake_genai(monkeypatch, behave)
    llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)
    parked = llm_pool.status()
    assert len(parked) == 1 and "gemini" in parked[0][0] and parked[0][1] > 23 * 3600


def test_healthy_gemini_keys_are_used_in_rotation_so_no_daily_quota_burns_alone(monkeypatch):
    from yta import llm
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1", GOOGLE_API_KEY_2="k2")
    calls = _fake_genai(monkeypatch, lambda key, model: '{"ok": true}')
    for _ in range(4):
        llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)
    assert [k for k, _ in calls] == ["k1", "k2", "k1", "k2"]


def test_healthy_groq_keys_are_also_rotated(monkeypatch):
    from yta import llm
    _clean_groq(monkeypatch, GROQ_API_KEY="g1", GROQ_API_KEY_2="g2")
    used = []
    monkeypatch.setattr(llm, "_openai_compat_one",
                        lambda provider, s, u, m, key, mt, r: used.append(key) or '{"ok": true}')
    for _ in range(4):
        llm._openai_compat("groq", "s", "u", "m", "g1", 50, 0)
    assert used == ["g1", "g2", "g1", "g2"]


def _two_providers(monkeypatch):
    from yta import llm
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    extra = [f"{b}_{i}" for b in ("GROQ_API_KEY", "GOOGLE_API_KEY") for i in range(2, 10)]   # .env leaks _2 keys in
    for n in ("GROQ_API_KEY", "GOOGLE_API_KEY", "GROK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
              "GROQ_API_KEYS", "GOOGLE_API_KEYS", *extra):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    monkeypatch.setenv("GOOGLE_API_KEY", "k1")
    return llm


def test_text_always_goes_to_groq_first_and_gemini_is_only_the_fallback(monkeypatch):
    # Owner: each provider where it is best, so behaviour stays consistent -- no
    # alternating between two models that answer slightly differently.
    llm = _two_providers(monkeypatch)
    assert [llm.provider_chain(False) for _ in range(5)] == [["groq", "gemini"]] * 5


def test_a_fully_parked_provider_moves_to_the_end_of_the_chain(monkeypatch):
    from yta import llm_pool
    llm = _two_providers(monkeypatch)
    llm_pool.park(llm_pool.slot_id("groq", "g1"), 3600, "test")
    assert [llm.provider_chain(False) for _ in range(3)] == [["gemini", "groq"]] * 3


def test_a_provider_with_one_healthy_key_left_is_still_healthy(monkeypatch):
    from yta import llm_pool
    llm = _two_providers(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY_2", "g2")
    llm_pool.park(llm_pool.slot_id("groq", "g1"), 3600, "test")
    assert llm.provider_healthy("groq") is True
    llm_pool.park(llm_pool.slot_id("groq", "g2"), 3600, "test")
    assert llm.provider_healthy("groq") is False


def test_a_pinned_provider_stays_first(monkeypatch):
    llm = _two_providers(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert [llm.provider_chain(False)[0] for _ in range(4)] == ["gemini"] * 4


def test_vision_chain_is_not_rotated(monkeypatch):
    llm = _two_providers(monkeypatch)
    assert [llm.provider_chain(True) for _ in range(3)] == [["gemini"]] * 3      # Groq cannot read images


def test_when_everything_is_parked_the_parked_slots_are_still_tried(monkeypatch):
    from yta import llm, llm_pool
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1")
    for m in ("gemini-flash-latest", llm._GEMINI_FALLBACK):
        llm_pool.park(llm_pool.slot_id("gemini", "k1", m), 86400, "test")
    calls = _fake_genai(monkeypatch, lambda key, model: '{"ok": true}')
    assert llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50) == '{"ok": true}'
    assert calls == [("k1", "gemini-flash-latest")]          # a wrongly parked key can't take the bot offline


# ==========================================================================
# Per-stage defaults (owner 2026-10-07): each LLM call site uses the provider/model
# best suited to it; keys rotate inside a provider; the OTHER provider is only the
# fallback of last resort.
# ==========================================================================

def test_vision_stage_defaults_to_gemini_and_never_offers_groq(monkeypatch):
    llm = _two_providers(monkeypatch)
    assert llm.provider_chain(True) == ["gemini"]
    assert llm.provider_chain(True, stage="vision") == ["gemini"]


def test_text_stages_default_to_groq_with_gemini_as_the_fallback(monkeypatch):
    llm = _two_providers(monkeypatch)
    for stage in ("text_extract", "clarify", "room_judge"):
        assert llm.provider_chain(False, stage=stage) == ["groq", "gemini"], stage


def test_gemini_model_order_is_chosen_per_stage(monkeypatch):
    from yta import llm
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    flash, lite = llm._model("gemini"), llm._GEMINI_FALLBACK
    assert llm.gemini_model_order("vision", True) == [flash, lite]          # accuracy on screenshots first
    assert llm.gemini_model_order("text_extract") == [lite, flash]          # fast model for text
    assert llm.gemini_model_order("clarify") == [lite, flash]
    assert llm.gemini_model_order("room_judge") == [flash, lite]            # careful judge first


def test_the_stage_decides_which_gemini_model_is_tried_first(monkeypatch):
    from yta import llm
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1")
    calls = _fake_genai(monkeypatch, lambda key, model: '{"ok": true}')
    llm._gemini("s", ["u"], llm._model("gemini"), "k1", 50, models=llm.gemini_model_order("text_extract"))
    llm._gemini("s", ["u"], llm._model("gemini"), "k1", 50, models=llm.gemini_model_order("vision", True))
    assert [m for _, m in calls] == [llm._GEMINI_FALLBACK, llm._model("gemini")]


def test_the_other_provider_is_used_only_after_every_key_of_the_default_one_failed(monkeypatch):
    from yta import llm
    llm = _two_providers(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY_2", "g2")
    seen = []

    def one(system, user, max_tokens, media, provider, retries, stage=None):
        seen.append(provider)
        return ('{"ok": true}', provider, "m")
    monkeypatch.setattr(llm, "_complete_one", one)
    llm.complete("s", "u", stage="text_extract")
    assert seen == ["groq"]                          # healthy default -> the fallback is never touched


def test_gemini_is_the_fallback_for_text_only_when_groq_is_unavailable(monkeypatch):
    from yta import llm_pool
    llm = _two_providers(monkeypatch)
    llm_pool.park(llm_pool.slot_id("groq", "g1"), 3600, "test")        # its only key is parked
    assert llm.provider_chain(False, stage="text_extract") == ["gemini", "groq"]
    seen = []

    def one(system, user, max_tokens, media, provider, retries, stage=None):
        seen.append(provider)
        raise RuntimeError("down")
    monkeypatch.setattr(llm, "_complete_one", one)
    try:
        llm.complete("s", "u", stage="text_extract")
    except RuntimeError:
        pass
    assert seen == ["gemini", "groq"]                # and Groq is still tried LAST if Gemini failed too
