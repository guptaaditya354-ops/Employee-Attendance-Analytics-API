# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, manual corrections with an audit trail, and analytics built on aggregation pipelines. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # Mac/Linux: cp .env.example .env  -> then set MONGO_URI / MONGO_DB
uvicorn app.main:app --port 8000
```
MongoDB 6.0 or newer is required. Indexes are created automatically at startup. Real environment variables take priority over `.env`.

## What is implemented
All endpoints in the contract: health, employees (create/list), punch-in, punch-out, attendance list, regularization (PATCH with history), the four analytics endpoints and `/admin/explain/{endpoint}`.

## Testing
* `pytest tests/test_core.py` (needs `pytest mongomock httpx`): rules R1-R5, validation, pagination, history and race conditions, on an in-memory fake database.
* `python tests/smoke_live.py`: needs a real MongoDB and the server running. It seeds awkward data (employee with no logs, mid-month joiner, weekend records, ties, orphan logs, days with no records), compares every analytics endpoint with values computed independently in Python, and checks that `/admin/explain` shows `IXSCAN` and no `COLLSCAN`. All checks passed locally.
* The 9 sample documents in `DATA_MODEL.md` give the same derived values when recomputed by the code.

## Notes
* Reported numbers use exact half-up rounding. The pipelines return integer sums and the final division is done in Python with `Fraction`, because MongoDB's `$round` rounds half to even.
* `moving_avg_7d` averages the already rounded (4 decimals) `attendance_rate` values.
* Nothing is missing from the contract.
* `REVIEW.md` lists the defects found in the starter code; `DECISIONS.md` answers the five design questions.
