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
    """behaviour(api_key, model) -> text or raises. Records (key, model) tried."""
    import sys
    import types as _t
    calls = []

    class _Client:
        def __init__(self, api_key=None, **kw):
            self._key = api_key
            self.models = self

        def generate_content(self, model, contents, config):
            calls.append((self._key, model))
            return _t.SimpleNamespace(text=behaviour(self._key, model))

    from google import genai as real_genai          # keep .types etc.; only swap the client
    monkeypatch.setattr(real_genai, "Client", _Client)
    return calls


def _quota():
    from google.genai.errors import ClientError
    return ClientError(429, {"error": {"message": "quota exceeded"}})


def _clean_env(monkeypatch, **keys):
    from yta import llm
    llm._GEMINI_SKIP_UNTIL.clear()
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
    from yta import llm
    _clean_env(monkeypatch, GOOGLE_API_KEY="k1")
    calls = _fake_genai(monkeypatch, lambda key, model: '{"ok": true}')
    llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)
    llm._gemini("s", ["u"], "gemini-flash-latest", "k1", 50)
    assert calls == [("k1", "gemini-flash-latest")] * 2 and not llm._GEMINI_SKIP_UNTIL
