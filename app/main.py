import os, re, calendar
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, ConfigDict, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=1500)
db = client[os.getenv("MONGO_DB", "attendance_db")]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = PRESENCE + ["ABSENT", "LEAVE"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def now_utc(): return datetime.now(UTC).replace(microsecond=0)
def dt_in(ms):
    if ms is None: return now_utc()
    if isinstance(ms, bool) or not isinstance(ms, int) or not 100_000_000_000 <= ms <= 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)
def dt_out(v):
    if v is None: return None
    if v.tzinfo is None: v = v.replace(tzinfo=UTC)
    return int(v.astimezone(UTC).replace(microsecond=0).timestamp() * 1000)
def round_half_up(v, places=2):
    return float(Decimal(str(v)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))
def time_parts(s): return tuple(map(int, s.split(":")))
def shift_start_dt(day, start): 
    h,m=time_parts(start); return datetime.fromisoformat(day).replace(hour=h,minute=m,tzinfo=IST).astimezone(UTC)
def shift_end_dt(day, start, end):
    sh,sm=time_parts(start); eh,em=time_parts(end)
    d=date.fromisoformat(day)
    if (eh,em) <= (sh,sm): d += timedelta(days=1)
    return datetime.combine(d, datetime.min.time(), IST).replace(hour=eh, minute=em).astimezone(UTC)
def attendance_day(ts, start, end):
    local=ts.astimezone(IST); sh,sm=time_parts(start); eh,em=time_parts(end)
    if (eh,em) <= (sh,sm) and (local.hour,local.minute) < (eh,em):
        return (local.date()-timedelta(days=1)).isoformat()
    return local.date().isoformat()
def late_minutes(ts, day, start):
    delta=int((ts-shift_start_dt(day,start)).total_seconds())
    return delta//60 if delta > 600 else 0
def work_hours(a,b): return round_half_up((b-a).total_seconds()/3600,2)
def overtime(b, day, start, end):
    mins=int((b-shift_end_dt(day,start,end)).total_seconds()//60)
    return mins if mins >= 30 else 0
def present_weight(doc):
    if doc.get("status") not in PRESENCE: return 0.0
    return 0.5 if doc.get("half_day",False) else 1.0
def valid_date(s):
    try:
        parsed = date.fromisoformat(s)
        if parsed.isoformat() != s:
            raise ValueError
        return s
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="date must be YYYY-MM-DD"
        )
def month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""): raise HTTPException(422,"month must be YYYY-MM")
    y,m=map(int,month.split("-")); start=f"{y:04d}-{m:02d}-01"; end=f"{y:04d}-{m:02d}-{calendar.monthrange(y,m)[1]:02d}"
    return start,end
def serialize_emp(d):
    d.pop("_id",None)
    if "created_at" in d: d["created_at"]=dt_out(d["created_at"])
    return d
def serialize_log(d):
    d.pop("_id",None)
    for k in ("punch_in","punch_out"): d[k]=dt_out(d.get(k))
    d.setdefault("work_hours",None); d.setdefault("late_minutes",0); d.setdefault("overtime_minutes",0); d.setdefault("half_day",False); d.setdefault("history",[])
    for h in d["history"]:
        h["at"]=dt_out(h.get("at"))
        for change in h.get("changes",{}).values():
            for key in ("from","to"):
                if isinstance(change.get(key),datetime): change[key]=dt_out(change[key])
    return d
def indexes():
    db.employees.create_index([("emp_code",ASCENDING)],unique=True,name="uq_emp_code")
    db.employees.create_index([("department",ASCENDING),("joined_on",ASCENDING)],name="ix_emp_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)],name="idx_employees_joined_on")
    db.attendance_logs.create_index([("emp_code",ASCENDING),("date",ASCENDING)],unique=True,name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date",DESCENDING),("emp_code",ASCENDING)],name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("emp_code",ASCENDING),("date",ASCENDING),("status",ASCENDING)],name="ix_attendance_emp_date_status")
@app.on_event("startup")
def startup():
    try: client.admin.command("ping"); indexes()
    except Exception: pass # health reports unavailable; startup remains within readiness deadline

class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    emp_code:str=Field(pattern=r"^EMP\d{4,6}$")
    name:str=Field(min_length=1,max_length=100)
    email:str=Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:str=Field(min_length=1,max_length=50)
    shift_start:str=Field(default="09:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:str=Field(default="18:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:str
    @field_validator("joined_on")
    @classmethod
    def check_joined(cls,v): valid_date(v); return v
    @field_validator("shift_end")
    @classmethod
    def shift_diff(cls,v,info):
        if info.data.get("shift_start")==v: raise ValueError("shift_start must differ from shift_end")
        return v
class PunchInIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    emp_code:str
    punched_at:Optional[int]=None
    status:Literal["PRESENT","WFH","ON_DUTY"]="PRESENT"
class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    emp_code:str
    punched_at:Optional[int]=None
class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    status:Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]]=None
    punch_in:Optional[int]=None
    punch_out:Optional[int]=None
    reason:str=Field(min_length=5,max_length=200)
    regularized_by:str=Field(min_length=1,max_length=50)

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except Exception: raise HTTPException(503,"MongoDB unavailable")

@app.post("/employees",status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump()
    if doc["shift_start"]==doc["shift_end"]: raise HTTPException(422,"shift_start must differ from shift_end")
    doc["created_at"]=now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return serialize_emp(doc)
@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department is not None else {}
    docs=list(db.employees.find(q,{"_id":0}).sort("emp_code",1).skip((page-1)*page_size).limit(page_size))
    return {"items":[serialize_emp(x) for x in docs],"total":db.employees.count_documents(q),"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=dt_in(body.punched_at); day=attendance_day(ts,emp["shift_start"],emp["shift_end"])
    if day < emp["joined_on"]: raise HTTPException(422,"punch-in predates employment")
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,"late_minutes":late_minutes(ts,day,emp["shift_start"]),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return serialize_log(doc)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code: q["emp_code"]=emp_code
    if status:
        if status not in STATUSES: raise HTTPException(422,"invalid status")
        q["status"]=status
    if date_from or date_to:
        q["date"]={}
        if date_from: q["date"]["$gte"]=valid_date(date_from)
        if date_to: q["date"]["$lte"]=valid_date(date_to)
        if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")
    total=db.attendance_logs.count_documents(q)
    docs=list(db.attendance_logs.find(q,{"_id":0}).sort([("date",-1),("emp_code",1)]).skip((page-1)*page_size).limit(page_size))
    return {"items":[serialize_log(x) for x in docs],"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=dt_in(body.punched_at)
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts}},sort=[("punch_in",-1)])
    if not rec: raise HTTPException(404,"no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"already punched out")
    if ts<=rec["punch_in"] or ts-rec["punch_in"]>timedelta(hours=24): raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    hours=work_hours(rec["punch_in"],ts); ot=overtime(ts,rec["date"],emp["shift_start"],emp["shift_end"])
    updated=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},{"$set":{"punch_out":ts,"work_hours":hours,"overtime_minutes":ot,"half_day":hours<4.50}},return_document=True)
    if not updated: raise HTTPException(409,"already punched out")
    return serialize_log(updated)

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeIn):
    valid_date(date)
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    rec=db.attendance_logs.find_one({"emp_code":emp_code,"date":date})
    if not rec: raise HTTPException(404,"attendance record not found")
    changes={}
    new_status=body.status if body.status is not None else rec["status"]
    if new_status in ("ABSENT","LEAVE") and (body.punch_in is not None or body.punch_out is not None): raise HTTPException(422,"ABSENT/LEAVE cannot have punch times")
    pi=dt_in(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    po=dt_in(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
    if new_status in PRESENCE and pi is None: raise HTTPException(422,"presence status requires punch_in")
    if new_status in ("ABSENT","LEAVE"): pi=po=None
    if pi is not None and pi.astimezone(IST).date().isoformat()!=date: raise HTTPException(422,"punch_in must be on attendance date")
    if po is not None and (pi is None or po<=pi or po-pi>timedelta(hours=24)): raise HTTPException(422,"invalid punch_out")
    emp_fields={"status":new_status,"punch_in":pi,"punch_out":po}
    newvals=dict(emp_fields)
    newvals["late_minutes"]=late_minutes(pi,date,emp["shift_start"]) if pi else 0
    newvals["work_hours"]=work_hours(pi,po) if pi and po else None
    newvals["overtime_minutes"]=overtime(po,date,emp["shift_start"],emp["shift_end"]) if pi and po else 0
    newvals["half_day"]=bool(pi and po and newvals["work_hours"]<4.5)
    for k,v in newvals.items():
        old=rec.get(k)
        if old!=v: changes[k]={"from":dt_out(old) if k in ("punch_in","punch_out") else old,"to":dt_out(v) if k in ("punch_in","punch_out") else v}
    if not changes: raise HTTPException(422,"request changes nothing")
    entry={"at":now_utc(),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Atomic conditional update prevents lost history entries.
    result=db.attendance_logs.update_one({"_id":rec["_id"],"history":rec.get("history",[])},{"$set":newvals,"$push":{"history":entry}})
    if result.modified_count!=1: raise HTTPException(409,"record changed concurrently; retry")
    return serialize_log(db.attendance_logs.find_one({"_id":rec["_id"]}))

def month_match(month):
    a,b=month_bounds(month); return {"date":{"$gte":a,"$lte":b}}
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    start,end=month_bounds(month); effective=max(start,emp["joined_on"])
    workdays=sum(1 for i in range(max(0,(date.fromisoformat(end)-date.fromisoformat(effective)).days+1)) if (date.fromisoformat(effective)+timedelta(days=i)).weekday()<5)
    # Other monthly counters are aggregated in MongoDB. Attendance records are
    # already unique by employee/date; restrict them to the month and employment.
    counters_pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": effective, "$lte": end}}},
        {"$group": {"_id": None,
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}
        }}
    ]
    r = list(db.attendance_logs.aggregate(counters_pipeline)); x = r[0] if r else {}
    present_pipeline=[{"$match":{"emp_code":emp_code,"date":{"$gte":effective,"$lte":end}}},{"$addFields":{"_dt":{"$dateFromString":{"dateString":"$date"}},"_w":{"$cond":[{"$in":["$status",PRESENCE]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}}},{"$match":{"$expr":{"$in":[{"$dayOfWeek":"$_dt"},[2,3,4,5,6]]}}},{"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$eq":["$half_day",True]},{"$cond":[{"$in":["$status",PRESENCE]},0.5,0]},{"$cond":[{"$in":["$status",PRESENCE]},1,0]}]}}}}]
    pr=list(db.attendance_logs.aggregate(present_pipeline)); pd=pr[0]["present_days"] if pr else 0.0
    return {"emp_code":emp_code,"month":month,"working_days":workdays,"present_days":round_half_up(pd,2),"leave_days":x.get("leave_days",0),"late_count":x.get("late_count",0),"total_late_minutes":x.get("late_minutes",0),"attendance_pct":round_half_up(pd/workdays*100,4) if workdays else None}

def build_department_summary_pipeline(start, end, department=None):
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", "$$joined"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                ]}}},
                {"$addFields": {"_dt": {"$dateFromString": {"dateString": "$date"}}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", PRESENCE]},
                            {"$in": [{"$dayOfWeek": "$_dt"}, [2, 3, 4, 5, 6]]}
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0
                    ]}},
                    # Keep sum and count so the department average is weighted
                    # by attendance records, not by the number of employees.
                    "work_hours_sum": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]},
                        "$work_hours", 0
                    ]}},
                    "work_hours_count": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]},
                        1, 0
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}
                }}
            ],
            "as": "stats"
        }},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$stats.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$stats.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}}
        }},
        {"$sort": {"_id": 1}}
    ]

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = month_bounds(month)
    rows = list(db.employees.aggregate(build_department_summary_pipeline(start, end, department)))
    items = []
    for r in rows:
        if r["headcount"] <= 0:
            continue
        count = r.get("work_hours_count", 0)
        avg = (r.get("work_hours_sum", 0) / count) if count else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(avg, 2) if avg is not None else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"]
        })
    return {"month": month, "items": items}

def build_late_leaderboard_pipeline(start, end, limit=10, department=None):
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "rank": 1}}
    ]
    return pipeline

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    start,end=month_bounds(month)
    pipeline = build_late_leaderboard_pipeline(start, end, limit, department)
    return {"month":month,"limit":limit,"items":list(db.attendance_logs.aggregate(pipeline))}

def build_department_trend_pipeline(department, start_date, end_date):
    """Build the exact MongoDB pipeline used by the trend endpoint."""
    pipeline = [
        {
            "$documents": [
                {
                    "date": (start_date + timedelta(days=i)).isoformat()
                }
                for i in range(
                    (end_date - start_date).days + 1
                )
            ]
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "headcount": {
                                "$sum": {
                                    "$cond": [
                                        {"$lte": ["$joined_on", "$$d"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "hc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "e",
                        }
                    },
                    {"$unwind": "$e"},
                    {"$match": {"$expr": {"$eq": ["$e.department", department]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", PRESENCE]},
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "att",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$first": "$hc.headcount"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$att.present_count"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$att.late_count"}, 0]},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$_avg", 4]},
            }
        },
    ]
    return pipeline

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_:str=Query(alias="from"),to:str=Query(...)):
    a=valid_date(from_); b=valid_date(to)
    start_date = date.fromisoformat(a)
    end_date = date.fromisoformat(b)
    if end_date < start_date or (end_date - start_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="invalid date range (maximum 92 days)"
        )    
    if not db.employees.find_one({"department":department}): raise HTTPException(404,"department not found")
    # Database-side gap filling, headcount join, and 7-day moving average.
    pipeline=[{"$match":{"department":department}},{"$project":{"emp_code":1,"joined_on":1}},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",a]},{"$lte":["$date",b]}]}}},{"$group":{"_id":"$date","present_count":{"$sum":{"$cond":[{"$in":["$status",PRESENCE]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"late_count":{"$sum":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}}}}],"as":"days"}},{"$unwind":{"path":"$days","preserveNullAndEmptyArrays":True}},{"$group":{"_id":"$days._id","present_count":{"$sum":{"$ifNull":["$days.present_count",0]}},"late_count":{"$sum":{"$ifNull":["$days.late_count",0]}}}}]
    # Use $documents/$densify from a calendar seed so empty days exist without Python loop.
    pipeline = build_department_trend_pipeline(department, start_date, end_date)
    # Use PyMongo's database-level aggregate API for a collectionless pipeline
    # whose first stage is $documents.
    rows = list(db.aggregate(pipeline))
    return {"department": department, "from": a, "to": b, "items": rows}

@app.get("/admin/explain/{endpoint}")
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_:Optional[str]=Query(None,alias="from"),to:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    allowed={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in allowed: raise HTTPException(422,"unknown endpoint")
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=valid_date(date_from)
            if date_to:q["date"]["$lte"]=valid_date(date_to)
        cmd={"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":db.command("explain",cmd,verbosity="executionStats")}
    if endpoint=="employee_monthly":
        if not emp_code or not month: raise HTTPException(422,"emp_code and month required")
        emp=db.employees.find_one({"emp_code":emp_code})
        if not emp: raise HTTPException(404,"employee not found")
        start,end=month_bounds(month); effective=max(start,emp["joined_on"])
        p=[{"$match":{"emp_code":emp_code,"date":{"$gte":effective,"$lte":end}}},{"$addFields":{"_dt":{"$dateFromString":{"dateString":"$date"}}}},{"$match":{"$expr":{"$in":[{"$dayOfWeek":"$_dt"},[2,3,4,5,6]]}}},{"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$in":["$status",PRESENCE]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}}}}]
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":db.command("explain",{"aggregate":"attendance_logs","pipeline":p,"cursor":{}},verbosity="executionStats")}
    if endpoint=="department_summary":
        if not month: raise HTTPException(422,"month required")
        start,end=month_bounds(month)
        p=build_department_summary_pipeline(start,end,department)
        return {"endpoint":endpoint,"collection":"employees","explain":db.command("explain",{"aggregate":"employees","pipeline":p,"cursor":{}},verbosity="executionStats")}
    if endpoint=="late_leaderboard":
        if not month: raise HTTPException(422,"month required")
        start,end=month_bounds(month)
        p=build_late_leaderboard_pipeline(start,end,limit,department)
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":db.command("explain",{"aggregate":"attendance_logs","pipeline":p,"cursor":{}},verbosity="executionStats")}
    if not department or not from_ or not to: raise HTTPException(422,"department, from and to required")
    a=valid_date(from_); b=valid_date(to)
    start_date=date.fromisoformat(a); end_date=date.fromisoformat(b)
    if end_date < start_date or (end_date-start_date).days + 1 > 92:
        raise HTTPException(422,"invalid date range (maximum 92 days)")
    if not db.employees.find_one({"department":department}): raise HTTPException(404,"department not found")
    p=build_department_trend_pipeline(department,start_date,end_date)
    return {"endpoint":endpoint,"collection":"$documents (collectionless aggregation)","explain":db.command("explain",{"aggregate":1,"pipeline":p,"cursor":{}},verbosity="executionStats")}
