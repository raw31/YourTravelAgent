import pytest


@pytest.fixture(autouse=True)
def _isolated_llm_pool(tmp_path, monkeypatch):
    """Parking state is process-global and persisted; no test may see another's
    (or write into the real data/ directory)."""
    monkeypatch.setenv("YTA_LLM_PARK_FILE", str(tmp_path / "llm_parking.json"))
    from yta import llm_pool
    llm_pool.reset()
    yield
    llm_pool.reset()
