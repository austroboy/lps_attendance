import calendar
import datetime as dt

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from academics.models import Section, Student, Teacher
from accounts.decorators import login_required_any, staff_required
from attendance.models import AttendanceRecord, AttendanceStatus, TeacherAttendanceRecord, TimeSlot
from common import csv_response, paginate


def _parse_date(text, fallback=None):
    try:
        return dt.date.fromisoformat(text)
    except (TypeError, ValueError):
        return fallback or timezone.localdate()


def _status_counts(queryset):
    rows = queryset.values("status").annotate(n=Count("id"))
    counts = {key: 0 for key, _label in AttendanceStatus.choices}
    for row in rows:
        counts[row["status"]] = row["n"]
    counts["total"] = sum(counts.values())
    present = counts["PRESENT"] + counts["LATE"]
    counts["present_total"] = present
    counts["rate"] = round(100 * present / counts["total"], 1) if counts["total"] else 0
    return counts


@staff_required
def daily(request):
    """One day, every class, at a glance."""
    day = _parse_date(request.GET.get("date"))
    records = AttendanceRecord.objects.filter(date=day).select_related(
        "student", "student__section", "student__section__school_class")
    allowed = request.user.allowed_section_ids()
    if allowed is not None:
        records = records.filter(student__section_id__in=allowed)

    by_section = {}
    for record in records:
        key = record.student.section
        bucket = by_section.setdefault(key, {"PRESENT": 0, "LATE": 0, "ABSENT": 0,
                                             "LEAVE": 0, "HOLIDAY": 0, "total": 0})
        bucket[record.status] = bucket.get(record.status, 0) + 1
        bucket["total"] += 1
    for bucket in by_section.values():
        present = bucket["PRESENT"] + bucket["LATE"]
        bucket["rate"] = round(100 * present / bucket["total"], 1) if bucket["total"] else 0

    if request.GET.get("export"):
        return csv_response(
            f"daily-{day}.csv",
            ["Class", "Section", "Present", "Late", "Absent", "Leave", "Total", "Rate %"],
            [[s.school_class.name, s.name, b["PRESENT"], b["LATE"], b["ABSENT"],
              b["LEAVE"], b["total"], b["rate"]] for s, b in by_section.items()])

    return render(request, "reports/daily.html", {
        "day": day,
        "rows": sorted(by_section.items(), key=lambda kv: (kv[0].school_class.order, kv[0].name)),
        "totals": _status_counts(records),
    })


@staff_required
def monthly(request):
    """A month of one section, one row per student."""
    today = timezone.localdate()
    year = int(request.GET.get("year") or today.year)
    month = int(request.GET.get("month") or today.month)
    section_id = request.GET.get("section")

    sections = Section.objects.select_related("school_class")
    allowed = request.user.allowed_section_ids()
    if allowed is not None:
        sections = sections.filter(id__in=allowed)
    if not section_id and sections:
        section_id = sections.first().pk

    days_in_month = calendar.monthrange(year, month)[1]
    first = dt.date(year, month, 1)
    last = dt.date(year, month, days_in_month)

    students = Student.objects.filter(section_id=section_id, is_active=True)
    records = (AttendanceRecord.objects
               .filter(date__range=(first, last), student__in=students)
               .values("student_id", "date", "status"))

    grid = {}
    for row in records:
        # One cell per day; late beats present, absent beats both, so the grid
        # shows the worst outcome of the day rather than the last one written.
        rank = {"PRESENT": 1, "LATE": 2, "LEAVE": 3, "ABSENT": 4, "HOLIDAY": 0}
        cell = grid.setdefault(row["student_id"], {})
        current = cell.get(row["date"].day)
        if current is None or rank.get(row["status"], 0) > rank.get(current, 0):
            cell[row["date"].day] = row["status"]

    table = []
    for student in students:
        cells = grid.get(student.pk, {})
        present = sum(1 for v in cells.values() if v in ("PRESENT", "LATE"))
        absent = sum(1 for v in cells.values() if v == "ABSENT")
        late = sum(1 for v in cells.values() if v == "LATE")
        marked = present + absent
        table.append({
            "student": student,
            "cells": [cells.get(d) for d in range(1, days_in_month + 1)],
            "present": present, "absent": absent, "late": late,
            "rate": round(100 * present / marked, 1) if marked else 0,
        })

    if request.GET.get("export"):
        header = (["Roll", "Name"] + [str(d) for d in range(1, days_in_month + 1)]
                  + ["Present", "Absent", "Late", "Rate %"])
        rows = [[r["student"].roll_no, r["student"].full_name]
                + [(c or "")[:1] for c in r["cells"]]
                + [r["present"], r["absent"], r["late"], r["rate"]] for r in table]
        return csv_response(f"monthly-{year}-{month:02d}.csv", header, rows)

    return render(request, "reports/monthly.html", {
        "page": paginate(request, table, per_page=50),
        "total": len(table),
        "days": list(range(1, days_in_month + 1)),
        "year": year, "month": month, "month_name": calendar.month_name[month],
        "sections": sections, "section_id": str(section_id or ""),
        "years": range(today.year - 3, today.year + 2),
        "months": [(i, calendar.month_name[i]) for i in range(1, 13)],
    })


@staff_required
def defaulters(request):
    """Students below an attendance threshold over a date range."""
    end = _parse_date(request.GET.get("to"))
    start = _parse_date(request.GET.get("from"), end - dt.timedelta(days=29))
    threshold = int(request.GET.get("threshold") or 80)

    records = AttendanceRecord.objects.filter(date__range=(start, end))
    allowed = request.user.allowed_section_ids()
    if allowed is not None:
        records = records.filter(student__section_id__in=allowed)

    aggregated = (records.values("student_id")
                  .annotate(total=Count("id"),
                            present=Count("id", filter=Q(status__in=["PRESENT", "LATE"])),
                            absent=Count("id", filter=Q(status="ABSENT"))))
    students = {s.pk: s for s in Student.objects.filter(
        pk__in=[row["student_id"] for row in aggregated]).select_related(
        "section", "section__school_class")}

    rows = []
    for row in aggregated:
        if not row["total"]:
            continue
        rate = round(100 * row["present"] / row["total"], 1)
        if rate < threshold:
            rows.append({"student": students.get(row["student_id"]), "rate": rate,
                         "present": row["present"], "absent": row["absent"],
                         "total": row["total"]})
    rows.sort(key=lambda r: r["rate"])

    if request.GET.get("export"):
        return csv_response(
            "low-attendance.csv",
            ["Roll", "Name", "Class", "Present", "Absent", "Total", "Rate %"],
            [[r["student"].roll_no, r["student"].full_name, str(r["student"].section),
              r["present"], r["absent"], r["total"], r["rate"]] for r in rows])

    return render(request, "reports/defaulters.html",
                  {"page": paginate(request, rows), "total": len(rows),
                   "start": start, "end": end, "threshold": threshold})


@staff_required
def teacher_report(request):
    end = _parse_date(request.GET.get("to"))
    start = _parse_date(request.GET.get("from"), end - dt.timedelta(days=29))
    records = TeacherAttendanceRecord.objects.filter(date__range=(start, end))

    aggregated = (records.values("teacher_id")
                  .annotate(total=Count("id"),
                            present=Count("id", filter=Q(status="PRESENT")),
                            late=Count("id", filter=Q(status="LATE")),
                            absent=Count("id", filter=Q(status="ABSENT"))))
    teachers = {t.pk: t for t in Teacher.objects.filter(
        pk__in=[row["teacher_id"] for row in aggregated])}
    rows = [{"teacher": teachers.get(row["teacher_id"]), **row} for row in aggregated]
    rows.sort(key=lambda r: r["teacher"].full_name if r["teacher"] else "")

    if request.GET.get("export"):
        return csv_response(
            "teacher-attendance.csv",
            ["Code", "Name", "Present", "Late", "Absent", "Total"],
            [[r["teacher"].employee_code, r["teacher"].full_name, r["present"],
              r["late"], r["absent"], r["total"]] for r in rows if r["teacher"]])

    return render(request, "reports/teacher.html",
                  {"rows": rows, "start": start, "end": end})


@staff_required
def device_report(request):
    from devices.models import Device, Punch
    end = _parse_date(request.GET.get("to"))
    start = _parse_date(request.GET.get("from"), end - dt.timedelta(days=6))
    punches = Punch.objects.filter(punch_time__date__range=(start, end))
    rows = (punches.values("device__name", "device__serial_number")
            .annotate(n=Count("id"),
                      unmatched=Count("id", filter=Q(process_note__icontains="No student")))
            .order_by("-n"))
    return render(request, "reports/devices.html", {
        "rows": rows, "start": start, "end": end,
        "devices": Device.objects.all(),
        "silent": [d for d in Device.objects.filter(is_active=True) if not d.is_online],
    })


@login_required_any
def my_attendance(request):
    """The student's own view. Nothing else is reachable from this screen."""
    student = request.user.student_profile
    if student is None and request.user.is_admin_level and request.GET.get("student"):
        student = get_object_or_404(Student, pk=request.GET["student"])
    if student is None:
        return render(request, "reports/my_attendance.html", {"student": None})

    today = timezone.localdate()
    year = int(request.GET.get("year") or today.year)
    month = int(request.GET.get("month") or today.month)
    days_in_month = calendar.monthrange(year, month)[1]
    first, last = dt.date(year, month, 1), dt.date(year, month, days_in_month)

    records = (AttendanceRecord.objects
               .filter(student=student, date__range=(first, last))
               .select_related("time_slot").order_by("date", "time_slot__start_time"))

    return render(request, "reports/my_attendance.html", {
        "student": student,
        "records": records,
        "counts": _status_counts(records),
        "year": year, "month": month, "month_name": calendar.month_name[month],
        "years": range(today.year - 3, today.year + 1),
        "months": [(i, calendar.month_name[i]) for i in range(1, 13)],
    })
