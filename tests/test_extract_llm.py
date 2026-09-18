"""yta/extract_llm.py::extract() -- the model is asked for "a single JSON
object" but occasionally wraps its answer in a list instead (seen live on
informal free text, e.g. a message that embeds a direct question alongside
the booking details, over WhatsApp v4's text-as-submission path). Regression
for the crash that caused: AttributeError: 'list' object has no attribute
'get' at the old `data.get("field_confidence", ...)` call.
"""
from yta.extract_llm import extract


def test_top_level_list_response_does_not_crash(monkeypatch):
    # Reproduces the exact shape seen live: the model wrapped its single
    # answer object in a JSON array.
    monkeypatch.setattr(
        "yta.llm.complete",
        lambda *a, **kw: ('[{"hotel": {"name": "Taj Santacruz"}}]', "groq", "test-model"),
    )
    result = extract("some free text", url="")
    assert result.fields.get("hotel.name") == "Taj Santacruz"
    assert result.confidence == {} or isinstance(result.confidence, dict)
    assert result.contradictions == []


def test_top_level_list_with_no_dict_entries_yields_empty_fields(monkeypatch):
    monkeypatch.setattr(
        "yta.llm.complete",
        lambda *a, **kw: ('["not even an object"]', "groq", "test-model"),
    )
    result = extract("some free text", url="")
    assert result.fields == {}


def test_top_level_non_dict_non_list_yields_empty_fields(monkeypatch):
    monkeypatch.setattr(
        "yta.llm.complete",
        lambda *a, **kw: ('"just a string"', "groq", "test-model"),
    )
    result = extract("some free text", url="")
    assert result.fields == {}


def test_normal_dict_response_still_works(monkeypatch):
    monkeypatch.setattr(
        "yta.llm.complete",
        lambda *a, **kw: ('{"hotel": {"name": "Atlantis"}, "field_confidence": {"hotel.name": 0.9}}',
                          "groq", "test-model"),
    )
    result = extract("some free text", url="")
    assert result.fields.get("hotel.name") == "Atlantis"
    assert result.confidence.get("hotel.name") == 0.9
