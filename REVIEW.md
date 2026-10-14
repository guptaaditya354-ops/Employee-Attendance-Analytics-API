# REVIEW.md

Defects found in the starter `app/main.py`, with how each shows up and how it was fixed.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds shift start with `punch_in.replace(hour=..)` in the punch's own zone (UTC from Mongo / server-local), not IST. | Shift 09:30 IST, punch 09:28 IST (03:58Z) -> start becomes 09:30Z, result is hugely negative or ~5.5 h off. | Shift start is built in IST for the attendance date (`shift_dt`). |
| 2 | `compute_late_minutes` | `int(seconds/60)` is applied *before* `> 10`, so the strict "more than 10:00" rule is wrong: 09:40:01 gives 10 -> not late. No seconds truncation (R1). | Punch 09:40:01 must give 10, starter gives 0. | Compare raw seconds `> 600`, then floor to minutes; instants truncated to whole seconds at the boundary. |
| 3 | `compute_late_minutes` / `punch_in` | Overnight shifts ignored: date and shift start come from the punch's own calendar day (R1). | 22:00-06:00 shift, punch-in 02:00 -> wrong date, wrong lateness. | `attendance_date()` moves early-morning punches of an overnight shift to the previous day; late uses that day's start. |
| 4 | `compute_work_hours` | Python `round()` is not half-up (R4): it works on floats and ties go to the even digit. | 450 seconds = 0.125 h; `round(0.125, 2)` gives 0.12, but the rule needs 0.13. | Exact `Fraction` arithmetic with half-up rounding (`rhu`). |
| 5 | `compute_overtime` | No 30-minute minimum (R3); ignores overnight (end is next day); mixes naive Mongo datetime with an aware one -> `TypeError` (500). | Punch-out 18:31 returns 1 (should be 0); any overnight punch-out is wrong; subtracting naive/aware crashes. | `compute_overtime` uses an aware end (next day when overnight), floors minutes and applies the >= 30 threshold. Client is `tz_aware=True`. |
| 6 | `MongoClient(...)` | Default client returns naive datetimes. | Mixed naive/aware arithmetic crashes; instants misread as local time. | `tz_aware=True`; all instants are aware UTC. |
| 7 | `create_employee` | Check-then-insert race and no unique index: parallel creates produce duplicates (no 409). | 10 parallel POSTs with the same emp_code -> several 201s. | Unique index on `emp_code` created at startup; `DuplicateKeyError` -> 409. |
| 8 | `create_employee` | No validation at all (emp_code format, email, lengths, shift format, `joined_on` date, `shift_start == shift_end`). `created_at = datetime.now()` is naive local time and is returned as an ISO string, not epoch ms. | `joined_on: "yesterday"`, `shift_start: "25:99"` are accepted; response `created_at` is a string. | Pydantic constraints from the contract; 422 on violations; `created_at` stored as UTC BSON date and returned as epoch ms. |
| 9 | `list_employees` | `skip = page * page_size` skips the whole first page; `total` counts the entire collection, not the filter; no sort; `page=0` gives negative skip (500); `page_size` unbounded; `if department:` ignores `""`. | page=1 returns the 2nd page; total ignores `department`; page=0 -> 500. | `skip=(page-1)*size`, `count_documents(filter)`, sort by `emp_code`, `page>=1`, `1<=page_size<=100`, `is not None` check. |
| 10 | `punch_in` | Unknown employee -> `emp` is `None` -> `TypeError` -> 500 instead of 404. | POST with an unknown emp_code. | Explicit 404. |
| 11 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the server time zone and keeps fractional seconds; date is not the IST date (R1); `if body.punched_at` treats 0 as "missing"; no range check, so seconds-looking values are accepted. | Server in UTC: a 00:10 IST punch gets yesterday's date; `punched_at` in seconds silently yields 1970. | UTC-aware conversion, truncation to seconds, IST attendance date, strict `EpochMs` type (int, 1e11..4.1e12) -> 422. |
| 12 | `punch_in` | Check-then-insert race: two simultaneous punch-ins both pass the `find_one` and both insert. | Parallel identical requests -> two 201s / two records. | Unique index `(emp_code, date)`; exactly one insert wins, the rest get 409. |
| 13 | `punch_in` | `status` is a free string: `ABSENT`, `LEAVE`, or garbage are accepted. | `status: "LEAVE"` returns 201 with a punch time. | `Literal["PRESENT","WFH","ON_DUTY"]` -> 422. |
| 14 | `punch_in` / `list_attendance` responses | Leaks `_id` as `id`, returns raw datetimes instead of epoch ms, so the response violates the contract. | Response has `id` and ISO strings. | `rec_out()` builds the contract shape; `_id` never leaves the service. |
| 15 | `list_attendance` | Loads the whole collection and sorts/paginates in Python (100k docs per request); sort lacks the `emp_code` tiebreak; no index; `date_from > date_to`, bad dates, bad `status`, bad paging not rejected; `page=0` slices negatively. | Slow at 100k records, unstable order for equal dates, 200 for invalid input. | DB-side filter/sort/skip/limit with index `(date desc, emp_code)`; validation -> 422; total from `count_documents`. |
| 16 | `/health` | Always returns ok, even if MongoDB is down. | Stop MongoDB, call `/health`. | Pings MongoDB; 503 otherwise. |
| 17 | startup | No indexes are created. | `explain` shows COLLSCAN. | `ensure_indexes()` in lifespan (idempotent, retried while Mongo starts). |

## Looked at and judged fine
* `load_dotenv()` does not override existing environment variables, so real env vars win as required.
* Comparing `date` strings with `$gte/$lte` is correct because `YYYY-MM-DD` sorts lexicographically like dates.
* `doc.pop("_id")` after `insert_one` is needed (PyMongo mutates the dict) and correct; the bug was returning other non-contract values.
* The IST constant (`UTC+05:30`) is right; the problem was not using it.
