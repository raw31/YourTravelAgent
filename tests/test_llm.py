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
