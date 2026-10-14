# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, duplicate safety, join target), `(department, emp_code)` for department filters and sorted lists, `joined_on` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, per-employee ranges), `(date desc, emp_code)` for the default list order, `(date, late_minutes, emp_code)` so the leaderboard reads only the index. Rejected: an index on `status`, because it has five values and the date index already narrows the search.

2. **Punch-in race.** Both requests compute the same attendance date and call `insert_one`. The unique `(emp_code, date)` index lets one insert succeed; the other gets a duplicate-key error, which I return as 409. I never check first and insert later.

3. **Ties.** I rank with `$rank`, so ties share a rank and the next one is skipped (1, 2, 2, 4). `limit` is applied after ranking (`rank <= limit`), so a tie at the cutoff returns all tied people, even if that is more than `limit` rows.

4. **Headcount.** The pipeline starts from `employees`, keeps people who joined by month end, then looks up their logs. Someone with zero logs still counts in `headcount`.

5. **100x data.** I would keep a pre-computed monthly summary per employee, updated on punch and correction, instead of aggregating raw records on every request.
