"""
Generic LLM-based OTA page parser (fallback for OTAs without a dedicated adapter)

This is the catch-all branch: for any OTA you haven't built a dedicated
adapter for, render/fetch the page, strip it to clean text, and let an
LLM extract your standard Booking Intent packet fields from it.

IMPORTANT: this does NOT replace the fetch step. You still need to get
page content first (Playwright for unknown JS-rendered sites is the safe
default; plain HTTP if you've confirmed the site is server-rendered).
This module only turns page text into structured fields.

Usage:
    export ANTHROPIC_API_KEY=...
    from llm_generic_parser import extract_packet_via_llm
    packet = extract_packet_via_llm(page_text, source_url="https://...")
"""
import os
import json
import re
import anthropic

SCHEMA_INSTRUCTIONS = """
You extract hotel booking details from OTA (Online Travel Agency) page
text and return ONLY a JSON object matching this exact schema. Use null
for any field you cannot find with confidence — never guess or infer a
value that isn't clearly stated in the text.

{
  "hotel": {
    "name": string or null,
    "address": string or null
  },
  "stay": {
    "check_in": "YYYY-MM-DD" or null,
    "check_out": "YYYY-MM-DD" or null,
    "nights": number or null,
    "rooms": number or null,
    "adults": number or null,
    "children": number or null
  },
  "requested_offer": {
    "room_name": string or null,
    "description": string or null,
    "meal_plan": string or null,
    "cancellation": string or null
  },
  "ota_offer": {
    "final_payable": number or null,
    "currency": "INR"/"USD"/etc or null
  },
  "confidence": {
    // one entry per top-level field above that you filled in,
    // 0.0-1.0, reflecting how explicit/unambiguous the source text was
    "hotel.name": number,
    "stay.check_in": number,
    ...
  }
}

Rules:
- Only use information explicitly present in the provided text.
- Do not fill in typical/default values for missing fields.
- final_payable should be the TOTAL amount the guest pays (including
  taxes/fees), not a subtotal, if both are present.
- Output ONLY the JSON object. No preamble, no markdown fences.
"""


def clean_page_text(raw_text: str, max_chars: int = 15000) -> str:
    """Trim noise before sending to the LLM — cuts cost and improves
    accuracy by removing boilerplate that isn't booking-relevant."""
    noise_patterns = [
        r"(?i)^(sign in|register|list your property).*$",
        r"(?i)^(cookie|privacy notice|terms of service).*$",
        r"^\+?\d{1,3}\s?\(?\+\d+\)?$",  # stray phone country-code lines
    ]
    lines = raw_text.splitlines()
    kept = [
        ln for ln in lines
        if ln.strip() and not any(re.match(p, ln.strip()) for p in noise_patterns)
    ]
    text = "\n".join(kept)
    return text[:max_chars]


def extract_packet_via_llm(page_text: str, source_url: str = "") -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    cleaned = clean_page_text(page_text)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SCHEMA_INSTRUCTIONS,
        messages=[
            {
                "role": "user",
                "content": f"Source URL: {source_url}\n\nPage text:\n{cleaned}",
            }
        ],
    )

    raw = response.content[0].text.strip()
    # Defensive: strip markdown fences if the model adds them anyway
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()

    try:
        extracted = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "error": "LLM output was not valid JSON",
            "raw_output": raw,
            "source": {"ota": "unknown", "url": source_url, "extraction_method": "llm_fallback"},
        }

    extracted["source"] = {
        "ota": "unknown",
        "url": source_url,
        "extraction_method": "llm_fallback",
    }
    return extracted


def validate_packet(packet: dict) -> list:
    """Basic sanity checks before this packet is trusted downstream.
    Returns a list of warning strings (empty list = looks sane)."""
    warnings = []
    stay = packet.get("stay", {})
    offer = packet.get("ota_offer", {})

    price = offer.get("final_payable")
    if price is not None and (not isinstance(price, (int, float)) or price <= 0):
        warnings.append(f"Suspicious price value: {price}")

    for field in ("check_in", "check_out"):
        val = stay.get(field)
        if val and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(val)):
            warnings.append(f"stay.{field} not in YYYY-MM-DD format: {val}")

    adults = stay.get("adults")
    if adults is not None and (not isinstance(adults, int) or adults < 1 or adults > 20):
        warnings.append(f"Suspicious adults count: {adults}")

    # Low-confidence fields are worth a warning too, not a hard failure —
    # your matching engine (Section 9) can decide how to treat these.
    for field, conf in packet.get("confidence", {}).items():
        if isinstance(conf, (int, float)) and conf < 0.5:
            warnings.append(f"Low confidence on {field}: {conf}")

    return warnings


if __name__ == "__main__":
    # Quick manual test: paste rendered page text below
    sample_text = "PASTE PAGE TEXT HERE FOR TESTING"
    packet = extract_packet_via_llm(sample_text, source_url="https://example-ota.com/...")
    print(json.dumps(packet, indent=2))
    warnings = validate_packet(packet)
    if warnings:
        print("\nValidation warnings:")
        for w in warnings:
            print(f"  - {w}")
