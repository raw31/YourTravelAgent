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
