"""One hospital, one slot per doctor-time. Shared by the API and the voice worker."""

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import database
from runtime_config import DATA_DIR, now_local

LOCK = DATA_DIR / "appointments.lock"

HOSPITAL = "City Hospital"
OPEN_AT = "09:00"
CLOSE_AT = "16:30"


def half_hour_slots() -> list[str]:
    """Clinic visits run every 30 minutes from 9:00 AM through 4:30 PM."""
    cursor = datetime.strptime(OPEN_AT, "%H:%M")
    end = datetime.strptime(CLOSE_AT, "%H:%M")
    slots = []
    while cursor <= end:
        slots.append(cursor.strftime("%H:%M"))
        cursor += timedelta(minutes=30)
    return slots


DAY_SLOTS = half_hour_slots()
DEPARTMENTS = {
    "general medicine": DAY_SLOTS,
    "skin": DAY_SLOTS,
    "pediatrics": DAY_SLOTS,
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@contextmanager
def _lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        try:
            if LOCK.exists() and time.time() - LOCK.stat().st_mtime > 10:
                LOCK.unlink(missing_ok=True)
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            time.sleep(0.02)
    else:
        raise TimeoutError("Appointment store is busy")
    try:
        yield
    finally:
        os.close(fd)
        LOCK.unlink(missing_ok=True)


def _load(owner_id: str) -> list:
    if not owner_id:
        return []
    return database.list_visits(owner_id)


def department_names() -> str:
    return ", ".join(DEPARTMENTS)


def describe_open(result: dict) -> str:
    """A short spoken answer. The full 30-minute grid is too long to read aloud."""
    slots = result["open_slots"]
    when = f"{result['weekday']} {result['date']}"
    if not slots:
        if result.get("passed_today"):
            return (
                f"It is {result['clock']} now. {result['department']} is closed for the rest of today. "
                "Say the day is closed. Do not say the slots are filled. Ask if another day works."
            )
        return f"No open times for {result['department']} on {when}."
    labels = [speak_time(slot) for slot in slots]
    first = ", ".join(labels[:4])
    tail = ""
    if len(labels) > 4:
        tail = (
            f" Visits continue every 30 minutes until {labels[-1]}. "
            f"{len(labels) - 4} more times are still open."
        )
    clock = ""
    if result.get("passed_today"):
        clock = f" It is {result['clock']} now, so earlier times today are closed."
    return (
        f"{result['department']} on {when} is open at {first}.{tail}{clock} "
        "Offer only these times. A time every 30 minutes from 9:00 AM to 4:30 PM can be booked if this list includes it."
    )


def resolve_day(day: str):
    text = " ".join(day.strip().lower().split())
    today = now_local().date()
    if text in {"today", ""}:
        found = today
    elif text == "tomorrow":
        found = today + timedelta(days=1)
    elif text in WEEKDAYS:
        delta = (WEEKDAYS.index(text) - today.weekday()) % 7
        found = today + timedelta(days=delta)
    else:
        found = datetime.strptime(text[:10], "%Y-%m-%d").date()
    if found < today or found > today + timedelta(days=14):
        raise ValueError("Choose a day in the next 14 days")
    if found.weekday() == 6:
        raise ValueError("City Hospital is closed on Sunday")
    return found


def _normalize_department(department: str) -> str:
    key = " ".join(department.strip().lower().split())
    aliases = {
        "general": "general medicine",
        "medicine": "general medicine",
        "gp": "general medicine",
        "dermatology": "skin",
        "skin doctor": "skin",
        "child": "pediatrics",
        "children": "pediatrics",
        "kids": "pediatrics",
        "pediatric": "pediatrics",
    }
    key = aliases.get(key, key)
    if key not in DEPARTMENTS:
        raise ValueError(f"Department must be one of: {department_names()}")
    return key


def _normalize_time(value: str) -> str:
    text = value.strip().lower().replace(".", ":")
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%H"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            continue
    raise ValueError("Time must look like 10:30")


def speak_time(slot: str) -> str:
    parsed = datetime.strptime(slot, "%H:%M")
    return parsed.strftime("%I:%M %p").lstrip("0")


def _still_ahead(when, slot: str, now: datetime | None = None) -> bool:
    """A slot today must still be at least 15 minutes away. Later days are unchanged."""
    current = now or now_local()
    if when != current.date():
        return True
    starts = datetime.combine(when, datetime.strptime(slot, "%H:%M").time())
    return starts >= current + timedelta(minutes=15)


def open_slots(department: str, day: str, owner_id: str) -> dict:
    dept = _normalize_department(department)
    when = resolve_day(day)
    now = now_local()
    taken = {
        row["time"]
        for row in _load(owner_id)
        if row.get("department") == dept and row.get("date") == when.isoformat()
    }
    free = [
        slot
        for slot in DEPARTMENTS[dept]
        if slot not in taken and _still_ahead(when, slot, now)
    ]
    return {
        "hospital": HOSPITAL,
        "department": dept,
        "date": when.isoformat(),
        "weekday": WEEKDAYS[when.weekday()],
        "open_slots": free,
        "clock": now.strftime("%I:%M %p").lstrip("0"),
        "passed_today": when == now.date() and any(
            not _still_ahead(when, slot, now) for slot in DEPARTMENTS[dept]
        ),
    }


def _ten_digits(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits if len(digits) == 10 else ""


def book(patient_name: str, phone: str, department: str, day: str, slot: str, owner_id: str, hospital: str) -> dict:
    try:
        return _book(patient_name, phone, department, day, slot, owner_id, hospital)
    except ValueError:
        database.audit("book", owner_id or "", "", False)
        raise


def _book(patient_name: str, phone: str, department: str, day: str, slot: str, owner_id: str, hospital: str) -> dict:
    if not owner_id:
        raise ValueError("This desk is not assigned to a hospital account.")
    name = " ".join(patient_name.strip().split())
    if len(name) < 2 or len(name) > 80:
        raise ValueError("Patient name is required")
    digits = _ten_digits(phone)
    if not digits:
        raise ValueError("A 10-digit phone number is required. Ask for all 10 digits.")
    chosen = _normalize_time(slot)
    available = open_slots(department, day, owner_id)
    if chosen not in available["open_slots"]:
        open_spoken = ", ".join(speak_time(slot) for slot in available["open_slots"]) or "none"
        raise ValueError(
            f"{speak_time(chosen)} is not open for {available['department']} "
            f"on {available['weekday']} {available['date']}. "
            f"Open times: {open_spoken}. Offer only these times."
        )
    with _lock():
        rows = _load(owner_id)
        if any(
            row.get("department") == available["department"]
            and row.get("date") == available["date"]
            and row.get("time") == chosen
            for row in rows
        ):
            raise ValueError(f"{speak_time(chosen)} was just taken. Ask for another time.")
        record = {
            "id": uuid.uuid4().hex[:8],
            "owner_id": owner_id,
            "hospital": hospital or HOSPITAL,
            "patient_name": name,
            "phone": digits,
            "department": available["department"],
            "date": available["date"],
            "time": chosen,
        }
        try:
            database.insert_visit(record)
        except sqlite3.IntegrityError:
            raise ValueError(f"{speak_time(chosen)} was just taken. Ask for another time.") from None
    database.audit("book", owner_id, record["id"], True)
    return record


def cancel(confirmation_id: str, patient_name: str, phone: str, owner_id: str) -> dict:
    try:
        return _cancel(confirmation_id, patient_name, phone, owner_id)
    except ValueError:
        database.audit("cancel", owner_id or "", confirmation_id or "", False)
        raise


def _cancel(confirmation_id: str, patient_name: str, phone: str, owner_id: str) -> dict:
    """Remove one visit only when the phone matches the booking."""
    if not owner_id:
        raise ValueError("This desk is not assigned to a hospital account.")
    cid = confirmation_id.strip().lower()
    name = " ".join(patient_name.strip().lower().split())
    digits = _ten_digits(phone)
    if not digits:
        raise ValueError("Ask for the 10-digit phone number used when the visit was booked.")
    if not cid and len(name) < 2:
        raise ValueError("Ask for the confirmation number or the patient name, and the phone number.")
    with _lock():
        matches = []
        for row in _load(owner_id):
            if row.get("phone") != digits:
                continue
            if cid and str(row.get("id", "")).lower() == cid:
                matches.append(row)
            elif name and name in str(row.get("patient_name", "")).lower():
                matches.append(row)
        if not matches:
            raise ValueError("No visit matches that patient and phone number.")
        if len(matches) > 1:
            choices = "; ".join(
                f"{row['id']} {row['department']} on {row['date']} at {speak_time(row['time'])}"
                for row in matches
            )
            raise ValueError(f"More than one visit matches. Ask which confirmation to cancel: {choices}")
        target = matches[0]
        database.delete_visit(target["id"], owner_id)
    database.audit("cancel", owner_id, target["id"], True)
    return target


def lookup(confirmation_id: str, phone: str) -> dict:
    """One visit. The phone must be the one given when it was booked."""
    cid = confirmation_id.strip().lower()
    digits = _ten_digits(phone)
    if len(cid) < 4 or not digits:
        raise ValueError("Enter the confirmation number and the 10-digit phone number used at booking.")
    for row in database.list_visits():
        if str(row.get("id", "")).lower() == cid and row.get("phone") == digits:
            database.audit("lookup", row.get("owner_id") or "", row["id"], True)
            return {
                "id": row["id"],
                "patient_name": row.get("patient_name", ""),
                "department": row.get("department", ""),
                "date": row.get("date", ""),
                "time": row.get("time", ""),
                "label": speak_time(row["time"]),
            }
    database.audit("lookup", "", cid[:8], False)
    raise ValueError("No visit matches that confirmation number and phone.")


def describe_own_booking(confirmation_id: str, patient_name: str, phone: str, owner_id: str) -> str:
    cid = confirmation_id.strip().lower()
    name = " ".join(patient_name.strip().lower().split())
    digits = _ten_digits(phone)
    if not digits or (not cid and len(name) < 2):
        return (
            "Ask for the 10-digit phone number used at booking and the confirmation number. "
            "Do not list other patients."
        )
    matches = []
    for row in _load(owner_id):
        if row.get("phone") != digits:
            continue
        if cid and str(row.get("id", "")).lower() == cid:
            matches.append(row)
        elif name and name in str(row.get("patient_name", "")).lower():
            matches.append(row)
    if not matches:
        return "No visit matches that phone number. Do not mention any other patient."
    lines = [
        (
            f"{row.get('patient_name')}, {row.get('department')}, "
            f"{row.get('date')} at {speak_time(row['time'])}, confirmation {row.get('id')}"
        )
        for row in matches[:3]
    ]
    return "Only this caller's visit: " + "; ".join(lines) + ". Do not mention any other patient."


def _starts(row: dict) -> datetime:
    return datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M")


def desk(owner_id: str, hospital_name: str) -> dict:
    now = now_local()
    upcoming = []
    earlier = []
    for row in _load(owner_id):
        item = {
            "department": row.get("department", ""),
            "date": row.get("date", ""),
            "time": row.get("time", ""),
            "label": speak_time(row["time"]),
        }
        if _starts(row) >= now:
            upcoming.append(item)
        else:
            earlier.append(item)
    upcoming.sort(key=_starts)
    earlier.sort(key=_starts, reverse=True)
    today = {
        name: [speak_time(slot) for slot in open_slots(name, "today", owner_id)["open_slots"]]
        for name in DEPARTMENTS
    }
    return {
        "hospital": hospital_name or HOSPITAL,
        "clock": now.strftime("%I:%M %p").lstrip("0"),
        "date": now.date().isoformat(),
        "slot_minutes": 30,
        "opens": speak_time(OPEN_AT),
        "closes": speak_time(CLOSE_AT),
        "today": today,
        "upcoming": upcoming,
        "earlier": earlier[:8],
        "days": _week(owner_id),
    }


def _week(owner_id: str) -> list:
    now = now_local()
    booked = {
        (row.get("department"), row.get("date"), row.get("time")): row
        for row in _load(owner_id)
    }
    days = []
    cursor = now.date()
    while len(days) < 6:
        if cursor.weekday() != 6:
            columns = {}
            for name in DEPARTMENTS:
                cells = []
                for slot in DAY_SLOTS:
                    row = booked.get((name, cursor.isoformat(), slot))
                    if row:
                        state = "booked"
                    elif not _still_ahead(cursor, slot, now):
                        state = "passed"
                    else:
                        state = "open"
                    cells.append({
                        "time": slot,
                        "label": speak_time(slot),
                        "state": state,
                    })
                columns[name] = cells
            days.append({
                "date": cursor.isoformat(),
                "weekday": WEEKDAYS[cursor.weekday()],
                "departments": columns,
            })
        cursor += timedelta(days=1)
    return days
