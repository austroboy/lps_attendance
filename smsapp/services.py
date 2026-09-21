"""Building and sending the messages a schedule asks for."""
from __future__ import annotations

import datetime as dt
import logging

from django.db import IntegrityError
from django.utils import timezone

from academics.models import Student
from attendance.models import AttendanceRecord, TeacherAttendanceRecord

from .gateway import SmsError, SslWirelessClient, chunked, new_csms_id, normalise_msisdn
from .models import Audience, SmsLog, SmsRun, SmsSchedule, SmsStatus

log = logging.getLogger(__name__)

SCHOOL_NAME = "LPS School"


def _context(record, person, slot_name):
    section = getattr(person, "section", None)
    return {
        "name": getattr(person, "full_name", ""),
        "roll": getattr(person, "roll_no", "") or getattr(person, "employee_code", ""),
        "class": section.school_class.name if section else "",
        "section": section.name if section else "",
        "date": record.date.strftime("%d-%m-%Y"),
        "time": timezone.localtime(record.in_time).strftime("%H:%M") if record.in_time else "-",
        "status": record.get_status_display(),
        "slot": slot_name,
        "guardian": getattr(person, "guardian_name", ""),
        "school": SCHOOL_NAME,
    }


def build_messages(schedule: SmsSchedule, day: dt.date):
    """Work out exactly who gets what. Returns a list of (person, msisdn, text)."""
    out = []
    statuses = [s for s in (schedule.statuses or [])]

    if schedule.audience == Audience.TEACHER:
        teacher_ids = list(schedule.teacher_targets.values_list("teacher_id", flat=True))
        records = TeacherAttendanceRecord.objects.filter(date=day, teacher_id__in=teacher_ids)
        if schedule.time_slot_id:
            records = records.filter(time_slot_id=schedule.time_slot_id)
        if statuses:
            records = records.filter(status__in=statuses)
        for record in records.select_related("teacher", "time_slot"):
            number = normalise_msisdn(record.teacher.phone)
            if not number:
                continue
            text = schedule.template.render(_context(record, record.teacher, record.time_slot.name))
            out.append((record.teacher, number, text))
        return out

    section_ids = list(schedule.sections.values_list("section_id", flat=True))
    records = AttendanceRecord.objects.filter(date=day, student__section_id__in=section_ids)
    if schedule.time_slot_id:
        records = records.filter(time_slot_id=schedule.time_slot_id)
    if statuses:
        records = records.filter(status__in=statuses)

    seen = set()
    for record in records.select_related(
            "student", "student__section", "student__section__school_class", "time_slot"):
        student = record.student
        if student.pk in seen:
            continue  # one message per student per run, even across slots
        raw = student.guardian_phone if schedule.audience == Audience.GUARDIAN else student.student_phone
        number = normalise_msisdn(raw or student.sms_number)
        if not number:
            continue
        seen.add(student.pk)
        out.append((student, number, schedule.template.render(
            _context(record, student, record.time_slot.name))))
    return out


def run_schedule(schedule: SmsSchedule, day: dt.date | None = None,
                 dry_run=False, force=False) -> SmsRun:
    """
    Send one schedule for one day.

    A SmsRun row per (schedule, date) is the guard against double-sending, so a
    crashed cron job that runs again ten minutes later will not spam parents.
    """
    day = day or timezone.localdate()

    if not force:
        existing = SmsRun.objects.filter(schedule=schedule, for_date=day).first()
        if existing:
            log.info("schedule %s already ran for %s", schedule, day)
            return existing

    messages = build_messages(schedule, day)
    run = SmsRun(schedule=schedule, for_date=day, total=len(messages))

    if dry_run:
        run.skipped = len(messages)
        run.detail = "\n".join(f"{n}: {t}" for _p, n, t in messages[:50])
        return run

    client = SslWirelessClient()
    sent = failed = 0
    errors = []

    for batch in chunked(messages, 100):
        payload, logs = [], []
        for person, number, text in batch:
            csms_id = new_csms_id()
            entry = SmsLog(
                msisdn=number, body=text, csms_id=csms_id, schedule=schedule, for_date=day,
                status=SmsStatus.QUEUED,
            )
            if hasattr(person, "section"):
                entry.student = person
            else:
                entry.teacher = person
            logs.append(entry)
            payload.append({"msisdn": number, "text": text, "csms_id": csms_id})

        SmsLog.objects.bulk_create(logs, ignore_conflicts=True)

        try:
            response = client.send_dynamic(payload)
        except SmsError as exc:
            failed += len(payload)
            errors.append(str(exc))
            SmsLog.objects.filter(csms_id__in=[m["csms_id"] for m in payload]).update(
                status=SmsStatus.FAILED, error=str(exc)[:250])
            continue

        sent += len(payload)
        info = {i.get("csms_id"): i for i in (response.get("smsinfo") or [])}
        now = timezone.now()
        for message in payload:
            detail = info.get(message["csms_id"], {})
            SmsLog.objects.filter(csms_id=message["csms_id"]).update(
                status=SmsStatus.SENT,
                sent_at=now,
                reference_id=str(detail.get("reference_id", ""))[:64],
                response=detail or response,
            )

    run.sent = sent
    run.failed = failed
    run.detail = "\n".join(errors)[:4000]
    try:
        run.save()
    except IntegrityError:
        run = SmsRun.objects.get(schedule=schedule, for_date=day)
    return run


def send_free_text(numbers, text, schedule=None):
    """Ad-hoc send from the Compose screen."""
    client = SslWirelessClient()
    cleaned = [n for n in (normalise_msisdn(x) for x in numbers) if n]
    results = {"sent": 0, "failed": 0, "invalid": len(numbers) - len(cleaned), "errors": []}
    for batch in chunked(cleaned, 100):
        csms_id = new_csms_id()
        logs = [SmsLog(msisdn=n, body=text, csms_id=new_csms_id(), schedule=schedule,
                       status=SmsStatus.QUEUED) for n in batch]
        SmsLog.objects.bulk_create(logs, ignore_conflicts=True)
        try:
            client.send_bulk(batch, text, csms_id=csms_id)
        except SmsError as exc:
            results["failed"] += len(batch)
            results["errors"].append(str(exc))
            SmsLog.objects.filter(csms_id__in=[entry.csms_id for entry in logs]).update(
                status=SmsStatus.FAILED, error=str(exc)[:250])
            continue
        results["sent"] += len(batch)
        SmsLog.objects.filter(csms_id__in=[entry.csms_id for entry in logs]).update(
            status=SmsStatus.SENT, sent_at=timezone.now())
    return results


def due_schedules(now=None):
    """Schedules whose send time has passed today and that have not run yet."""
    now = now or timezone.localtime()
    day = now.date()
    weekday = day.weekday()
    already = set(SmsRun.objects.filter(for_date=day).values_list("schedule_id", flat=True))
    for schedule in SmsSchedule.objects.filter(is_active=True).select_related("template"):
        if schedule.pk in already or not schedule.runs_on(weekday):
            continue
        if schedule.send_time <= now.time():
            yield schedule
