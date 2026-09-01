"""Real-world occupancy cases from pasted OTA links — regression corpus.

Add a case: append to tests/fixtures/occupancy_cases.json. `kind:"url"`
checks signals_from_url + resolve; `kind:"api"` checks the render digest
surfaces the per-room split the LLM then reads.
"""
import json
from pathlib import Path

import pytest

from yta.occupancy import signals_from_url, resolve
from yta.render import _json_digest

FIX = Path(__file__).parent / "fixtures"
CASES = json.loads((FIX / "occupancy_cases.json").read_text())


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "url"],
                         ids=lambda c: c["name"])
def test_url_case(case):
    occ, conf, src, _ = resolve(signals_from_url(case["url"]))
    assert occ == case["expect"], f"{case['name']}: {src}"
    assert conf >= case.get("min_conf", 0.0)


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "api"],
                         ids=lambda c: c["name"])
def test_api_case(case):
    body = json.loads((FIX / case["api_fixture"]).read_text())
    digest = _json_digest([{"url": "x", "kind": "response", "body": body}], 9000)
    for frag in case.get("digest_must_contain", []):
        assert frag in digest, f"{case['name']}: {frag!r} missing from digest"
    # the digest must let a reader reconstruct the expected per-room split
    for i, room in enumerate(case["expect"]):
        assert f"roomGuests[{i}].adultString = {room['adults']} adult" in digest
