import io
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from calendar_notification_monitor.fetch import FetchFailure, cache_metadata, fetch
from calendar_notification_monitor.model import MAX_BYTES, SOURCE_URL


class Reply(io.BytesIO):
    def __init__(self, data=b"", code=200, headers=None):
        super().__init__(data)
        self.code = code
        self.headers = headers or {}


def test_anonymous_fixed_url_and_conditional_get(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-github-token")
    monkeypatch.setenv("GOOGLE_TOKEN", "private-google-token")
    def opener(request, timeout):
        assert request.full_url == SOURCE_URL
        assert timeout == 20
        assert request.get_header("If-none-match") == '"abc123"'
        assert request.get_header("If-modified-since") == "Mon, 05 Oct 2026 00:00:00 GMT"
        assert "Authorization" not in request.headers
        assert all("private-" not in value for value in request.headers.values())
        return Reply(b"synthetic")
    payload, status, metadata = fetch(
        {"etag": '"abc123"', "last_modified": "Mon, 05 Oct 2026 00:00:00 GMT"}, opener=opener,
    )
    assert payload == b"synthetic"
    assert status == 200
    assert metadata["conditional"] is True
    assert metadata["started_at"] <= metadata["finished_at"]


def test_304_http_error_is_not_a_retry():
    def opener(request, timeout):
        raise HTTPError(SOURCE_URL, 304, "not modified", Message(), io.BytesIO(b"excluded body"))
    payload, status, metadata = fetch({"etag": '"abc123"'}, opener=opener)
    assert payload is None and status == 304
    assert len(metadata["attempts"]) == 1
    assert "excluded" not in str(metadata)


@pytest.mark.parametrize("status", [302, 401, 403, 404])
def test_nonretry_status_does_not_follow_or_log_body(status):
    calls = []
    def opener(request, timeout):
        calls.append(request)
        return Reply(b"password=PRIVATE", status, {"Location": "https://private.example.invalid"})
    with pytest.raises(FetchFailure) as failure:
        fetch(opener=opener, sleep=lambda delay: None)
    assert len(calls) == 1
    assert "PRIVATE" not in str(failure.value.request_metadata)
    assert "private.example" not in str(failure.value.request_metadata)


@pytest.mark.parametrize("mode", ["timeout", "http", "truncated"])
def test_bounded_retries(mode):
    calls, waits = [], []
    def opener(request, timeout):
        calls.append(request)
        if mode == "timeout":
            raise URLError("password=PRIVATE")
        if mode == "http":
            return Reply(b"password=PRIVATE", 503)
        return Reply(b"short", headers={"Content-Length": "10"})
    with pytest.raises(FetchFailure) as failure:
        fetch(opener=opener, sleep=waits.append)
    assert len(calls) == 3
    assert waits == [1, 2]
    assert len(failure.value.request_metadata["attempts"]) == 3
    assert "PRIVATE" not in str(failure.value)
    assert "PRIVATE" not in str(failure.value.request_metadata)


def test_retry_can_succeed():
    replies = iter([Reply(code=503), Reply(b"ok")])
    payload, status, metadata = fetch(opener=lambda req, timeout: next(replies), sleep=lambda delay: None)
    assert payload == b"ok" and status == 200
    assert len(metadata["attempts"]) == 2


@pytest.mark.parametrize("headers", [
    {"Content-Length": str(MAX_BYTES + 1)},
    {"Content-Length": "invalid"},
    {"Content-Encoding": "gzip"},
])
def test_invalid_or_oversize_response(headers):
    with pytest.raises(FetchFailure):
        fetch(opener=lambda req, timeout: Reply(headers=headers))


def test_cache_headers_are_a_sanitized_allowlist():
    headers = {
        "ETag": '"public123"', "Last-Modified": "Mon, 05 Oct 2026 00:00:00 GMT",
        "Date": "Mon, 05 Oct 2026 00:01:00 GMT", "Age": "12",
        "Cache-Control": "public, max-age=300, private-secret=PASSWORD",
        "Location": "https://private.invalid", "Set-Cookie": "token=PASSWORD",
    }
    result = cache_metadata(headers)
    assert result["cache_control"] == ["max-age=300", "public"]
    assert "PASSWORD" not in str(result)
    assert "private.invalid" not in str(result)
    assert cache_metadata({"ETag": '"token=PASSWORD"', "Age": "private", "Date": "private"}) == {}
