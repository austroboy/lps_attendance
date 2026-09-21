"""
Turning raw punches into attendance.

A punch knows nothing except "user 42 was verified at 07:58:11 on gate 1". All
of the meaning — which class, which slot, present or late — is applied here, so
you can change a timetable and re-run the engine over history without losing
anything.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone

from academics.models import Holiday, Student, Teacher
from devices.models import DevicePurpose, Punch

from .models import (
    AttendanceRecord,
    AttendanceStatus,
    SlotType,
    TeacherAttendanceRecord,
    TimeSlot,
)

log = logging.getLogger(__name__)


def _local(dt_value):
    return timezone.localtime(dt_value)


def _minutes_between(later: dt.time, earlier: dt.time) -> int:
    a = later.hour * 60 + later.minute
    b = earlier.hour * 60 + earlier.minute
    return max(0, a - b)


def slots_for_student(student, weekday, slot_type=SlotType.STUDENT):
    return (TimeSlot.objects
            .filter(is_active=True, slot_type=slot_type, sections__section_id=student.section_id)
            .distinct()
            .order_by("start_time"))


def slots_for_teacher(teacher, weekday):
    return (TimeSlot.objects
            .filter(is_active=True, slot_type=SlotType.TEACHER, teachers__teacher_id=teacher.pk)
            .distinct()
            .order_by("start_time"))


def _pick_slot(slots, moment: dt.time, weekday: int):
    """
    Choose the slot a punch belongs to.

    In-window wins. If nothing is open we fall back to the slot that closed most
    recently — that is how a check-out punch gets attached to the right session.
    """
    open_now, closed_before = [], []
    for slot in slots:
        if not slot.runs_on(weekday):
            continue
        if slot.start_time <= moment <= slot.end_time:
            open_now.append(slot)
        elif slot.out_start_time and slot.out_end_time and \
                slot.out_start_time <= moment <= slot.out_end_time:
            return slot, "out"
        elif slot.end_time < moment:
            closed_before.append(slot)
    if open_now:
        return open_now[0], "in"
    if closed_before:
        return max(closed_before, key=lambda s: s.end_time), "out"
    return None, None


@transaction.atomic
def process_punch(punch: Punch) -> str:
    """Resolve one punch. Returns a short note describing what happened."""
    if punch.processed:
        return punch.process_note

    moment = _local(punch.punch_time)
    day = moment.date()
    clock = moment.time()
    weekday = day.weekday()

    purpose = punch.device.purpose
    note = ""

    person = None
    kind = None
    if purpose in (DevicePurpose.STUDENT, DevicePurpose.BOTH):
        person = Student.objects.filter(
            device_user_id=punch.device_user_id, is_active=True).select_related("section").first()
        kind = "student" if person else None
    if person is None and purpose in (DevicePurpose.TEACHER, DevicePurpose.BOTH):
        person = Teacher.objects.filter(
            device_user_id=punch.device_user_id, is_active=True).first()
        kind = "teacher" if person else None

    if person is None:
        note = f"No student or teacher is mapped to device user id {punch.device_user_id}"
        Punch.objects.filter(pk=punch.pk).update(processed=True, process_note=note[:200])
        return note

    holiday = Holiday.objects.filter(date=day).first()
    if holiday and (kind == "student" or holiday.applies_to_teachers):
        note = f"{day} is a holiday ({holiday.name}) — punch stored, no record created"
        Punch.objects.filter(pk=punch.pk).update(processed=True, process_note=note[:200])
        return note

    if kind == "student" and "FACE" in str(punch.verify_mode).upper():
        # Belt and braces. Some firmware never sends realtime_enroll_data, but
        # if the terminal just recognised this student by face then the face is
        # certainly enrolled — so the roster can tick without us being told.
        try:
            # Its own savepoint. On PostgreSQL a failed statement poisons the
            # whole surrounding transaction even when the exception is caught,
            # so without this a hiccup here would cost the attendance record
            # being written around it. SQLite never showed that.
            with transaction.atomic():
                person.mark_face_enrolled(device=punch.device, detail="verified by face")
        except Exception:  # pragma: no cover
            log.exception("could not mark face from punch %s", punch.pk)

    if kind == "student":
        slots = list(slots_for_student(person, weekday))
    else:
        slots = list(slots_for_teacher(person, weekday))

    if not slots:
        note = "No time slot covers this person today"
        Punch.objects.filter(pk=punch.pk).update(processed=True, process_note=note[:200])
        return note

    today_slots = [s for s in slots if s.runs_on(weekday)]
    if not today_slots:
        weekday_name = day.strftime("%A")
        note = f"No time slot for this person runs on {weekday_name}"
        Punch.objects.filter(pk=punch.pk).update(processed=True, process_note=note[:200])
        return note

    slot, direction = _pick_slot(today_slots, clock, weekday)
    if slot is None:
        note = f"{clock:%H:%M} falls outside every slot window today"
        Punch.objects.filter(pk=punch.pk).update(processed=True, process_note=note[:200])
        return note

    model = AttendanceRecord if kind == "student" else TeacherAttendanceRecord
    lookup = {"date": day, "time_slot": slot,
              ("student" if kind == "student" else "teacher"): person}

    record = model.objects.filter(**lookup).first()
    if record is None:
        status = AttendanceStatus.PRESENT if clock < slot.late_time else AttendanceStatus.LATE
        record = model.objects.create(
            **lookup,
            status=status,
            in_time=punch.punch_time,
            minutes_late=_minutes_between(clock, slot.late_time) if status == AttendanceStatus.LATE else 0,
            device=punch.device,
        )
        note = f"{slot.name}: marked {record.get_status_display().lower()}"
    else:
        changed = []
        if record.is_manual:
            note = f"{slot.name}: record was set by hand, left alone"
        else:
            if record.in_time is None or punch.punch_time < record.in_time:
                record.in_time = punch.punch_time
                in_clock = _local(record.in_time).time()
                record.status = (AttendanceStatus.PRESENT if in_clock < slot.late_time
                                 else AttendanceStatus.LATE)
                record.minutes_late = (_minutes_between(in_clock, slot.late_time)
                                       if record.status == AttendanceStatus.LATE else 0)
                changed.append("in")
            if direction == "out" or (record.out_time is None or punch.punch_time > record.out_time):
                if record.in_time is None or punch.punch_time > record.in_time:
                    record.out_time = punch.punch_time
                    changed.append("out")
            record.device = punch.device
            record.save()
            note = f"{slot.name}: updated {'/'.join(changed) or 'nothing'}"

    Punch.objects.filter(pk=punch.pk).update(processed=True, process_note=note[:200])
    return note


def process_pending(limit=2000) -> int:
    """Catch-up pass for punches that arrived while rules were incomplete."""
    count = 0
    queryset = Punch.objects.filter(processed=False).select_related("device").order_by("punch_time")
    for punch in queryset[:limit]:
        process_punch(punch)
        count += 1
    return count


def reprocess_day(day: dt.date) -> int:
    """Wipe the automatic records for a day and rebuild them from raw punches."""
    start = timezone.make_aware(dt.datetime.combine(day, dt.time.min))
    end = timezone.make_aware(dt.datetime.combine(day, dt.time.max))
    AttendanceRecord.objects.filter(date=day, is_manual=False).delete()
    TeacherAttendanceRecord.objects.filter(date=day, is_manual=False).delete()
    punches = Punch.objects.filter(punch_time__range=(start, end)).select_related("device")
    punches.update(processed=False, process_note="")
    count = 0
    for punch in punches.order_by("punch_time"):
        punch.refresh_from_db()
        process_punch(punch)
        count += 1
    return count


def close_slots(day: dt.date | None = None, now: dt.time | None = None) -> dict:
    """
    Mark everyone with no punch as absent, for every slot whose window has shut.

    Safe to run repeatedly — it only fills gaps and never touches a record that
    already exists.
    """
    day = day or timezone.localdate()
    now = now or timezone.localtime().time()
    weekday = day.weekday()
    created = {"students": 0, "teachers": 0}

    if Holiday.objects.filter(date=day).exists():
        return created

    for slot in TimeSlot.objects.filter(is_active=True).prefetch_related("sections", "teachers"):
        if not slot.runs_on(weekday) or slot.end_time > now:
            continue

        if slot.slot_type == SlotType.STUDENT:
            section_ids = list(slot.sections.values_list("section_id", flat=True))
            students = Student.objects.filter(section_id__in=section_ids, is_active=True)
            marked = set(AttendanceRecord.objects
                         .filter(date=day, time_slot=slot, student__in=students)
                         .values_list("student_id", flat=True))
            rows = [AttendanceRecord(date=day, time_slot=slot, student=s,
                                     status=AttendanceStatus.ABSENT)
                    for s in students if s.pk not in marked]
            AttendanceRecord.objects.bulk_create(rows, ignore_conflicts=True)
            created["students"] += len(rows)
        else:
            teacher_ids = list(slot.teachers.values_list("teacher_id", flat=True))
            teachers = Teacher.objects.filter(pk__in=teacher_ids, is_active=True)
            marked = set(TeacherAttendanceRecord.objects
                         .filter(date=day, time_slot=slot, teacher__in=teachers)
                         .values_list("teacher_id", flat=True))
            rows = [TeacherAttendanceRecord(date=day, time_slot=slot, teacher=t,
                                            status=AttendanceStatus.ABSENT)
                    for t in teachers if t.pk not in marked]
            TeacherAttendanceRecord.objects.bulk_create(rows, ignore_conflicts=True)
            created["teachers"] += len(rows)

    return created
