import pytest


def event(uid="series@example.invalid", extra="", start="20261005T170000Z", end="20261005T180000Z"):
    return (
        f"BEGIN:VEVENT\nUID:{uid}\nDTSTAMP:20261001T000000Z\n"
        f"DTSTART:{start}\nDTEND:{end}\nSUMMARY:Public working group\n"
        f"{extra}END:VEVENT\n"
    )


def calendar(*events, zones=""):
    return ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Synthetic//EN\n" + zones + "".join(events) + "END:VCALENDAR\n").replace("\n", "\r\n").encode()


@pytest.fixture
def sample():
    return calendar(event(extra="RRULE:FREQ=WEEKLY;BYDAY=MO,WE\nCREATED:20260101T000000Z\nLAST-MODIFIED:20261001T000000Z\nSEQUENCE:2\n"))


def response(payload, status=200, cache=None):
    def fetcher(previous):
        return payload, status, {
            "started_at": "2026-10-05T00:00:00+00:00",
            "finished_at": "2026-10-05T00:00:01+00:00",
            "conditional": bool(previous),
            "attempts": [{"number": 1, "http_status": status, "cache": cache or {}}],
        }
    return fetcher
