"""
Bulk teacher import.

Same shape as the student import — a spreadsheet in, a background job, a
report of what happened — but keyed on employee code rather than admission
number, and with no class structure to build.

The one real decision is the key. Phone numbers look unique and are not: in
the school's own list, husbands and wives share one, and a teacher appears
twice under the same number with the name spelled two ways. Names repeat too.
So a code is required, and a row without one is refused rather than guessed.
"""
from __future__ import annotations

import datetime as dt
import re

from django.db import transaction
from django.utils import timezone

from .import_models import ImportJob, ImportStatus
from .imports import (
    CHUNK,
    MAX_PROBLEMS,
    TRUE_WORDS,
    ImportError_,
    _clean,
    _clean_phone,
    build_lookup,
    read_rows,
)
from .models import Teacher

COLUMNS = {
    "employee_code": ["employee_code", "employee code", "code", "userid", "user_id",
                      "user id", "employee id", "employee_id", "teacher id", "id"],
    "full_name": ["full_name", "name", "full name", "teacher name"],
    "phone": ["phone", "phone number", "mobile", "phone_number", "contact"],
    "designation": ["designation", "post", "position"],
    "campus": ["campus", "employee type", "employee_type", "branch"],
    "joined_on": ["joined_on", "joining date", "joining_date", "join date", "joined"],
    "device_user_id": ["device_user_id", "device user id", "device_id", "machine_id"],
    "is_active": ["is_active", "active", "status"],
}
REQUIRED = ["employee_code", "full_name"]
LOOKUP = build_lookup(COLUMNS)


def parse_date(value):
    """
    Dates arrive as real dates, as 19/1/2008, 16-07-2017, or 3/5/22.

    Day first, always. The school writes dates the Bangladeshi way, and a file
    with 19/1/2008 and 23/07/2022 in it cannot be month-first, so 9/5/2022 is
    9 May rather than 5 September. Anything unreadable comes back None and is
    reported, not guessed.
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _clean(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?", text)
    if match:
        year, month, day = (int(x) for x in match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", text)
        if not match:
            raise ValueError(text)
        day, month, year = (int(x) for x in match.groups())
        if year < 100:
            year += 2000 if year <= timezone.localdate().year % 100 else 1900
    return dt.date(year, month, day)


def run_teacher_job(job: ImportJob, allow_logins=True):
    job.status = ImportStatus.RUNNING
    job.started_at = timezone.now()
    job.processed = job.created = job.updated = job.skipped = job.deactivated = 0
    job.problems = []
    job.error = ""
    job.save()

    problems = []

    def note(line, message):
        if len(problems) < MAX_PROBLEMS:
            problems.append(f"Row {line}: {message}")

    try:
        job.upload.open("rb")
        rows = list(read_rows(job.upload, job.original_name or job.upload.name,
                              lookup=LOOKUP, required=REQUIRED, sheet="Teachers"))
        job.upload.close()
    except ImportError_ as exc:
        return _fail(job, str(exc))
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        if "zip file" in detail.lower():
            detail = ("That file is not a real Excel workbook. Open it and use File, "
                      "Save as, and pick Excel Workbook (.xlsx) or CSV.")
        else:
            detail = f"Could not read that file: {detail}"
        return _fail(job, detail)

    job.total_rows = len(rows)
    job.save(update_fields=["total_rows"])

    parsed, seen = [], set()
    for line, raw in rows:
        code = _clean(raw.get("employee_code"))
        name = _clean(raw.get("full_name"))
        if not code:
            note(line, f"{name or 'a row'} has no employee code — give every teacher one")
            job.skipped += 1
            continue
        if len(code) > 30:
            note(line, "this looks like a heading or a note, not a teacher — skipped")
            job.skipped += 1
            continue
        if not name:
            note(line, f"{code} has no name")
            job.skipped += 1
            continue
        if code.lower() in seen:
            note(line, f"{code} appears more than once in this file")
            job.skipped += 1
            continue
        seen.add(code.lower())

        try:
            joined = parse_date(raw.get("joined_on"))
        except (ValueError, TypeError):
            note(line, f"{code}: could not read the joining date "
                       f"'{_clean(raw.get('joined_on'))}' — left blank")
            joined = None

        active_raw = _clean(raw.get("is_active"))
        parsed.append({
            "employee_code": code,
            "full_name": name[:120],
            "phone": _clean_phone(raw.get("phone")),
            "designation": _clean(raw.get("designation"))[:80],
            "campus": _clean(raw.get("campus"))[:60],
            "joined_on": joined,
            "device_user_id": _clean(raw.get("device_user_id"))[:32],
            "is_active": True if not active_raw else active_raw.lower() in TRUE_WORDS,
        })

    # Codes are matched without regard to case, so T-001 and t-001 are one
    # person. A school has hundreds of teachers, not thousands, so one query
    # for all of them is cheaper than being clever.
    existing = {t.employee_code.lower(): t for t in Teacher.objects.all()}
    codes = [row["employee_code"] for row in parsed]

    if job.dry_run:
        job.created = sum(1 for c in codes if c.lower() not in existing)
        job.updated = len(codes) - job.created
        return _finish(job, problems)

    fields = ["full_name", "phone", "designation", "campus", "joined_on",
              "device_user_id", "is_active"]
    to_create, to_update = [], []
    for row in parsed:
        teacher = existing.get(row["employee_code"].lower())
        if teacher is None:
            to_create.append(Teacher(**row))
        else:
            for field in fields:
                value = row[field]
                # A blank cell never wipes what is already on record: the
                # device id in particular is set on the terminal screen, not in
                # the spreadsheet, and must survive a re-upload.
                if value in ("", None) and field in ("device_user_id", "joined_on", "phone"):
                    continue
                setattr(teacher, field, value)
            to_update.append(teacher)

    with transaction.atomic():
        Teacher.objects.bulk_create(to_create, batch_size=CHUNK, ignore_conflicts=True)
        Teacher.objects.bulk_update(to_update, fields, batch_size=CHUNK)
    job.created = len(to_create)
    job.updated = len(to_update)

    if job.create_logins and allow_logins:
        _create_logins([row["employee_code"] for row in parsed])
    elif job.create_logins:
        problems.insert(0, "Logins were not created — that takes a few minutes and cannot run "
                           "inside this page. On the server: "
                           "python manage.py create_logins --teachers")

    if job.deactivate_missing and seen:
        stale = (Teacher.objects.filter(is_active=True)
                 .exclude(employee_code__in=codes))
        stale = [t.pk for t in stale if t.employee_code.lower() not in seen]
        job.deactivated = Teacher.objects.filter(pk__in=stale).update(is_active=False)

    return _finish(job, problems)


def _create_logins(codes):
    from .logins import create_teacher_logins
    create_teacher_logins(Teacher.objects.filter(employee_code__in=codes))


def _fail(job, message):
    job.status = ImportStatus.FAILED
    job.error = message
    job.finished_at = timezone.now()
    job.save()
    return job


def _finish(job, problems):
    job.processed = job.total_rows
    job.problems = problems
    job.status = ImportStatus.DONE
    job.finished_at = timezone.now()
    job.save()
    return job
