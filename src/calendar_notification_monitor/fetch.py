import re
import time
from email.utils import format_datetime, parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .model import MAX_BYTES, SOURCE_URL, MonitorError, utc_now

TIMEOUT_SECONDS = 20
ATTEMPT_DEADLINE_SECONDS = 90
MAX_ATTEMPTS = 3
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cache_metadata(headers):
    metadata = {}
    etag = headers.get("ETag", "")
    if re.fullmatch(r'(?:W/)?"[A-Za-z0-9._:\-]{1,200}"', etag):
        metadata["etag"] = etag
    for source, target in (("Last-Modified", "last_modified"), ("Date", "date")):
        value = headers.get(source, "")
        if len(value) > 128:
            continue
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            continue
        if parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0:
            metadata[target] = format_datetime(parsed, usegmt=True)
    age = headers.get("Age", "")
    if re.fullmatch(r"[0-9]{1,10}", age):
        metadata["age_seconds"] = int(age)
    directives = []
    for directive in headers.get("Cache-Control", "").lower().split(","):
        directive = directive.strip()
        if re.fullmatch(r"(?:public|private|no-cache|no-store|must-revalidate|(?:s-maxage|max-age)=[0-9]{1,10})", directive):
            directives.append(directive)
    if directives:
        metadata["cache_control"] = sorted(set(directives))
    return metadata


class FetchFailure(MonitorError):
    def __init__(self, code, request_metadata):
        super().__init__(code)
        self.request_metadata = request_metadata


def fetch(cache=None, opener=None, sleep=time.sleep):
    cache = cache or {}
    headers = {
        "User-Agent": "calendar-notification-monitor/0.1",
        "Accept": "text/calendar",
        "Accept-Encoding": "identity",
    }
    if cache.get("etag"):
        headers["If-None-Match"] = cache["etag"]
    if cache.get("last_modified"):
        headers["If-Modified-Since"] = cache["last_modified"]
    # Google requests do not use environment proxy credentials or GitHub auth.
    opener = opener or build_opener(ProxyHandler({}), NoRedirect()).open
    metadata = {
        "started_at": utc_now(), "finished_at": None,
        "conditional": "If-None-Match" in headers or "If-Modified-Since" in headers,
        "attempts": [],
    }
    for number in range(1, MAX_ATTEMPTS + 1):
        attempt = {"number": number, "started_at": utc_now()}
        retry = False
        code = "retrieval_failed"
        response = None
        try:
            deadline = time.monotonic() + ATTEMPT_DEADLINE_SECONDS
            try:
                response = opener(Request(SOURCE_URL, headers=headers), timeout=TIMEOUT_SECONDS)
            except HTTPError as exc:
                response = exc
            status = response.code
            attempt["http_status"] = status
            attempt["cache"] = cache_metadata(response.headers)
            if status == 304:
                attempt["finished_at"] = utc_now()
                metadata["attempts"].append(attempt)
                metadata["finished_at"] = attempt["finished_at"]
                return None, status, metadata
            if status != 200:
                code = "http_status_error"
                retry = status in RETRY_STATUSES
            else:
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding not in {"", "identity"}:
                    raise MonitorError("unsupported_content_encoding")
                length = response.headers.get("Content-Length")
                if length is not None and not re.fullmatch(r"[0-9]{1,12}", length):
                    raise MonitorError("invalid_content_length")
                if length is not None and int(length) > MAX_BYTES:
                    raise MonitorError("feed_too_large")
                chunks = []
                size = 0
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError
                    read = getattr(response, "read1", response.read)
                    chunk = read(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise MonitorError("feed_too_large")
                    chunks.append(chunk)
                if length is not None and size != int(length):
                    code = "incomplete_response"
                    retry = True
                else:
                    attempt["bytes_received"] = size
                    attempt["finished_at"] = utc_now()
                    metadata["attempts"].append(attempt)
                    metadata["finished_at"] = attempt["finished_at"]
                    return b"".join(chunks), status, metadata
        except MonitorError as exc:
            code = exc.code
        except (URLError, TimeoutError, ConnectionError, OSError, HTTPException):
            code = "network_error"
            retry = True
        finally:
            if response is not None:
                response.close()
        attempt["error"] = code
        attempt["finished_at"] = utc_now()
        metadata["attempts"].append(attempt)
        if not retry or number == MAX_ATTEMPTS:
            metadata["finished_at"] = utc_now()
            raise FetchFailure(code, metadata)
        sleep(min(2 ** (number - 1), 4))
    raise AssertionError("unreachable")
