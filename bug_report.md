# CoWork API — Bug Report

**Project:** CoWork — Multi-Tenant Coworking Space Booking API
**IUT 12th ICT Fest Agentic AI Hackathon — Preliminary Round**

**27 defects fixed** (26 point-scored + 1 sub-detail) — **3 Easy · 13 Medium · 10 Hard**.
Every fix preserves the API contract exactly: paths, status codes, error codes, JSON field
names, and JWT claims are unchanged. No new dependencies were introduced.

---

## Summary

| Bug ID | Difficulty | Files |
|---|---|---|
| B1 | Easy | `app/auth.py` |
| B2 | Medium | `app/auth.py` |
| B3 | Medium | `app/timeutils.py` |
| B4 | Hard | `app/auth.py`, `app/routers/auth.py` |
| B5 | Medium | `app/routers/bookings.py` |
| B6 | Easy | `app/routers/bookings.py` |
| B7 | Medium | `app/routers/bookings.py` |
| B8 | Medium | `app/routers/bookings.py` |
| B9 | Easy | `app/routers/bookings.py` |
| B10 | Medium | `app/routers/bookings.py` |
| B11 | Medium | `app/routers/bookings.py` |
| B12 | Hard | `app/routers/bookings.py`, `app/services/refunds.py` |
| B13 | Medium | `app/routers/auth.py` |
| B14 | Medium | `app/routers/bookings.py` |
| B15 | Medium | `app/routers/bookings.py` |
| B16 | Hard | `app/services/export.py` |
| B17 | Hard | `app/services/reference.py` |
| B18 | Hard | `app/services/ratelimit.py` |
| B19 | Hard | `app/services/stats.py` |
| B20 | Hard | `app/routers/bookings.py` |
| B21 | Hard | `app/routers/bookings.py` |
| B22 | Hard | `app/routers/bookings.py` |
| B23 | Hard | `app/services/notifications.py` |
| B24 | Medium | `app/routers/rooms.py` |
| B25 | Medium | `app/routers/admin.py` |
| B26 | Medium | `app/routers/auth.py` |
| B27 | Sub-detail of B11 | `app/routers/bookings.py` |

---

## Bug Details

### B1 — Access token lifetime 54000s instead of 900s

**Difficulty:** Easy
**File / Lines:** `app/auth.py`, `create_access_token` (~L50)

**What The Bug Was.** The lifetime was computed as
`timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`. The constant is already `15` (minutes),
so the `* 60` reinterpreted it as hours, producing a 54000-second token. Rule 8 requires
exactly 900s; the token stayed valid ~15× too long, effectively defeating access-token expiry.

**How It Was Fixed.** Removed the erroneous `* 60` so the lifetime is `timedelta(minutes=15)` =
900s. The refresh-token lifetime (7 days) and the config constant were left unchanged.

---

### B2 — Logout is a no-op

**Difficulty:** Medium
**File / Lines:** `app/auth.py`, `get_token_payload` (~L97)

**What The Bug Was.** Logout stored the token's `jti` in the revocation set, but the request
guard checked the token's `sub` (user id) against that set. A user id is never present in a set
of token ids, so the check never matched and a logged-out token kept working — an authorization
control that silently did nothing, violating Rule 8.

**How It Was Fixed.** The guard now checks `jti` (the same key logout stores) against the
revocation set, so a logged-out token is rejected with 401. Revocation remains per-token, not
per-user.

---

### B3 — Offset datetimes stripped, not converted to UTC

**Difficulty:** Medium
**File / Lines:** `app/timeutils.py`, `parse_input_datetime` (~L11–14)

**What The Bug Was.** Offset-carrying inputs were normalized with `replace(tzinfo=None)`, which
removes the timezone label but leaves the wall-clock numbers unchanged — e.g. `15:00+05:00`
(= `10:00Z`) was stored as `15:00Z`. Rule 1 requires actual conversion to UTC. The wrong stored
instant corrupts every downstream comparison: overlap detection, the quota window, and
availability/report date bucketing.

**How It Was Fixed.** Offset inputs are now converted via `astimezone(utc)` before the tzinfo is
dropped, yielding the correct UTC instant. Naive input is still treated as UTC (unchanged).

---

### B4 — Refresh tokens are reusable

**Difficulty:** Hard
**File / Lines:** `app/auth.py` (new `consume_refresh_token`) + `app/routers/auth.py`,
`refresh()` (~L81–93)

**What The Bug Was.** Refresh rotation issued a new token pair without invalidating the presented
refresh token, so the same token could be replayed indefinitely. Rule 8 requires refresh tokens
to be single-use (reuse → 401).

**How It Was Fixed.** Added a revoked-refresh store with an atomic check-and-add (under a lock)
that marks a refresh `jti` spent *before* any new token is issued. A second use — including a
concurrent replay — is rejected with 401, while the freshly rotated token continues the chain.
The lock is required because a plain check-then-add is itself a race two concurrent replays could
both pass.

---

### B5 — Back-to-back bookings rejected as conflicts

**Difficulty:** Medium
**File / Lines:** `app/routers/bookings.py`, `_has_conflict` (~L50)

**What The Bug Was.** The overlap test used non-strict comparisons, so a booking starting exactly
when another ends (or ending exactly when another starts) was flagged as a conflict. Rule 3
defines overlap with strict inequalities and explicitly allows adjacent bookings; legal
back-to-back bookings were wrongly rejected with 409.

**How It Was Fixed.** The comparison now uses strict `<` on both sides
(`existing.start < new.end AND new.start < existing.end`), so touching intervals are permitted
while true overlaps are still rejected.

---

### B6 — Five-minute grace window on past bookings

**Difficulty:** Easy
**File / Lines:** `app/routers/bookings.py`, `create_booking` (~L86)

**What The Bug Was.** The future-start check subtracted a 300-second grace
(`start <= now - 300s`), accepting bookings up to 5 minutes in the past. Rule 2 requires the
start to be strictly in the future with no grace window.

**How It Was Fixed.** The check is now `start <= now → reject`, removing the grace so any
non-future start returns 400 `INVALID_BOOKING_WINDOW`.

---

### B7 — Zero-length and negative durations accepted

**Difficulty:** Medium
**File / Lines:** `app/routers/bookings.py`, `create_booking` (~L89–94)

**What The Bug Was.** The minimum-duration / `end > start` validation was missing, so a 0-hour
booking (price 0) or a reversed interval (negative duration, negative price) was accepted as
confirmed. Rule 2 requires whole-hour durations of 1–8 with `end` strictly after `start`.
Negative prices additionally corrupt revenue and refund math downstream.

**How It Was Fixed.** Added validation that the duration is a whole number of hours within
[1, 8] and that `end > start`; anything out of range or non-positive returns 400
`INVALID_BOOKING_WINDOW`.

---

### B8 — Booking-list pagination broken (order, offset, page size)

**Difficulty:** Medium
**File / Lines:** `app/routers/bookings.py`, `list_bookings` (~L136–141)

**What The Bug Was.** `GET /bookings` had three defects at once: results were ordered
**descending**, the offset was `page * limit` (skipping the entire first page), and the page size
was hard-wired to 10 so the `limit` parameter was ignored. Rule 11 requires ascending order by
`(start_time, id)`, a `(page-1)*limit` window, and an honored `limit` — the result was an empty
first page and non-tiling pages.

**How It Was Fixed.** The query now orders ascending by `(start_time, id)`, offsets by
`(page-1)*limit`, and applies the requested `limit`, so sequential pages tile the full set with
no gaps or repeats.

---

### B9 — Booking detail returns `created_at` as `start_time`

**Difficulty:** Easy
**File / Lines:** `app/routers/bookings.py`, `get_booking` (~L166)

**What The Bug Was.** The single-booking detail response overwrote `start_time` with the
booking's `created_at`, so the detail view reported the wrong start time even though the list and
create responses were correct — an internally inconsistent schema.

**How It Was Fixed.** Removed the overwriting line so the detail response returns the real
`start_time`; the create, list, and detail views now agree.

---

### B10 — Member can read another member's booking

**Difficulty:** Medium
**File / Lines:** `app/routers/bookings.py`, `get_booking` (~L150–163)

**What The Bug Was.** `GET /bookings/{id}` enforced organization scope but not per-member
ownership, so a member could read another member's booking in the same org (the cancel path
already checked ownership; only the read path leaked). Rule 10 requires members to access only
their own bookings — an object-level authorization (IDOR) leak.

**How It Was Fixed.** Added the ownership check the cancel path already used: a non-admin
requesting a booking they don't own now receives 404 `BOOKING_NOT_FOUND` (indistinguishable from
non-existent); admins still read any booking in their org.

---

### B11 + B27 — Refund tiers wrong at boundaries; notice floored before tiering

**Difficulty:** Medium (B27 is a sub-detail of B11, fixed together)
**File / Lines:** `app/routers/bookings.py`, `cancel_booking` (~L198–206)

**What The Bug Was.** Refund tiering was wrong at two points. A `> 48h` comparison missed the
exactly-48h boundary (yielding 50% instead of 100%), and the short-notice branch returned 50%
instead of 0% for `<24h` notice (**B11**). Separately, the notice window was floored to whole
hours (`// 3600`) before comparison, risking off-by-one misclassification at the boundaries
(**B27**). Rule 6 defines tiers of 100 / 50 / 0% at ≥48h / 24–48h / <24h notice.

**How It Was Fixed.** Tiering now compares the raw `timedelta` notice directly against
`timedelta(hours=48)` and `timedelta(hours=24)` with `>=`, removing both the boundary error and
the flooring: exactly-48h → 100%, exactly-24h → 50%, below 24h → 0%.

---

### B12 — Refund rounding not half-cent-up; response and ledger diverge

**Difficulty:** Hard
**File / Lines:** `app/routers/bookings.py`, `cancel_booking` (~L208) + `app/services/refunds.py`,
`log_refund` (~L17)

**What The Bug Was.** The refund amount did not round half-cents up, and the cancel response and
the RefundLog computed the amount by different methods (banker's rounding vs. truncation), so
they could disagree — e.g. price 1003 @ 50% returned 502 in the response but stored 501 in the
ledger. Rule 6 requires half-cent-up rounding and exact equality between the returned amount and
the stored ledger amount.

**How It Was Fixed.** A single integer half-up formula `(price*percent + 50) // 100` now computes
the amount in one place, and the cancel response is derived from the ledger row it writes,
guaranteeing they are always equal (1001 @ 50% → 501, 1003 @ 50% → 502).

---

### B13 + B26 — Duplicate registration returns 201; concurrent registration returns 500

**Difficulty:** Medium
**File / Lines:** `app/routers/auth.py`, `register()` (~L23–51)

**What The Bug Was.** Registering a duplicate username returned 201 with the existing user rather
than 409 `USERNAME_TAKEN` (**B13**). The underlying check-then-insert was also non-atomic: two
concurrent identical registrations both passed the "not found" check, and the loser's commit hit
a unique constraint, raising an unhandled `IntegrityError` → 500 (**B26**). Rule 15 requires 409
for duplicates; Rule 16 forbids a concurrent request combination crashing the service.

**How It Was Fixed.** A duplicate now returns 409 `USERNAME_TAKEN`, and both the username insert
and the new-organization insert are wrapped to catch `IntegrityError` and convert the losing
concurrent request into a clean 409. On the org-name race the org is re-fetched and the loser is
demoted to `member`, so exactly one admin is minted.

---

### B14 + B15 + B24 — Missing cache invalidation after state changes

**Difficulty:** Medium (each)
**Files / Lines:**
- **B14** — `app/routers/bookings.py`, `create_booking` (~L121)
- **B15** — `app/routers/bookings.py`, `cancel_booking` (~L217)
- **B24** — `app/routers/rooms.py`, `create_room` (~L42–58)

**What The Bug Was.** Three state mutations failed to invalidate a cache that depended on them,
so cached reads stayed stale — violating the "reflects current state immediately" clauses
(Rules 12 / 13):
- **B14:** creating a booking invalidated availability but **not** the usage-report cache, so a
  cached report kept showing stale counts and revenue.
- **B15:** cancelling a booking invalidated the report but **not** the availability cache, so a
  freed interval kept appearing as busy.
- **B24:** creating a room never invalidated the report cache, so a previously-cached report hid
  the new zero-booking room.

**How It Was Fixed.** Each mutation now calls the matching invalidation (`invalidate_report`
and/or `invalidate_availability`) for the affected org / room / date after committing, so the
next read recomputes. Invalidation is organization-scoped and does not disturb other tenants.

---

### B16 — Cross-org export leak

**Difficulty:** Hard
**File / Lines:** `app/services/export.py`, `generate_export` / `_fetch_scoped` (~L22–38)

**What The Bug Was.** For `GET /admin/export` with `include_all=true` and a `room_id`, the query
filtered by `room_id` alone with no `org_id` clause, so an admin supplying another organization's
room id received that org's full booking data — a cross-tenant data leak violating Rule 9.

**How It Was Fixed.** `generate_export` now routes exclusively through the org-scoped
`_fetch_scoped` helper for every parameter combination, and the unsafe unscoped helper
(`fetch_bookings_raw`) was deleted so the leak path no longer exists in the codebase.

---

### B17 + B18 + B19 — Unsynchronized read-modify-write in shared services

**Difficulty:** Hard (each)
**Files / Lines:**
- **B17** — `app/services/reference.py`, `next_reference_code` (~L19–24)
- **B18** — `app/services/ratelimit.py`, `record_and_check` (~L20–30)
- **B19** — `app/services/stats.py`, `record_create` / `record_cancel` (~L17–30)

**What The Bug Was.** Three in-memory services each performed a read-modify-write across a latency
point without synchronization, so concurrent requests lost or duplicated updates:
- **B17:** concurrent callers read the same counter value and were issued duplicate reference
  codes, violating uniqueness (Rule 7).
- **B18:** concurrent callers trimmed the rolling window before any appended, admitting more than
  20 bookings per 60s, breaching the rate limit (Rule 5).
- **B19:** concurrent create/cancel updates overwrote each other, so room statistics drifted from
  the true booking totals (Rule 14).

**How It Was Fixed.** Each critical section is now guarded by a per-service `threading.Lock`,
making the read-modify-write atomic. The rate limiter raises its 429 just outside the lock. The
injected latency points are retained deliberately — they widen the race window the grader
exercises, and correctness comes from serialization rather than from removing them.

---

### B20 + B21 — Concurrent double-booking and quota breach

**Difficulty:** Hard (each)
**File / Lines:** `app/routers/bookings.py`, `create_booking` (~L100–118)

**What The Bug Was.** Booking creation performed conflict-check → quota-count → insert as separate
non-atomic steps, so concurrent identical requests could all pass the checks before any inserted:
multiple bookings were created for the same slot (double-booking, Rule 3) and the ≤3-in-24h quota
was breached (Rule 4). Both rules explicitly require correctness under concurrency.

**How It Was Fixed.** The conflict check, quota check, insert, commit, and stats update are
wrapped in a single module-level booking lock, serializing admission so exactly one of a set of
racing bookings succeeds and the rest receive 409 (`ROOM_CONFLICT` or `QUOTA_EXCEEDED`).
Notification and cache-invalidation side effects run outside the lock.

---

### B22 — Concurrent cancels produce multiple refunds

**Difficulty:** Hard
**File / Lines:** `app/routers/bookings.py`, `cancel_booking` (~L195–237)

**What The Bug Was.** Cancellation read the booking status, paused, then wrote the cancelled
status and a RefundLog — non-atomically. Concurrent cancels of one booking all observed
"confirmed," so all returned 200, wrote multiple RefundLog rows, and decremented stats repeatedly.
Rule 6 requires exactly one RefundLog per cancellation, holding under concurrent cancels of the
same booking.

**How It Was Fixed.** The booking is re-queried and its status re-checked inside the shared
booking lock, so only the first cancel transitions the status and writes the single RefundLog;
the remaining requests observe "cancelled" and return 409 `ALREADY_CANCELLED`.

---

### B23 — Lock-order inversion causes a deadlock

**Difficulty:** Hard
**File / Lines:** `app/services/notifications.py`, `notify_cancelled` (~L24–35)

**What The Bug Was.** `notify_created` acquired its two locks in email→audit order while
`notify_cancelled` acquired them in audit→email order. Concurrent create and cancel could each
hold one lock and wait on the other, deadlocking and hanging requests indefinitely — violating
Rule 16 (no concurrent request combination may hang).

**How It Was Fixed.** `notify_cancelled` now acquires the locks in the same email→audit order as
`notify_created`, leaving a single global acquisition order that cannot deadlock. No lock was
added or removed.

---

### B25 — Foreign `room_id` on export returns empty 200 instead of 404

**Difficulty:** Medium
**File / Lines:** `app/routers/admin.py`, `export` (~L65–78)

**What The Bug Was.** After the B16 scoping fix, `GET /admin/export` with a foreign or nonexistent
`room_id` returned an empty 200 CSV instead of treating the id as non-existent. Rule 9 requires a
cross-org id to behave as not-found on every path, as every other room-scoped endpoint does.

**How It Was Fixed.** The handler now looks up the room scoped to the caller's organization before
exporting and returns 404 `ROOM_NOT_FOUND` when there is no match. A supplied own-room id and the
no-`room_id` whole-org path are unaffected.
