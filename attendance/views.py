import datetime as dt

from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from academics.models import SchoolClass, Section, Teacher
from accounts.decorators import admin_required, staff_required
from common import ColourInput, StyledFormMixin, TimeInput, WeekdayField, csv_response, paginate

from .models import (
    AttendanceRecord,
    AttendanceStatus,
    SlotSection,
    SlotTeacher,
    SlotType,
    TeacherAttendanceRecord,
    TimeSlot,
)
from .services import close_slots, process_pending, reprocess_day


class TimeSlotForm(StyledFormMixin, forms.ModelForm):
    days = WeekdayField()

    class Meta:
        model = TimeSlot
        fields = ["name", "slot_type", "start_time", "late_time", "end_time",
                  "out_start_time", "out_end_time", "days", "colour", "order",
                  "note", "is_active"]
        widgets = {
            "start_time": TimeInput(), "late_time": TimeInput(), "end_time": TimeInput(),
            "out_start_time": TimeInput(), "out_end_time": TimeInput(),
            "colour": ColourInput(),
        }


class ManualRecordForm(StyledFormMixin, forms.Form):
    status = forms.ChoiceField(choices=AttendanceStatus.choices)
    note = forms.CharField(required=False, max_length=200)


# ------------------------------------------------------------------ slots ---

@admin_required
def slot_list(request):
    slot_type = request.GET.get("type", SlotType.STUDENT)
    slots = (TimeSlot.objects.filter(slot_type=slot_type)
             .annotate(n_sections=Count("sections", distinct=True),
                       n_teachers=Count("teachers", distinct=True)))
    return render(request, "attendance/slot_list.html",
                  {"slots": slots, "slot_type": slot_type,
                   "is_teacher_board": slot_type == SlotType.TEACHER})


@admin_required
def slot_form(request, pk=None):
    instance = get_object_or_404(TimeSlot, pk=pk) if pk else None
    initial = {}
    if not instance:
        initial["slot_type"] = request.GET.get("type", SlotType.STUDENT)
    form = TimeSlotForm(request.POST or None, instance=instance, initial=initial)
    if request.method == "POST" and form.is_valid():
        slot = form.save()
        messages.success(request, f"Slot “{slot.name}” saved.")
        return redirect(f"{request.path.rsplit('/slots/', 1)[0]}/board/?type={slot.slot_type}"
                        if False else f"/attendance/board/?type={slot.slot_type}")
    return render(request, "attendance/slot_form.html",
                  {"form": form, "slot": instance,
                   "title": "Edit time slot" if instance else "New time slot"})


@admin_required
def slot_delete(request, pk):
    slot = get_object_or_404(TimeSlot, pk=pk)
    slot_type = slot.slot_type
    slot.delete()
    messages.success(request, "Slot deleted. Existing attendance records went with it.")
    return redirect(f"/attendance/board/?type={slot_type}")


# ------------------------------------------------------------------ board ---

@admin_required
def board(request):
    """
    The drag-and-drop timetable.

    Left: every class-section (or teacher). Right: the slot boxes. Drop a card
    into a box and that group is on that schedule from the next punch onward.
    """
    slot_type = request.GET.get("type", SlotType.STUDENT)
    is_teacher = slot_type == SlotType.TEACHER

    slots = (TimeSlot.objects.filter(slot_type=slot_type)
             .prefetch_related("sections__section__school_class", "teachers__teacher"))

    if is_teacher:
        pool = list(Teacher.objects.filter(is_active=True))
        assigned = set(SlotTeacher.objects.values_list("teacher_id", flat=True))
    else:
        pool = list(Section.objects.select_related("school_class"))
        assigned = set(SlotSection.objects.values_list("section_id", flat=True))

    return render(request, "attendance/board.html", {
        "slots": slots,
        "pool": pool,
        "assigned": assigned,
        "slot_type": slot_type,
        "is_teacher": is_teacher,
        "classes": SchoolClass.objects.prefetch_related("sections"),
    })


@admin_required
def board_assign(request):
    """Called by the board when a card is dropped, or an x is clicked."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)

    slot = get_object_or_404(TimeSlot, pk=request.POST.get("slot"))
    action = request.POST.get("action", "add")
    kind = request.POST.get("kind", "section")
    target_id = request.POST.get("target")

    if kind == "teacher":
        model, field = SlotTeacher, "teacher_id"
    else:
        model, field = SlotSection, "section_id"

    if action == "add":
        model.objects.get_or_create(time_slot=slot, **{field: target_id})
        label = "added"
    else:
        model.objects.filter(time_slot=slot, **{field: target_id}).delete()
        label = "removed"

    count = slot.teachers.count() if kind == "teacher" else slot.sections.count()
    return JsonResponse({"ok": True, "action": label, "count": count})


@admin_required
def board_assign_class(request):
    """Dropping a whole class puts every one of its sections in the box."""
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    slot = get_object_or_404(TimeSlot, pk=request.POST.get("slot"))
    school_class = get_object_or_404(SchoolClass, pk=request.POST.get("target"))
    added = 0
    for section in school_class.sections.all():
        _obj, created = SlotSection.objects.get_or_create(time_slot=slot, section=section)
        added += created
    return JsonResponse({"ok": True, "added": added, "count": slot.sections.count()})


# --------------------------------------------------------------- register ---

def _visible_sections(user):
    allowed = user.allowed_section_ids()
    queryset = Section.objects.select_related("school_class")
    return queryset if allowed is None else queryset.filter(id__in=allowed)


@staff_required
def register(request):
    """Day view: one class, one slot, every student, editable."""
    day_text = request.GET.get("date") or timezone.localdate().isoformat()
    try:
        day = dt.date.fromisoformat(day_text)
    except ValueError:
        day = timezone.localdate()

    sections = _visible_sections(request.user)
    section_id = request.GET.get("section") or (sections.first().pk if sections else None)
    slot_id = request.GET.get("slot")

    records = (AttendanceRecord.objects
               .filter(date=day)
               .select_related("student", "student__section", "time_slot", "device"))
    if section_id:
        records = records.filter(student__section_id=section_id)
    if slot_id:
        records = records.filter(time_slot_id=slot_id)
    if request.user.allowed_section_ids() is not None:
        records = records.filter(student__section_id__in=request.user.allowed_section_ids())

    slots = TimeSlot.objects.filter(slot_type=SlotType.STUDENT)
    if section_id:
        slots = slots.filter(sections__section_id=section_id).distinct()

    if request.GET.get("export"):
        return csv_response(
            f"register-{day}.csv",
            ["Roll", "Name", "Class", "Slot", "Status", "In", "Out", "Late (min)"],
            [[r.student.roll_no, r.student.full_name, str(r.student.section),
              r.time_slot.name, r.get_status_display(),
              timezone.localtime(r.in_time).strftime("%H:%M") if r.in_time else "",
              timezone.localtime(r.out_time).strftime("%H:%M") if r.out_time else "",
              r.minutes_late] for r in records])

    summary = records.values("status").annotate(n=Count("id"))
    # Count on the whole filtered set, then page — the totals must describe the
    # class, not whichever fifty rows happen to be on screen.
    return render(request, "attendance/register.html", {
        "day": day,
        "page": paginate(request, records),
        "total": records.count(),
        "sections": sections,
        "section_id": str(section_id or ""),
        "slots": slots,
        "slot_id": slot_id,
        "summary": {row["status"]: row["n"] for row in summary},
        "statuses": AttendanceStatus.choices,
    })


@staff_required
def record_update(request, pk):
    record = get_object_or_404(AttendanceRecord.objects.select_related("student"), pk=pk)
    allowed = request.user.allowed_section_ids()
    if allowed is not None and record.student.section_id not in allowed:
        messages.error(request, "That student is not in one of your classes.")
        return redirect("attendance:register")

    status = request.POST.get("status")
    if status in dict(AttendanceStatus.choices):
        record.status = status
        record.is_manual = True
        record.edited_by = request.user
        record.note = request.POST.get("note", "")[:200]
        record.save()
        messages.success(request, f"{record.student.full_name} set to {record.get_status_display()}.")
    return redirect(request.META.get("HTTP_REFERER", "/attendance/register/"))


@staff_required
def teacher_register(request):
    day_text = request.GET.get("date") or timezone.localdate().isoformat()
    try:
        day = dt.date.fromisoformat(day_text)
    except ValueError:
        day = timezone.localdate()
    records = (TeacherAttendanceRecord.objects.filter(date=day)
               .select_related("teacher", "time_slot", "device"))
    if not request.user.is_admin_level:
        teacher = request.user.teacher_profile
        records = records.filter(teacher=teacher) if teacher else records.none()

    summary = records.values("status").annotate(n=Count("id"))
    return render(request, "attendance/teacher_register.html", {
        "day": day, "page": paginate(request, records), "total": records.count(),
        "summary": {row["status"]: row["n"] for row in summary},
        "statuses": AttendanceStatus.choices,
    })


@admin_required
def teacher_record_update(request, pk):
    record = get_object_or_404(TeacherAttendanceRecord, pk=pk)
    status = request.POST.get("status")
    if status in dict(AttendanceStatus.choices):
        record.status = status
        record.is_manual = True
        record.edited_by = request.user
        record.save()
    return redirect(request.META.get("HTTP_REFERER", "/attendance/teachers/"))


# ------------------------------------------------------------ maintenance ---

@admin_required
def maintenance(request):
    result = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "process_pending":
            result = f"{process_pending()} punch(es) resolved."
        elif action == "close_slots":
            day_text = request.POST.get("date") or timezone.localdate().isoformat()
            counts = close_slots(dt.date.fromisoformat(day_text))
            result = (f"{counts['students']} student and {counts['teachers']} teacher "
                      f"absences filled in for {day_text}.")
        elif action == "reprocess":
            day_text = request.POST.get("date") or timezone.localdate().isoformat()
            result = f"{reprocess_day(dt.date.fromisoformat(day_text))} punch(es) replayed."
        messages.success(request, result)
    from devices.models import Punch
    return render(request, "attendance/maintenance.html", {
        "today": timezone.localdate().isoformat(),
        "pending": Punch.objects.filter(processed=False).count(),
        "unmatched": Punch.objects.filter(
            process_note__icontains="No student or teacher").count(),
        "result": result,
    })
