"""Live smoke test for the analytics endpoints. Needs a REAL MongoDB (6.0+) and a running server.

    export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_smoke
    uvicorn app.main:app --port 8000 &
    python tests/smoke_live.py            # seeds MONGO_DB (drops both collections!) and compares

Expected values are computed here in plain Python from the seeded documents (independent of the
Mongo pipelines), covering: employee with no logs, mid-month joiner, weekend records, ties at the
leaderboard cutoff, half days, legacy docs without half_day, orphan logs, gap days in the trend.
"""
import os, random, sys
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()  # picks up MONGO_URI from .env (real environment variables still win)

BASE = os.getenv("BASE_URL", "http://localhost:8000")
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_smoke")]
PRES = {"PRESENT", "WFH", "ON_DUTY"}

def rhu(x, p):
    x = Fraction(x) * 10 ** p
    return ((2 * x.numerator + x.denominator) // (2 * x.denominator)) / 10 ** p

emps = [("EMP0001", "Eng", "2026-01-05"), ("EMP0002", "Eng", "2026-01-05"), ("EMP0003", "Eng", "2026-07-15"),
        ("EMP0004", "Eng", "2026-01-05"),  # EMP0004: no logs at all
        ("EMP0005", "Ops", "2026-01-05"), ("EMP0006", "Ops", "2026-01-05"), ("EMP0007", "Ops", "2026-08-01"),
        ("EMP0008", "Ops", "2026-01-05")]
db.employees.drop(); db.attendance_logs.drop()
db.employees.insert_many([{"emp_code": c, "name": f"Name {c}", "email": "x@y.zz", "department": d, "shift_start": "09:30",
                           "shift_end": "18:30", "joined_on": j, "created_at": datetime.now(timezone.utc)} for c, d, j in emps])
rnd = random.Random(7)
logs = []
for c, d, j in emps:
    if c in ("EMP0004", "EMP0007"): continue
    for day in range(1, 32):
        dt = date(2026, 7, day)
        if rnd.random() < 0.25 or dt < date.fromisoformat(j) and rnd.random() < 0.9: continue  # gaps = days with no record
        st = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [8, 2, 1, 1, 1])[0]
        doc = {"emp_code": c, "date": dt.isoformat(), "status": st, "history": []}
        if st in PRES:
            late = rnd.choice([0, 0, 0, 12, 35, 35, 50]); hours = round(rnd.choice([3.5, 4.49, 4.5, 8.0, 9.05, 9.5]), 2)
            doc.update(punch_in=datetime(2026, 7, day, 4, tzinfo=timezone.utc), punch_out=datetime(2026, 7, day, 13, tzinfo=timezone.utc),
                       work_hours=hours, late_minutes=late, overtime_minutes=rnd.choice([0, 0, 45]), half_day=hours < 4.5)
            if rnd.random() < 0.1: del doc["half_day"]  # legacy doc (treated as False)
            if rnd.random() < 0.1: doc.update(punch_out=None, work_hours=None, overtime_minutes=0, half_day=False)  # open record
        else:
            doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
        logs.append(doc)
logs.append({"emp_code": "EMP9999", "date": "2026-07-08", "status": "PRESENT", "late_minutes": 999, "overtime_minutes": 0, "work_hours": 8.0,
             "punch_in": None, "punch_out": None, "half_day": False, "history": []})  # orphan: must be ignored by the leaderboard
# force a tie at the cutoff: EMP0005 and EMP0006 get identical late totals
db.attendance_logs.insert_many(logs)
# drop() above also removed the indexes the running server created at startup -> recreate them (same function the app uses)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import main as app_main
app_main.ensure_indexes()
col = db.attendance_logs
emp_by = {c: (d, j) for c, d, j in emps}
july = [l for l in logs if l["date"].startswith("2026-07") and l["emp_code"] in emp_by]
wk = lambda s: date.fromisoformat(s).weekday() < 5
halves = lambda l: (1 if l.get("half_day") else 2) if l["status"] in PRES and wk(l["date"]) else 0
def working(j, a, b):
    a = max(a, date.fromisoformat(j)); return sum(1 for i in range((b - a).days + 1) if (a + timedelta(i)).weekday() < 5) if a <= b else 0

fails = 0
def check(name, got, exp):
    global fails
    if got != exp:
        fails += 1; print("FAIL", name, "\n  got", got, "\n  exp", exp)
    else: print("ok  ", name)

h = httpx.Client(base_url=BASE, timeout=30)
assert h.get("/health").status_code == 200

# employee monthly
for c, (d, j) in emp_by.items():
    ls = [l for l in july if l["emp_code"] == c]; hv = sum(halves(l) for l in ls); wd = working(j, date(2026, 7, 1), date(2026, 7, 31))
    exp = {"emp_code": c, "month": "2026-07", "working_days": wd, "present_days": hv / 2, "leave_days": sum(l["status"] == "LEAVE" for l in ls),
           "late_count": sum((l["late_minutes"] or 0) > 0 for l in ls), "total_late_minutes": sum(l["late_minutes"] or 0 for l in ls),
           "total_overtime_minutes": sum(l["overtime_minutes"] or 0 for l in ls), "attendance_pct": rhu(Fraction(hv * 50, wd), 2) if wd else None}
    check(f"monthly {c}", h.get(f"/analytics/employees/{c}/monthly", params={"month": "2026-07"}).json(), exp)
check("monthly 404", h.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code, 404)
check("monthly 422", h.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code, 422)

# department summary (July: EMP0007 joins in August -> not counted)
exp_items = []
for dep in sorted({d for _, d, _ in emps}):
    members = [c for c, (d, j) in emp_by.items() if d == dep and j <= "2026-07-31"]; ls = [l for l in july if l["emp_code"] in members]
    hrs = [l["work_hours"] for l in ls if l["status"] in PRES and l["work_hours"] is not None]
    exp_items.append({"department": dep, "headcount": len(members), "present_days": sum(halves(l) for l in ls) / 2,
                      "avg_work_hours": rhu(Fraction(round(sum(Fraction(str(x)) for x in hrs) * 100), 100 * len(hrs)), 2) if hrs else None,
                      "late_count": sum((l["late_minutes"] or 0) > 0 for l in ls), "total_late_minutes": sum(l["late_minutes"] or 0 for l in ls),
                      "leave_count": sum(l["status"] == "LEAVE" for l in ls), "on_duty_count": sum(l["status"] == "ON_DUTY" for l in ls)})
check("summary", h.get("/analytics/departments/summary", params={"month": "2026-07"}).json(), {"month": "2026-07", "items": exp_items})
check("summary dept filter", h.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()["items"],
      [i for i in exp_items if i["department"] == "Ops"])
check("summary empty month", h.get("/analytics/departments/summary", params={"month": "2020-01"}).json(), {"month": "2020-01", "items": []})

# leaderboard
def board(dep=None, limit=10):
    tot = {}
    for l in july:
        if (l["late_minutes"] or 0) > 0 and (dep is None or emp_by[l["emp_code"]][0] == dep):
            t = tot.setdefault(l["emp_code"], [0, 0]); t[0] += l["late_minutes"]; t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0])); out = []
    for i, (c, (m, n)) in enumerate(rows):
        rank = 1 + sum(1 for _, (m2, _) in rows if m2 > m)
        if rank <= limit: out.append({"rank": rank, "emp_code": c, "name": f"Name {c}", "department": emp_by[c][0], "total_late_minutes": m, "late_count": n})
    return out
for dep, lim in ((None, 10), (None, 2), (None, 1), ("Eng", 3), ("Ops", 50)):
    params = {"month": "2026-07", "limit": lim, **({"department": dep} if dep else {})}
    check(f"leaderboard dep={dep} limit={lim}", h.get("/analytics/leaderboard/late", params=params).json()["items"], board(dep, lim))
print("     leaderboard ranks:", [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in board()])

# trend (also a range crossing the month boundary, with days that have no records, and August with a joiner)
def trend(dep, a, b):
    rows, rates = [], []
    members = [(c, j) for c, (d, j) in emp_by.items() if d == dep]
    for i in range((b - a).days + 1):
        day = a + timedelta(i); ds = day.isoformat(); ls = [l for l in logs if l["emp_code"] in dict(members) and l["date"] == ds]
        hc = sum(j <= ds for _, j in members); p = sum(((1 if l.get("half_day") else 2) if l["status"] in PRES else 0) for l in ls)
        rate = rhu(Fraction(p, 2 * hc), 4) if day.weekday() < 5 and hc else None
        rates.append(rate); win = [r for r in rates[-7:] if r is not None]
        ma = rhu(sum(Fraction(str(r)) for r in win) / len(win), 4) if win else None
        rows.append({"date": ds, "is_working_day": day.weekday() < 5, "headcount": hc, "present_count": p / 2,
                     "late_count": sum((l["late_minutes"] or 0) > 0 for l in ls), "attendance_rate": rate, "moving_avg_7d": ma})
    return rows
for dep, a, b in (("Eng", date(2026, 7, 1), date(2026, 7, 31)), ("Ops", date(2026, 7, 25), date(2026, 8, 10)), ("Eng", date(2027, 1, 1), date(2027, 1, 3))):
    got = h.get(f"/analytics/departments/{dep}/trend", params={"from": a.isoformat(), "to": b.isoformat()}).json()
    check(f"trend {dep} {a}..{b}", got["items"], trend(dep, a, b))
check("trend 404", h.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code, 404)
check("trend 422 order", h.get("/analytics/departments/Eng/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code, 422)
check("trend 422 >92d", h.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-10-02"}).status_code, 422)
check("trend 92d ok", h.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code, 200)

# explain: IXSCAN and no COLLSCAN
import json
for ep, params in (("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}), ("attendance_list", {}),
                   ("attendance_list", {"emp_code": "EMP0001"}), ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
                   ("department_summary", {"month": "2026-07"}), ("department_summary", {"month": "2026-07", "department": "Eng"}),
                   ("late_leaderboard", {"month": "2026-07"}), ("late_leaderboard", {"month": "2026-07", "department": "Eng"}),
                   ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"})):
    r = h.get(f"/admin/explain/{ep}", params=params); s = json.dumps(r.json()) if r.status_code == 200 else ""
    check(f"explain {ep} {params}", (r.status_code, "IXSCAN" in s, "COLLSCAN" in s), (200, True, False))
print("\nFAILURES:", fails); sys.exit(1 if fails else 0)
