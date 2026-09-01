"""Low-level TripJack Hotel API v3 HTTP client.

Auth: `apikey` request header (v3 breaking change — no more Bearer token).
Errors: TripJack returns a `{status.success:false, error:{code,message}}`
envelope, often with HTTP 200 — so success is checked from the body, not
the status line.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except ImportError:
    pass

# test hosts (docs §"Quick host/endpoint reference"). Prod differs — set via env.
_HOSTS = {
    "test": {
        "hms": "apitest-hms.tripjack.com",           # listing / pricing / review / static-detail
        "oms": "apitest-hotel-booker.tripjack.com",  # book / confirm / cancel / booking-details
        "gw": "apitest.tripjack.com",                # nationality-info / fetch-static-hotels
    },
}

# error codes that are worth retrying
_RETRY_CODES = {"SUPPLIER_UNAVAILABLE", "6514", "6516", "INTERNAL_ERROR",
                "SUPPLIER_TIMEOUT", "6515"}
_RATE_CODES = {"RATE_LIMITED", "6517"}
# codes that mean "the session died, re-search" — surfaced, never retried here
_SESSION_DEAD = {"SEARCH_SESSION_EXPIRED", "6502", "INVALID_SEARCH_ID",
                 "HOTEL_ID_MISMATCH_WITH_SESSION", "6503"}


class TripJackError(RuntimeError):
    def __init__(self, code, message, http_status=None, request_id=None):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.request_id = request_id
        super().__init__(f"[{code}] {message}"
                         + (f" (HTTP {http_status})" if http_status else ""))

    @property
    def session_dead(self) -> bool:
        return str(self.code) in _SESSION_DEAD


class TripJackClient:
    def __init__(self, api_key: str | None = None, *, env: str = "test",
                 nationality: str = "106", timeout: float = 25.0,
                 hosts: dict | None = None):
        self.api_key = api_key or os.environ.get("TRIPJACK_API_KEY", "")
        self.env = env or os.environ.get("TRIPJACK_ENV", "test")
        self.nationality = nationality or os.environ.get("TRIPJACK_NATIONALITY", "106")
        self.timeout = timeout
        self.hosts = hosts or _HOSTS.get(self.env) or _HOSTS["test"]

    @classmethod
    def from_env(cls) -> "TripJackClient":
        return cls(
            api_key=os.environ.get("TRIPJACK_API_KEY", ""),
            env=os.environ.get("TRIPJACK_ENV", "test"),
            nationality=os.environ.get("TRIPJACK_NATIONALITY", "106"),
        )

    def configured(self) -> bool:
        return bool(self.api_key)

    # -- transport --------------------------------------------------

    def _post(self, group: str, path: str, body: dict, *, retries: int = 3) -> dict:
        import requests

        if not self.api_key:
            raise TripJackError("UNAUTHORIZED",
                                "TRIPJACK_API_KEY not set (add it to .env)", 401)
        url = f"https://{self.hosts[group]}{path}"
        headers = {"apikey": self.api_key, "Content-Type": "application/json",
                   "Accept": "application/json"}

        for attempt in range(retries + 1):
            resp = requests.post(url, json=body, headers=headers, timeout=self.timeout)
            try:
                data = resp.json()
            except ValueError:
                raise TripJackError("INVALID_RESPONSE",
                                    f"non-JSON response: {resp.text[:200]}",
                                    resp.status_code)

            if data.get("status", {}).get("success"):
                return data

            # TripJack uses either {error:{code,message}} or {errors:[{errCode,message}]}
            err = data.get("error") or {}
            if not err and data.get("errors"):
                e0 = data["errors"][0] or {}
                err = {"code": e0.get("errCode") or e0.get("code"),
                       "message": e0.get("message")}
            code = str(err.get("code")
                       or data.get("status", {}).get("httpStatus")
                       or resp.status_code)
            msg = err.get("message") or "request failed"
            rid = err.get("requestId") or data.get("requestId")

            if code in _RATE_CODES or resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                if attempt < retries:
                    time.sleep(min(wait, 30))
                    continue
            elif code in _RETRY_CODES or resp.status_code == 503:
                if attempt < retries:
                    time.sleep(2 ** attempt)          # 1s, 2s, 4s
                    continue

            raise TripJackError(err.get("code") or code, msg, resp.status_code, rid)

        raise TripJackError("RETRIES_EXHAUSTED", "gave up after retries")

    # -- endpoints -------------------------------------------------

    def listing(self, *, hids, check_in, check_out, rooms, currency, nationality,
                correlation_id, timeout_ms: int = 13000) -> dict:
        return self._post("hms", "/hms/v3/hotel/listing", {
            "checkIn": check_in, "checkOut": check_out, "rooms": rooms,
            "currency": currency, "correlationId": correlation_id,
            "nationality": nationality, "timeoutMs": timeout_ms,
            "hids": list(hids),
        })

    @staticmethod
    def pricing_body(*, hid, check_in, check_out, rooms, currency, nationality,
                     correlation_id, timeout_ms: int = 13000) -> dict:
        """The exact JSON body POSTed to /hms/v3/hotel/pricing."""
        return {
            "correlationId": correlation_id, "hid": str(hid),
            "checkIn": check_in, "checkOut": check_out, "rooms": rooms,
            "currency": currency, "nationality": nationality,
            "timeoutMs": timeout_ms,
        }

    PRICING_PATH = "/hms/v3/hotel/pricing"
    PRICING_URL_TEST = "https://apitest-hms.tripjack.com/hms/v3/hotel/pricing"

    def pricing(self, **kw) -> dict:
        return self._post("hms", self.PRICING_PATH, self.pricing_body(**kw))

    def review(self, *, correlation_id, option_id, review_hash, hid) -> dict:
        return self._post("hms", "/hms/v3/hotel/review", {
            "correlationId": correlation_id, "optionId": option_id,
            "reviewHash": review_hash, "hid": str(hid),
        })

    def static_detail(self, hid) -> dict:
        return self._post("hms", "/hms/v3/hotel/static-detail", {"hid": str(hid)})

    def nationalities(self) -> dict:
        import requests
        url = f"https://{self.hosts['gw']}/hms/v3/nationality-info"
        r = requests.get(url, headers={"apikey": self.api_key}, timeout=self.timeout)
        return r.json()
