"""Concurrency probes for the CoWork API (B4, B17-B23, B26).

Standard library only -- no extra dependencies. Runs against a LIVE server:

    uvicorn app.main:app --port 8000     # terminal 1
    python tests/concurrency_probe.py    # terminal 2

Each probe prints PASS/FAIL and the observed behavior. Before any fixes, most
probes FAIL -- that is the baseline. Run 3x after fixing: races are
probabilistic and one clean run proves nothing.

Every probe provisions its OWN org/user/room. This is deliberate: POST /bookings
is rate limited to 20 per rolling 60s per user, and the booking quota is per
user, so a shared identity would poison later probes.

Optional: BASE_URL env var (default http://127.0.0.1:8000).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
STAMP = int(time.time() * 1000)

_results: list[tuple[str, bool, str]] = []


def call(method, path, body=None, token=None, timeout=10):
    """Return (status_code, parsed_body_or_text). Status 0 means timeout/hang."""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:  # timeout / connection reset -> treat as a hang
        return 0, str(e)


def parallel(fn, n, workers=None):
    with ThreadPoolExecutor(max_workers=workers or n) as pool:
        return list(pool.map(lambda i: fn(i), range(n)))


def record(name, ok, detail):
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


def fresh_ctx(tag, rate=1000):
    """Provision an isolated org + admin + room. Returns (token, room_id).

    Isolation matters: rate limit and booking quota are both per-user.
    """
    org = f"probe-{tag}-{STAMP}"
    call("POST", "/auth/register", {"org_name": org, "username": "u", "password": "pw"})
    st, t = call("POST", "/auth/login", {"org_name": org, "username": "u", "password": "pw"})
    if st != 200:
        sys.exit(f"[{tag}] login failed: {st} {t}")
    token = t["access_token"]

    st, room = call("POST", "/rooms",
                    {"name": f"room-{tag}", "capacity": 4, "hourly_rate_cents": rate},
                    token=token)
    if st != 201:
        sys.exit(f"[{tag}] room creation failed: {st} {room}")
    return token, room["id"]


def slot(start_h, dur_h=1):
    s = (datetime.utcnow() + timedelta(hours=start_h)).replace(minute=0, second=0, microsecond=0)
    return s.isoformat(), (s + timedelta(hours=dur_h)).isoformat()


def book(token, room_id, start_h, dur_h=1):
    s, e = slot(start_h, dur_h)
    return call("POST", "/bookings",
                {"room_id": room_id, "start_time": s, "end_time": e}, token=token)


# --------------------------------------------------------------------------
# B20 -- concurrent double-booking must yield exactly one 201
# --------------------------------------------------------------------------
def probe_double_booking():
    token, room = fresh_ctx("dbl")
    s, e = slot(100)
    payload = {"room_id": room, "start_time": s, "end_time": e}

    results = parallel(lambda _: call("POST", "/bookings", payload, token=token), 8)
    codes = [c for c, _ in results]
    created, conflicts = codes.count(201), codes.count(409)
    record("B20 double-booking (8 concurrent identical slots)",
           created == 1 and conflicts == 7,
           f"201s={created} (want 1), 409s={conflicts} (want 7), all={sorted(codes)}")


# --------------------------------------------------------------------------
# B17 -- reference codes unique under concurrent creation
# --------------------------------------------------------------------------
def probe_reference_uniqueness():
    token, room = fresh_ctx("ref")
    # non-overlapping, far-future slots: every request should succeed on its merits
    results = parallel(lambda i: book(token, room, 200 + i * 2), 12)

    codes = [b["reference_code"] for st, b in results if st == 201 and isinstance(b, dict)]
    uniq = set(codes)
    dupes = [c for c in uniq if codes.count(c) > 1]
    record("B17 reference-code uniqueness (12 concurrent creates)",
           len(codes) > 0 and len(codes) == len(uniq),
           f"issued={len(codes)}, distinct={len(uniq)}" + (f", dupes={dupes}" if dupes else ""))


# --------------------------------------------------------------------------
# B21 -- quota: at most 3 confirmed with start in (now, now+24h]
# --------------------------------------------------------------------------
def probe_quota():
    token, room = fresh_ctx("quota")
    # 6 concurrent, all inside the 24h window, non-overlapping
    results = parallel(lambda i: book(token, room, 2 + i * 3), 6)

    codes = [c for c, _ in results]
    created = codes.count(201)
    quota = sum(1 for c, b in results
                if c == 409 and isinstance(b, dict) and b.get("code") == "QUOTA_EXCEEDED")
    record("B21 booking quota (6 concurrent inside 24h window)",
           created <= 3,
           f"201s={created} (want <=3), QUOTA_EXCEEDED={quota}, all={sorted(codes)}")


# --------------------------------------------------------------------------
# B19 -- stats always equal the values derivable from the bookings
# --------------------------------------------------------------------------
def probe_stats_consistency():
    token, room = fresh_ctx("stats", rate=1000)
    results = parallel(lambda i: book(token, room, 500 + i * 2), 10)

    confirmed = [b for st, b in results if st == 201 and isinstance(b, dict)]
    want_count = len(confirmed)
    want_revenue = sum(b["price_cents"] for b in confirmed)

    _, s = call("GET", f"/rooms/{room}/stats", token=token)
    got_count = s.get("total_confirmed_bookings") if isinstance(s, dict) else None
    got_revenue = s.get("total_revenue_cents") if isinstance(s, dict) else None

    record("B19 stats consistency (10 concurrent creates)",
           got_count == want_count and got_revenue == want_revenue,
           f"count={got_count} (want {want_count}), revenue={got_revenue} (want {want_revenue})")


# --------------------------------------------------------------------------
# B18 -- rate limit: 20 per rolling 60s per user, concurrent-safe
# --------------------------------------------------------------------------
def probe_rate_limit():
    token, room = fresh_ctx("rate", rate=100)
    s, e = slot(300)
    payload = {"room_id": room, "start_time": s, "end_time": e}

    # 30 concurrent on one slot: at most 20 admitted (201 or 409), rest 429.
    # generous timeout -- the fixed ratelimit serializes on a 0.1s sleep.
    results = parallel(lambda _: call("POST", "/bookings", payload, token=token, timeout=30), 30)
    codes = [c for c, _ in results]
    admitted = sum(1 for c in codes if c in (201, 400, 409))
    limited = codes.count(429)
    record("B18 rate limit (30 concurrent POST /bookings, one user)",
           admitted <= 20 and limited >= 10,
           f"admitted={admitted} (want <=20), 429s={limited} (want >=10)")


# --------------------------------------------------------------------------
# B22 -- concurrent cancels: exactly one 200 and exactly one RefundLog
# --------------------------------------------------------------------------
def probe_concurrent_cancel():
    token, room = fresh_ctx("cancel")
    st, booking = book(token, room, 400)
    if st != 201:
        record("B22 concurrent cancel", False, f"setup failed: {st} {booking}")
        return
    bid = booking["id"]

    results = parallel(lambda _: call("POST", f"/bookings/{bid}/cancel", token=token), 5)
    codes = [c for c, _ in results]
    ok = codes.count(200)
    already = sum(1 for c, b in results
                  if c == 409 and isinstance(b, dict) and b.get("code") == "ALREADY_CANCELLED")

    _, detail = call("GET", f"/bookings/{bid}", token=token)
    refunds = len(detail.get("refunds", [])) if isinstance(detail, dict) else -1

    record("B22 concurrent cancel (5 concurrent, same booking)",
           ok == 1 and already == 4 and refunds == 1,
           f"200s={ok} (want 1), ALREADY_CANCELLED={already} (want 4), RefundLogs={refunds} (want 1)")


# --------------------------------------------------------------------------
# B23 -- liveness: mixed concurrent create + cancel must never hang
# --------------------------------------------------------------------------
def probe_deadlock():
    token, room = fresh_ctx("dead")

    ids = []
    for i in range(6):
        st, b = book(token, room, 600 + i * 2)
        if st == 201:
            ids.append(b["id"])

    def do_cancel(bid):
        t0 = time.time()
        c, _ = call("POST", f"/bookings/{bid}/cancel", token=token, timeout=8)
        return ("cancel", c, time.time() - t0)

    def do_create(i):
        t0 = time.time()
        c, _ = book(token, room, 700 + i * 2)
        return ("create", c, time.time() - t0)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(do_cancel, b) for b in ids]
        futs += [pool.submit(do_create, i) for i in range(4)]
        out = [f.result() for f in futs]

    # status 0 or a duration at the client timeout means the request hung
    hung = [o for o in out if o[1] == 0 or o[2] >= 7.5]
    hc, _ = call("GET", "/health", timeout=3)
    slowest = max(o[2] for o in out) if out else 0.0

    record("B23 liveness under mixed create+cancel",
           len(hung) == 0 and hc == 200,
           f"hung={len(hung)} (want 0), /health={hc}, slowest={slowest:.2f}s")


# --------------------------------------------------------------------------
# B26 -- concurrent registration must never 500
# --------------------------------------------------------------------------
def probe_register_race():
    org = f"race-{STAMP}"
    body = {"org_name": org, "username": "racer", "password": "pw"}
    results = parallel(lambda _: call("POST", "/auth/register", body), 5)
    codes = [c for c, _ in results]
    created = codes.count(201)
    taken = sum(1 for c, b in results
                if c == 409 and isinstance(b, dict) and b.get("code") == "USERNAME_TAKEN")
    errors = sum(1 for c in codes if c >= 500)
    record("B26 concurrent register (5 identical)",
           created == 1 and taken == 4 and errors == 0,
           f"201s={created} (want 1), USERNAME_TAKEN={taken} (want 4), 5xx={errors} (want 0)")


def probe_register_org_race():
    org = f"orgrace-{STAMP}"
    results = parallel(
        lambda i: call("POST", "/auth/register",
                       {"org_name": org, "username": f"u{i}", "password": "pw"}), 5)
    codes = [c for c, _ in results]
    roles = [b.get("role") for c, b in results if c == 201 and isinstance(b, dict)]
    errors = sum(1 for c in codes if c >= 500)
    record("B26 concurrent new-org register (5 users, same brand-new org)",
           errors == 0 and roles.count("admin") == 1,
           f"5xx={errors} (want 0), admins={roles.count('admin')} (want 1), roles={roles}")


# --------------------------------------------------------------------------
# B4 -- concurrent refresh replay must yield exactly one 200
# --------------------------------------------------------------------------
def probe_refresh_replay():
    org = f"refresh-{STAMP}"
    call("POST", "/auth/register", {"org_name": org, "username": "r", "password": "pw"})
    _, t = call("POST", "/auth/login", {"org_name": org, "username": "r", "password": "pw"})
    rt = t["refresh_token"]

    results = parallel(lambda _: call("POST", "/auth/refresh", {"refresh_token": rt}), 5)
    codes = [c for c, _ in results]
    record("B4 concurrent refresh replay (5x same token)",
           codes.count(200) == 1,
           f"200s={codes.count(200)} (want 1), 401s={codes.count(401)} (want 4)")


def main():
    print(f"\nCoWork concurrency probe -> {BASE}\n" + "=" * 68)
    st, _ = call("GET", "/health", timeout=3)
    if st != 200:
        sys.exit(f"server not reachable at {BASE} (/health -> {st})")

    print("\n-- Booking races (B17, B20, B21) --")
    probe_double_booking()
    probe_reference_uniqueness()
    probe_quota()

    print("\n-- Service races (B18, B19) --")
    probe_stats_consistency()
    probe_rate_limit()

    print("\n-- Cancel race (B22) --")
    probe_concurrent_cancel()

    print("\n-- Liveness (B23) --")
    probe_deadlock()

    print("\n-- Auth races (B4, B26) --")
    probe_register_race()
    probe_register_org_race()
    probe_refresh_replay()

    print("\n" + "=" * 68)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"{passed}/{len(_results)} probes passed")
    for name, ok, _ in _results:
        if not ok:
            print(f"  still failing: {name}")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()
