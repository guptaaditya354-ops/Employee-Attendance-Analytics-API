"""Core tests (rules R1-R5, CRUD, races) against an in-process fake MongoDB (mongomock).
Aggregation pipelines ($lookup/$facet/$setWindowFields/...) are NOT supported by mongomock;
run tests/smoke_live.py against a real MongoDB for those."""
import threading
from datetime import datetime, timezone
import mongomock, pytest
from fastapi.testclient import TestClient
from app import main

IST_H = 5.5
def ms(y, mo, d, h, mi, s=0, ms_=0):  # IST wall clock -> epoch ms
    dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000) - int(IST_H * 3600 * 1000) + ms_

@pytest.fixture()
def c(monkeypatch):
    mc = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(main, "db", mc["t"])
    main.ensure_indexes()
    cl = TestClient(main.app)
    for code, ss, se in (("EMP0001", "09:30", "18:30"), ("EMP0005", "22:00", "06:00")):
        r = cl.post("/employees", json={"emp_code": code, "name": "N", "email": "a@b.co", "department": "Eng",
                                        "shift_start": ss, "shift_end": se, "joined_on": "2026-01-05"})
        assert r.status_code == 201, r.text
    return cl

def test_late_boundaries(c):
    for (h, m, s, exp) in ((9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 0, 0, 0)):
        r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, h, m, s)})
        assert r.status_code == 201 and r.json()["late_minutes"] == exp, (h, m, s, r.json())
        c.db = None
        main.db.attendance_logs.delete_many({})

def test_truncation_and_ist_date(c):
    # 09:40:00.900 counts as 09:40:00 -> not late; stored value truncated
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 0, 900)})
    j = r.json(); assert j["late_minutes"] == 0 and j["punch_in"] % 1000 == 0
    # 00:10 IST is still 2026-07-07 in IST (but 2026-07-06 in UTC)
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 10)})
    assert r.json()["date"] == "2026-07-07"

def test_duplicate_and_unknown(c):
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    assert c.post("/attendance/punch-in", json=body).status_code == 201
    assert c.post("/attendance/punch-in", json=body).status_code == 409
    assert c.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404

@pytest.mark.parametrize("bad", [1783312500, 1783312500000.0, "1783312500000", True, 5e12])
def test_bad_timestamps(c, bad):
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422

def test_bad_status(c):
    for s in ("ABSENT", "LEAVE", "x"):
        assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": s}).status_code == 422

def test_punch_out_rules(c):
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    out = ms(2026, 7, 6, 19, 10)
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}); j = r.json()
    assert r.status_code == 200 and j["late_minutes"] == 35 and j["overtime_minutes"] == 40
    assert j["work_hours"] == 9.08 and j["half_day"] is False
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}).status_code == 409
    assert c.post("/attendance/punch-out", json={"emp_code": "NOPE"}).status_code == 404

def test_punch_out_422_and_404(c):
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404  # no punch-in
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 0)})
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 0)}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 1)}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 0)}).status_code == 200

def test_overtime_threshold_and_half_day(c):
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30), "status": "WFH"})
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 59, 59)}).json()
    assert r["overtime_minutes"] == 0   # 29 min < 30
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 13, 0)}).json()
    assert r["work_hours"] == 3.5 and r["half_day"] is True
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 8, 9, 30)})
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 8, 13, 59, 59)}).json()
    assert r["work_hours"] == 4.5 and r["half_day"] is False   # 4.49997 rounds to 4.50 -> not a half day
    assert main.compute_work_hours(datetime(2026,1,1,tzinfo=timezone.utc), datetime(2026,1,1,0,0,18,tzinfo=timezone.utc)) == 0.01  # 0.005 -> half-up

def test_overnight(c):
    # punch in 21:55 on the 6th, out 06:40 on the 7th -> date 07-06, ot 40, hours 8.75
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)}).json()
    assert r["date"] == "2026-07-06" and r["late_minutes"] == 0
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)}).json()
    assert r["overtime_minutes"] == 40 and r["work_hours"] == 8.75
    # early-morning punch belongs to the previous day's shift
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 9, 2, 0)}).json()
    assert r["date"] == "2026-07-08"

def test_pagination_and_list(c):
    for d in range(1, 6):
        c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, d, 9, 30)})
        c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, d, 22, 0)})
    j = c.get("/attendance", params={"page_size": 3, "page": 2}).json()
    assert j["total"] == 10 and len(j["items"]) == 3
    j = c.get("/attendance", params={"date_from": "2026-07-02", "date_to": "2026-07-03", "emp_code": "EMP0001"}).json()
    assert j["total"] == 2 and [i["date"] for i in j["items"]] == ["2026-07-03", "2026-07-02"]
    assert c.get("/attendance", params={"date_from": "2026-07-04", "date_to": "2026-07-03"}).status_code == 422
    assert c.get("/attendance", params={"page_size": 101}).status_code == 422
    assert c.get("/attendance", params={"page": 0}).status_code == 422
    assert c.get("/attendance", params={"status": "bogus"}).status_code == 422
    assert c.get("/attendance", params={"page": 10**30}).json()["items"] == []
    assert "id" not in j["items"][0] and "_id" not in j["items"][0]
    j = c.get("/employees", params={"page_size": 1, "page": 2}).json()
    assert j["total"] == 2 and j["items"][0]["emp_code"] == "EMP0005"
    assert c.get("/employees", params={"department": "eng"}).json()["total"] == 0

def test_employee_validation(c):
    base = {"emp_code": "EMP0009", "name": "N", "email": "a@b.co", "department": "D", "joined_on": "2026-01-05"}
    assert c.post("/employees", json=base).status_code == 201
    assert c.post("/employees", json=base).status_code == 409
    for patch in ({"emp_code": "X1"}, {"joined_on": "2026-02-30"}, {"shift_start": "9:30"}, {"email": "nope"},
                  {"shift_start": "09:30", "shift_end": "09:30"}, {"name": ""}):
        assert c.post("/employees", json={**base, "emp_code": "EMP0010", **patch}).status_code == 422, patch
    r = c.post("/employees", json={**base, "emp_code": "EMP0011"}).json()
    assert r["shift_start"] == "09:30" and isinstance(r["created_at"], int) and "_id" not in r

def test_regularize(c):
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 19, 0)})
    url = "/attendance/EMP0001/2026-07-07"
    base = {"reason": "biometric glitch", "regularized_by": "hr"}
    assert c.patch(url, json=base).status_code == 422                                  # nothing changes
    assert c.patch(url, json={**base, "status": "LEAVE", "punch_in": ms(2026,7,7,9,0)}).status_code == 422
    assert c.patch(url, json={**base, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422  # wrong day
    assert c.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 9, 0)}).status_code == 422  # before in
    assert c.patch(url, json={**base, "punch_in": None}).status_code == 422
    assert c.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    assert c.patch("/attendance/NOPE/2026-07-07", json={**base, "status": "LEAVE"}).status_code == 404
    assert c.patch(url, json={**base, "reason": "x"}).status_code == 422
    r = c.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 9, 28), "work_hours": 99})
    j = r.json(); assert r.status_code == 200
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.53 and len(j["history"]) == 1
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"}, ch
    assert ch["punch_in"]["from"] == ms(2026, 7, 7, 10, 5)
    r = c.patch(url, json={**base, "status": "LEAVE"}); j = r.json()
    assert j["punch_in"] is None and j["work_hours"] is None and len(j["history"]) == 2
    assert j["history"][1]["changes"]["punch_in"]["to"] is None
    r = c.patch(url, json={**base, "status": "ON_DUTY"})            # presence needs a punch_in
    assert r.status_code == 422
    r = c.patch(url, json={**base, "status": "ON_DUTY", "punch_in": ms(2026, 7, 7, 9, 30)})
    assert r.status_code == 200 and len(r.json()["history"]) == 3

def test_legacy_record_without_history(c):
    main.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": datetime(2026,7,14,4,2,tzinfo=timezone.utc), "punch_out": datetime(2026,7,14,13,1,tzinfo=timezone.utc),
        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    j = c.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert j["history"] == [] and j["half_day"] is False
    r = c.patch("/attendance/EMP0001/2026-07-14", json={"reason": "forgot lunch", "regularized_by": "hr", "status": "WFH"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1

def test_race_punch_in_and_out(c):
    codes, outs = [], []
    def pin(): codes.append(c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}).status_code)
    ts = [threading.Thread(target=pin) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(codes) == [201] + [409] * 11
    def pout(): outs.append(c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}).status_code)
    ts = [threading.Thread(target=pout) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(outs) == [200] + [409] * 11

def test_regularize_race_no_lost_history(c):
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    res = []
    def go(i): res.append(c.patch("/attendance/EMP0001/2026-07-06", json={"reason": "race test", "regularized_by": f"u{i}", "punch_in": ms(2026, 7, 6, 9, i)}).status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(1, 9)]
    [t.start() for t in ts]; [t.join() for t in ts]
    n = len(main.db.attendance_logs.find_one({"emp_code": "EMP0001"})["history"])
    assert res.count(200) == n and set(res) <= {200, 409}, (res, n)

def test_helpers_rounding_and_dates():
    assert main.rhu(__import__("fractions").Fraction(5, 1000), 2) == 0.01
    assert main.rhu(__import__("fractions").Fraction(1, 3), 4) == 0.3333
    assert main.weekdays_between(main.Date(2026, 7, 1), main.Date(2026, 7, 31)) == 23
    assert main.month_bounds("2026-02")[1].day == 28
