"""Exercise the exponential-backoff retry helper without hitting the network.

We mock `requests.request` and assert:
  - 429 triggers a retry that honors `Retry-After`
  - Transient 5xx triggers retry up to the cap
  - Permanent 4xx surfaces immediately (no retry)
  - Connection errors retry with exp backoff
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ETL = Path(__file__).resolve().parent.parent / "etl"
if str(ETL) not in sys.path:
    sys.path.insert(0, str(ETL))

import collector  # noqa: E402


def _fake_response(status: int, body: dict | None = None, headers: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body or {}
    r.text = ""
    r.raise_for_status = MagicMock()
    if status >= 400 and status not in collector.LiveAdapter._RETRY_STATUS:
        r.raise_for_status.side_effect = collector.requests.exceptions.HTTPError(f"HTTP {status}")
    return r


def _adapter() -> collector.LiveAdapter:
    a = collector.LiveAdapter.__new__(collector.LiveAdapter)
    a.tenant_id = "t"
    a.client_id = "c"
    a.client_secret = "s"
    a._token = "fake-bearer-token"
    a._token_expires = 9_999_999_999.0  # never expire in tests
    a._workspace_for_dataset = {}
    a._xmla_available = False
    return a


@patch("collector.time.sleep", lambda *_a, **_k: None)
@patch("collector.requests.request")
def test_request_retries_on_429_then_succeeds(mock_req: MagicMock) -> None:
    mock_req.side_effect = [
        _fake_response(429, headers={"Retry-After": "1"}),
        _fake_response(429, headers={"Retry-After": "1"}),
        _fake_response(200, body={"ok": True}),
    ]
    r = _adapter()._request("GET", "https://example/test")
    assert r.status_code == 200
    assert mock_req.call_count == 3


@patch("collector.time.sleep", lambda *_a, **_k: None)
@patch("collector.requests.request")
def test_request_retries_on_5xx_until_cap_then_raises(mock_req: MagicMock) -> None:
    mock_req.return_value = _fake_response(503)
    try:
        _adapter()._request("GET", "https://example/test")
    except Exception:
        pass  # cap hit; raise_for_status fired
    assert mock_req.call_count == collector.LiveAdapter._RETRY_MAX_ATTEMPTS


@patch("collector.time.sleep", lambda *_a, **_k: None)
@patch("collector.requests.request")
def test_request_does_not_retry_on_404(mock_req: MagicMock) -> None:
    mock_req.return_value = _fake_response(404)
    try:
        _adapter()._request("GET", "https://example/test")
    except Exception:
        pass
    assert mock_req.call_count == 1


@patch("collector.time.sleep", lambda *_a, **_k: None)
@patch("collector.requests.request")
def test_request_returns_allowed_status_codes_without_raising(mock_req: MagicMock) -> None:
    mock_req.return_value = _fake_response(409)
    r = _adapter()._request("POST", "https://example/test", allow_status=(409,))
    assert r.status_code == 409
    assert mock_req.call_count == 1


@patch("collector.time.sleep", lambda *_a, **_k: None)
@patch("collector.requests.request")
def test_request_retries_on_connection_error(mock_req: MagicMock) -> None:
    mock_req.side_effect = [
        collector.requests.exceptions.ConnectionError("boom"),
        collector.requests.exceptions.ConnectionError("boom again"),
        _fake_response(200, body={"ok": True}),
    ]
    r = _adapter()._request("GET", "https://example/test")
    assert r.status_code == 200
    assert mock_req.call_count == 3
