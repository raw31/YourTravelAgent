"""yta/web.py's EXTENSION_KEY gate on /api/extract, /api/review, /api/job --
the check that keeps those routes from being free-for-anyone once the
server is reachable from outside localhost (the AWS box, behind Caddy).
Each hits a real ThreadingHTTPServer on an ephemeral port, since
BaseHTTPRequestHandler isn't unit-testable without a live socket."""
import threading

import pytest
import requests

from yta import web


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(web, "EXTENSION_KEY", "s3cr3t")
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        t.join(timeout=2)


def test_api_extract_rejects_missing_key(server):
    r = requests.post(f"{server}/api/extract", json={"url": ""})
    assert r.status_code == 401


def test_api_extract_rejects_wrong_key(server):
    r = requests.post(f"{server}/api/extract", json={"url": ""},
                       headers={"X-YTA-Key": "wrong"})
    assert r.status_code == 401


def test_api_extract_accepts_correct_key(server):
    r = requests.post(f"{server}/api/extract", json={"url": ""},
                       headers={"X-YTA-Key": "s3cr3t"})
    assert r.status_code == 202


def test_api_job_rejects_missing_key(server):
    r = requests.get(f"{server}/api/job?id=whatever")
    assert r.status_code == 401


def test_api_job_accepts_correct_key(server):
    # unknown job id -- 404, not 401, once past the auth gate
    r = requests.get(f"{server}/api/job?id=whatever",
                      headers={"X-YTA-Key": "s3cr3t"})
    assert r.status_code == 404


def test_webhook_whatsapp_unaffected_by_the_key(server, monkeypatch):
    # Meta calls this one, not the extension -- never gated on X-YTA-Key.
    monkeypatch.setattr("yta.whatsapp.verify_challenge", lambda q: None)
    r = requests.get(f"{server}/webhook/whatsapp")
    assert r.status_code == 403   # verify_challenge said no -- not 401 (auth gate)


def test_rejected_request_does_not_corrupt_the_next_one_on_keepalive(server):
    # Regression: the 401 branch used to fire BEFORE the request body was
    # read off the socket. On a reused HTTP/1.1 keep-alive connection (what
    # Caddy uses reverse-proxying to this server) the unread bytes of a
    # rejected request's body stayed in the stream and glued onto the next
    # request line, so a wrong-key call intermittently broke the very next
    # call too. Regardless of order, both these should get real answers.
    with requests.Session() as s:
        r1 = s.post(f"{server}/api/extract", json={"url": ""},
                     headers={"X-YTA-Key": "wrong"})
        assert r1.status_code == 401
        r2 = s.post(f"{server}/api/extract", json={"url": ""},
                     headers={"X-YTA-Key": "s3cr3t"})
        assert r2.status_code == 202


def test_no_key_configured_means_no_gate(monkeypatch):
    monkeypatch.setattr(web, "EXTENSION_KEY", "")
    httpd = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        r = requests.post(f"http://127.0.0.1:{port}/api/extract", json={"url": ""})
        assert r.status_code == 202
    finally:
        httpd.shutdown()
        t.join(timeout=2)
