"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)

Conventions used throughout this file
  * Instants are BSON datetimes (UTC, tz-aware) in MongoDB and epoch *milliseconds* in the API.
  * Calendar dates ("YYYY-MM-DD") and shift times ("HH:MM") are plain strings, IST.
  * All reported rounding is half-up and done with exact integer / Fraction arithmetic
    (MongoDB's $round is banker's rounding, so the pipelines return exact integer sums and
    the final division + rounding happens on the few result rows).
"""
import json
import logging
import os
import re
import time
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HOURS = 4.5  # R5

StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceT = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMs = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes():
    global _indexes_ready
    # employees: identity (race-safe unique emp_code), list/filter by department, headcount by join date
    db.employees.create_index([("emp_code", 1)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    db.employees.create_index([("joined_on", 1)], name="joined_on")
    # attendance_logs: one record per (employee, day) -> punch-in race guard, per-employee
    # range queries, $lookup joins and punch-out lookup ("latest record up to a time")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_code_date_unique")
    # GET /attendance default order (date desc, emp_code asc) with date-range filters
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp_code")
    # late leaderboard: date range + late_minutes > 0, grouped by emp_code (covered by the index)
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="date_late_emp")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app):
    deadline = time.time() + 12
    while True:
        try:
            ensure_indexes()
            break
        except Exception as exc:  # MongoDB may still be starting; /health retries later
            if time.time() > deadline:
                log.error("could not create indexes at startup: %s", exc)
                break
            time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
def mongo_down(_request, exc):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Small helpers: validation, time, exact rounding
# --------------------------------------------------------------------------- #
def invalid(loc, msg):
    """Raise a 422 in FastAPI's default validation-error shape."""
    raise RequestValidationError([{"type": "value_error", "loc": list(loc), "msg": msg}])


def parse_date(value: str, loc) -> Date:
    if not DATE_RE.fullmatch(value):
        invalid(loc, "date must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        invalid(loc, "not a valid calendar date")


def to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)  # BSON dates hold milliseconds


def rhu(value, places: int) -> float:
    """Exact round-half-up of a non-negative rational to `places` decimals (R4, R8)."""
    x = Fraction(value) * 10 ** places
    n = (2 * x.numerator + x.denominator) // (2 * x.denominator)
    return n / 10 ** places


def minutes_of(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(emp) -> bool:
    return minutes_of(emp["shift_end"]) <= minutes_of(emp["shift_start"])


def shift_dt(day: Date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, emp) -> Date:
    """R1: IST calendar date of the punch-in; early-morning punches of an overnight shift
    belong to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < minutes_of(emp["shift_end"]) * 60:
            day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, day: Date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    secs = int((punch_in - shift_dt(day, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    return rhu(Fraction(int((punch_out - punch_in).total_seconds()), 3600), 2)


def compute_overtime(punch_out: datetime, day: Date, emp) -> int:
    """R3: floor minutes after shift end, only if >= 30. Overnight shifts end on the next calendar day."""
    end = shift_dt(day, emp["shift_end"])
    if is_overnight(emp):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(emp, day: Date, status: str, punch_in, punch_out) -> dict:
    """All derived fields for a record, from its final state (table in DATA_MODEL.md section 3)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, day, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, day, emp),
        "half_day": hours < HALF_DAY_HOURS,
    }


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def emp_out(doc) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entry) -> dict:
    changes = {}
    for field, ft in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            ft = {"from": to_ms(ft.get("from")), "to": to_ms(ft.get("to"))}
        changes[field] = ft
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def rec_out(doc) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v):
        if not DATE_RE.fullmatch(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceT = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


# --------------------------------------------------------------------------- #
# system, employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:  # uniqueness is enforced by the unique index, not by a check-then-insert
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return emp_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = []
    if skip < total:
        items = [emp_out(d) for d in db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:  # unique index (emp_code, date): of N simultaneous requests exactly one insert wins
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return rec_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    # most recent record whose punch_in <= ts (records without punch_in never match a date comparison)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=24):
        invalid(["body", "punched_at"], "punched_at must be after punch_in and within 24 hours of it")
    der = derive(emp, Date.fromisoformat(rec["date"]), rec["status"], pin, ts)
    # the filter on punch_out: null makes the close atomic - a concurrent second punch-out matches nothing
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": der["work_hours"],
            "overtime_minutes": der["overtime_minutes"],
            "half_day": der["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return rec_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status):
    """Filter + sort for GET /attendance; shared with /admin/explain so both build it identically."""
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date(date_from, ["query", "date_from"]) if date_from is not None else None
    d_to = parse_date(date_to, ["query", "date_to"]) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        invalid(["query", "date_from"], "date_from must not be after date_to")
    rng = {}
    if date_from is not None:
        rng["$gte"] = date_from
    if date_to is not None:
        rng["$lte"] = date_to
    if rng:
        q["date"] = rng
    if status is not None:
        q["status"] = status
    return q, [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q, sort = build_attendance_query(emp_code, date_from, date_to, status)
    col = db.attendance_logs
    total = col.count_documents(q) if q else col.estimated_document_count()
    skip = (page - 1) * page_size
    items = []
    if skip < total:
        items = [rec_out(d) for d in col.find(q).sort(sort).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ["path", "date"])
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status or rec["status"]
    if new_status in PRESENCE:
        new_in = to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            invalid(["body", "punch_in"], "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp).isoformat() != date:
            invalid(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            invalid(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours of it")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            invalid(["body", "status"], "punch_in / punch_out cannot be set together with ABSENT or LEAVE")
        new_in = new_out = None

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive(emp, Date.fromisoformat(date), new_status, new_in, new_out))
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    if not changes:
        invalid(["body"], "the request changes nothing")

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: the update only applies if the record is still exactly as we read it
    # (same state, same history length). A concurrent editor loses with 409 instead of overwriting.
    n = len(rec.get("history") or [])
    flt = {
        "emp_code": emp_code, "date": date,
        "status": old["status"], "punch_in": old["punch_in"], "punch_out": old["punch_out"],
        f"history.{n}": {"$exists": False},
    }
    if n:
        flt[f"history.{n - 1}"] = {"$exists": True}
    updated = db.attendance_logs.find_one_and_update(
        flt, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return rec_out(updated)


# --------------------------------------------------------------------------- #
# analytics: pipeline builders
# --------------------------------------------------------------------------- #
MonthQ = Annotated[str, Query(pattern=MONTH_PATTERN)]


def month_bounds(month: str):
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        invalid(["query", "month"], "invalid year")
    return Date(year, mon, 1), Date(year, mon, monthrange(year, mon)[1])


def weekdays_between(a: Date, b: Date) -> int:
    """Mon-Fri days in [a, b] (R7); no holiday calendar."""
    if a > b:
        return 0
    return sum(1 for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5)


# expressions over one attendance_logs document; every optional field tolerates "missing" (legacy data)
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
_IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
_IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_HALVES_ANY_DAY = {"$cond": [_IS_PRESENT, {"$cond": [_IS_HALF, 1, 2]}, 0]}  # presence in half-day units
_LATE_FLAG = {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _stats_accumulators() -> dict:
    """Integer-valued sums, so the final division/rounding is exact. halves = present days x 2."""
    return {
        "halves": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, {"$cond": [_IS_HALF, 1, 2]}, 0]}},
        "wh_cents": {"$sum": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
        "wh_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
        "late_count": {"$sum": _LATE_FLAG},
        "late_minutes": {"$sum": _LATE},
        "ot_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    }


def p_employee_monthly(emp_code: str, first: Date, last: Date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None, **_stats_accumulators()}},
    ]


def p_department_summary(first: Date, last: Date, department: Optional[str]) -> list:
    """Starts from employees (so people with zero logs still count, R9) and joins their month of logs."""
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {"_id": None, **_stats_accumulators()}},
            ],
            "as": "s",
        }},
        {"$set": {"s": {"$arrayElemAt": ["$s", 0]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            **{k: {"$sum": {"$ifNull": [f"$s.{k}", 0]}} for k in
               ("halves", "wh_cents", "wh_n", "late_count", "late_minutes", "leave", "on_duty")},
        }},
        {"$sort": {"_id": 1}},
    ]


def p_late_leaderboard(first: Date, last: Date, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4); ties share a rank
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": "$total", "late_count": 1,
        }},
    ]
    return pipeline


def p_department_trend(department: str, d_from: Date, d_to: Date) -> list:
    """One employees-side pass: $facet computes (a) join-date counts and (b) per-day record sums,
    then $range builds exactly one row per calendar day (gap filling inside the database) and
    $setWindowFields adds the 7-row moving window over the requested range only."""
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    first, last = d_from.isoformat(), d_to.isoformat()
    x_p = {"$ifNull": ["$$x.p", 0]}
    day_row = {"$let": {
        "vars": {"dt": {"$add": [start, {"$multiply": ["$$i", 86400000]}]}},
        "in": {"$let": {
            "vars": {
                "d": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}},
                "wd": {"$lte": [{"$isoDayOfWeek": "$$dt"}, 5]},
            },
            "in": {"$let": {
                "vars": {
                    "hc": {"$sum": {"$map": {
                        "input": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j._id", "$$d"]}}},
                        "as": "k", "in": "$$k.n"}}},
                    "x": {"$arrayElemAt": [
                        {"$filter": {"input": "$days", "as": "e", "cond": {"$eq": ["$$e._id", "$$d"]}}}, 0]},
                },
                "in": {
                    "date": "$$d",
                    "is_working_day": "$$wd",
                    "headcount": "$$hc",
                    "p": x_p,
                    "l": {"$ifNull": ["$$x.l", 0]},
                    # rate scaled by 10^4 and rounded half-up with integer arithmetic:
                    # floor((10000*p + hc) / (2*hc))  ==  half_up(p/2/hc * 10^4)
                    "rate": {"$cond": [
                        {"$and": ["$$wd", {"$gt": ["$$hc", 0]}]},
                        {"$floor": {"$divide": [
                            {"$add": [{"$multiply": [10000, x_p]}, "$$hc"]},
                            {"$multiply": [2, "$$hc"]}]}},
                        None]},
                },
            }},
        }},
    }}
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department}},
        {"$facet": {
            "joins": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
            "days": [
                {"$lookup": {
                    "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                    "pipeline": [
                        {"$match": {"date": {"$gte": first, "$lte": last}}},
                        {"$project": {"_id": 0, "date": 1, "p": _HALVES_ANY_DAY, "l": _LATE_FLAG}},
                    ],
                    "as": "logs",
                }},
                {"$unwind": "$logs"},
                {"$group": {"_id": "$logs.date", "p": {"$sum": "$logs.p"}, "l": {"$sum": "$logs.l"}}},
            ],
        }},
        {"$project": {"_id": 0, "rows": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": day_row}}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "ma_sum": {"$sum": "$rate", "window": window},
            "ma_n": {"$sum": {"$cond": [{"$ne": ["$rate", None]}, 1, 0]}, "window": window},
        }}},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# analytics: endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    agg = next(iter(db.attendance_logs.aggregate(p_employee_monthly(emp_code, first, last))), {})
    working_days = weekdays_between(max(first, Date.fromisoformat(emp["joined_on"])), last)  # R7
    halves = int(agg.get("halves", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(agg.get("leave", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("ot_minutes", 0)),
        # present_days / working_days * 100 == halves * 50 / working_days
        "attendance_pct": rhu(Fraction(halves * 50, working_days), 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(p_department_summary(first, last, department)):
        wh_n = int(r["wh_n"])
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": int(r["halves"]) / 2,
            "avg_work_hours": rhu(Fraction(int(round(r["wh_cents"])), 100 * wh_n), 2) if wh_n else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["late_minutes"]),
            "leave_count": int(r["leave"]),
            "on_duty_count": int(r["on_duty"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(p_late_leaderboard(first, last, limit, department)))
    for r in rows:
        r["rank"] = int(r["rank"])
        r["total_late_minutes"] = int(r["total_late_minutes"])
        r["late_count"] = int(r["late_count"])
    return {"month": month, "items": rows}


def check_trend_range(d_from: str, d_to: str):
    a = parse_date(d_from, ["query", "from"])
    b = parse_date(d_to, ["query", "to"])
    if b < a:
        invalid(["query", "to"], "'to' must not be before 'from'")
    if (b - a).days + 1 > 92:
        invalid(["query", "to"], "range must not exceed 92 days")
    return a, b


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, d_from: Annotated[str, Query(alias="from")],
                     d_to: Annotated[str, Query(alias="to")]):
    a, b = check_trend_range(d_from, d_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(p_department_trend(department, a, b)):
        rate = r.get("rate")
        ma_n = int(r["ma_n"])
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": int(r["p"]) / 2,
            "late_count": int(r["l"]),
            "attendance_rate": None if rate is None else int(rate) / 10000,
            "moving_avg_7d": rhu(Fraction(int(r["ma_sum"]), ma_n), 0) / 10000 if ma_n else None,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# admin: explain
# --------------------------------------------------------------------------- #
def _need(value, name):
    if value is None:
        invalid(["query", name], f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    d_from: Optional[str] = Query(None, alias="from"),
    d_to: Optional[str] = Query(None, alias="to"),
    page: Page = 1,
    page_size: PageSize = 20,
):
    # Each branch builds the operation with the very same builder the real endpoint uses.
    if endpoint == "attendance_list":
        q, sort = build_attendance_query(emp_code, date_from, date_to, status)
        coll = "attendance_logs"
        inner = {"find": coll, "filter": q, "sort": dict(sort),
                 "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            first, last = month_bounds(_need(month, "month"))
            coll, pipeline = "attendance_logs", p_employee_monthly(_need(emp_code, "emp_code"), first, last)
        elif endpoint == "department_summary":
            first, last = month_bounds(_need(month, "month"))
            coll, pipeline = "employees", p_department_summary(first, last, department)
        elif endpoint == "late_leaderboard":
            first, last = month_bounds(_need(month, "month"))
            coll, pipeline = "attendance_logs", p_late_leaderboard(first, last, limit, department)
        else:
            a, b = check_trend_range(_need(d_from, "from"), _need(d_to, "to"))
            coll, pipeline = "employees", p_department_trend(_need(department, "department"), a, b)
        inner = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    plain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": plain}
