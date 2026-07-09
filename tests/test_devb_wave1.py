"""Dev B (Masnun) Wave-1 regression tests: B5,B6,B7,B8,B9,B10,B11,B12,B14,B15.

Black-box tests through TestClient against the spec in README.md.
Run: python -m pytest tests/test_devb_wave1.py -q
Requires pytest + httpx locally (NOT in requirements.txt — do not add them).
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------- helpers

def _register_login(org: str, username: str, password: str = "pw") -> str:
    client.post("/auth/register", json={"org_name": org, "username": username, "password": password})
    r = client.post("/auth/login", json={"org_name": org, "username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _org():
    """Fresh org with an admin and two members; returns tokens + org name."""
    org = f"org-{uuid.uuid4().hex[:10]}"
    admin = _register_login(org, "admin")
    m1 = _register_login(org, "m1")
    m2 = _register_login(org, "m2")
    return org, admin, m1, m2


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _room(admin_tok: str, rate: int = 1000) -> int:
    r = client.post("/rooms", headers=_h(admin_tok),
                    json={"name": f"r-{uuid.uuid4().hex[:6]}", "capacity": 4, "hourly_rate_cents": rate})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _slot(room_id: int, start: datetime, end: datetime) -> dict:
    return {"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}


def _now() -> datetime:
    return datetime.utcnow()


def _future(days: float) -> datetime:
    """A start_time `days` days ahead, snapped to a whole minute for readability."""
    return (_now() + timedelta(days=days)).replace(second=0, microsecond=0)


def _book(tok: str, room_id: int, start: datetime, hours: float = 1):
    return client.post("/bookings", headers=_h(tok), json=_slot(room_id, start, start + timedelta(hours=hours)))


# ---------------------------------------------------------------- B6: no grace window

def test_b6_no_grace_window_past_start_rejected():
    _, admin, m1, _ = _org()
    room = _room(admin)
    past = _now() - timedelta(seconds=60)  # inside the buggy 300s grace window
    r = _book(m1, room, past)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"


def test_b6_future_start_still_accepted():
    _, admin, m1, _ = _org()
    room = _room(admin)
    r = _book(m1, room, _future(30))
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- B7: duration bounds

@pytest.mark.parametrize("hours,expect", [(0, 400), (-2, 400), (1, 201), (8, 201), (9, 400)])
def test_b7_duration_bounds(hours, expect):
    _, admin, m1, _ = _org()
    room = _room(admin)
    r = _book(m1, room, _future(40), hours=hours)
    assert r.status_code == expect, f"{hours}h -> {r.status_code}: {r.text}"
    if r.status_code == 201:
        assert r.json()["price_cents"] == 1000 * hours
    else:
        assert r.json()["code"] == "INVALID_BOOKING_WINDOW"


# ---------------------------------------------------------------- B5: overlap strictness

def test_b5_back_to_back_allowed():
    _, admin, m1, _ = _org()
    room = _room(admin)
    s = _future(35)
    assert _book(m1, room, s).status_code == 201
    after = _book(m1, room, s + timedelta(hours=1))          # touches on the right
    before = _book(m1, room, s - timedelta(hours=1))         # touches on the left
    assert after.status_code == 201, after.text
    assert before.status_code == 201, before.text


def test_b5_true_overlap_still_rejected():
    _, admin, m1, _ = _org()
    room = _room(admin)
    s = _future(36)
    assert _book(m1, room, s, hours=2).status_code == 201
    r = _book(m1, room, s + timedelta(hours=1), hours=2)     # 1h true overlap
    assert r.status_code == 409 and r.json()["code"] == "ROOM_CONFLICT"
    r = _book(m1, room, s, hours=2)                          # identical interval
    assert r.status_code == 409


# ---------------------------------------------------------------- B9: detail start_time

def test_b9_detail_start_time_is_real_start():
    _, admin, m1, _ = _org()
    room = _room(admin)
    s = _future(45)
    created = _book(m1, room, s).json()
    detail = client.get(f"/bookings/{created['id']}", headers=_h(m1)).json()
    assert detail["start_time"] == created["start_time"]     # create response is known-correct
    assert detail["start_time"].startswith(s.isoformat())


# ---------------------------------------------------------------- B10: member visibility

def test_b10_member_cannot_read_others_booking():
    _, admin, m1, m2 = _org()
    room = _room(admin)
    bid = _book(m1, room, _future(50)).json()["id"]
    r = client.get(f"/bookings/{bid}", headers=_h(m2))
    assert r.status_code == 404 and r.json()["code"] == "BOOKING_NOT_FOUND"
    assert client.get(f"/bookings/{bid}", headers=_h(m1)).status_code == 200   # owner OK
    assert client.get(f"/bookings/{bid}", headers=_h(admin)).status_code == 200  # same-org admin OK


# ---------------------------------------------------------------- B8: pagination

def test_b8_pagination_asc_tiled_no_gaps():
    _, admin, m1, _ = _org()
    room_a, room_b = _room(admin), _room(admin)
    starts = [_future(60) + timedelta(hours=2 * i) for i in range(4)]
    for s in starts:
        assert _book(m1, room_a, s).status_code == 201
    # tie on start_time (same slot, different room) -> breaks by ascending id
    assert _book(m1, room_b, starts[0]).status_code == 201

    seen, total = [], None
    for page in (1, 2, 3):
        body = client.get(f"/bookings?page={page}&limit=2", headers=_h(m1)).json()
        assert len(body["items"]) <= 2                       # limit honored (was hardcoded 10)
        assert body["limit"] == 2
        seen += [i["id"] for i in body["items"]]
        total = body["total"]
    assert total == 5
    assert len(seen) == len(set(seen)) == 5                  # no repeats, no skips
    full = client.get("/bookings?page=1&limit=100", headers=_h(m1)).json()["items"]
    keys = [(i["start_time"], i["id"]) for i in full]
    assert keys == sorted(keys)                              # ascending by (start_time, id)
    beyond = client.get("/bookings?page=9&limit=2", headers=_h(m1)).json()
    assert beyond["items"] == [] and beyond["total"] == 5


# ---------------------------------------------------------------- B11: refund tiers

def _cancel_percent(notice: timedelta) -> dict:
    _, admin, m1, _ = _org()
    room = _room(admin)
    start = _now() + notice
    r = _book(m1, room, start)
    assert r.status_code == 201, r.text
    return client.post(f"/bookings/{r.json()['id']}/cancel", headers=_h(m1)).json()


def test_b11_tier_100_at_and_above_48h():
    assert _cancel_percent(timedelta(hours=72))["refund_percent"] == 100
    # "exactly 48h" boundary: creation->cancel latency means we must aim slightly past it
    assert _cancel_percent(timedelta(hours=48, minutes=1))["refund_percent"] == 100


def test_b11_tier_50_between_24_and_48h():
    assert _cancel_percent(timedelta(hours=47, minutes=30))["refund_percent"] == 50
    assert _cancel_percent(timedelta(hours=24, minutes=1))["refund_percent"] == 50


def test_b11_tier_0_below_24h():
    assert _cancel_percent(timedelta(hours=23, minutes=30))["refund_percent"] == 0
    assert _cancel_percent(timedelta(hours=12))["refund_percent"] == 0


# ---------------------------------------------------------------- B12: refund rounding + ledger consistency

def _cancel_with_price(rate: int, notice: timedelta):
    _, admin, m1, _ = _org()
    room = _room(admin, rate=rate)
    start = _now() + notice
    created = _book(m1, room, start).json()
    resp = client.post(f"/bookings/{created['id']}/cancel", headers=_h(m1)).json()
    detail = client.get(f"/bookings/{created['id']}", headers=_h(m1)).json()
    return resp, detail


def test_b12_half_cent_rounds_up_and_matches_ledger():
    for rate, expected in ((1001, 501), (1003, 502), (1000, 500)):
        resp, detail = _cancel_with_price(rate, timedelta(hours=36))   # 50% tier
        assert resp["refund_amount_cents"] == expected, f"price {rate} @50%"
        assert len(detail["refunds"]) == 1                             # exactly one RefundLog
        assert detail["refunds"][0]["amount_cents"] == resp["refund_amount_cents"]


def test_b12_full_and_zero_refunds_exact():
    resp, detail = _cancel_with_price(1001, timedelta(hours=72))       # 100%
    assert resp["refund_amount_cents"] == 1001
    assert detail["refunds"][0]["amount_cents"] == 1001
    resp, detail = _cancel_with_price(1001, timedelta(hours=2))        # 0%
    assert resp["refund_amount_cents"] == 0
    assert detail["refunds"][0]["amount_cents"] == 0


# ---------------------------------------------------------------- B14: report fresh after create

def test_b14_usage_report_fresh_after_create():
    _, admin, m1, _ = _org()
    room = _room(admin)
    s = _future(90)
    frm, to = (s - timedelta(days=5)).date().isoformat(), (s + timedelta(days=5)).date().isoformat()
    q = f"/admin/usage-report?from={frm}&to={to}"
    primed = client.get(q, headers=_h(admin)).json()                   # PRIME the cache
    assert next(r for r in primed["rooms"] if r["room_id"] == room)["confirmed_bookings"] == 0
    assert _book(m1, room, s).status_code == 201                       # mutate
    fresh = client.get(q, headers=_h(admin)).json()                    # must reflect immediately
    row = next(r for r in fresh["rooms"] if r["room_id"] == room)
    assert row["confirmed_bookings"] == 1 and row["revenue_cents"] == 1000


# ---------------------------------------------------------------- B15: availability fresh after cancel

def test_b15_availability_fresh_after_cancel():
    _, admin, m1, _ = _org()
    room = _room(admin)
    s = _future(100)
    bid = _book(m1, room, s).json()["id"]
    date = s.date().isoformat()
    primed = client.get(f"/rooms/{room}/availability?date={date}", headers=_h(m1)).json()  # PRIME
    assert len(primed["busy"]) == 1
    assert client.post(f"/bookings/{bid}/cancel", headers=_h(m1)).status_code == 200
    fresh = client.get(f"/rooms/{room}/availability?date={date}", headers=_h(m1)).json()
    assert fresh["busy"] == []                                         # must reflect immediately
