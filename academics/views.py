import logging

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.decorators import admin_required, staff_required
from common import csv_response, paginate

from .forms import (
    BulkStudentImportForm,
    ClassAccessForm,
    GroupForm,
    HolidayForm,
    SchoolClassForm,
    SectionForm,
    SessionForm,
    ShiftForm,
    StudentForm,
    TeacherForm,
    VersionForm,
)
from .import_models import ImportJob, ImportStatus
from .roster import (
    can_edit_student,
    cancel_face_enrolment,
    deactivate,
    face_summary,
    reactivate,
    reset_face,
    set_rfid,
    start_face_enrolment,
    student_devices,
)
from .models import (
    AcademicSession,
    ClassAccess,
    FaceStatus,
    Group,
    Holiday,
    LeaveReason,
    SchoolClass,
    Section,
    Shift,
    Student,
    Teacher,
    Version,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- classes ---

@admin_required
def class_list(request):
    classes = (SchoolClass.objects
               .annotate(section_count=Count("sections", distinct=True),
                         student_count=Count("sections__students", distinct=True))
               .select_related("session"))
    return render(request, "academics/class_list.html", {
        "classes": classes,
        "sections": Section.objects.select_related("school_class", "shift", "version", "group"),
        "sessions": AcademicSession.objects.all(),
        "shifts": Shift.objects.all(),
        "versions": Version.objects.all(),
        "groups": Group.objects.all(),
    })


@admin_required
def class_form(request, pk=None):
    instance = get_object_or_404(SchoolClass, pk=pk) if pk else None
    form = SchoolClassForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Class saved.")
        return redirect("academics:class_list")
    return render(request, "academics/simple_form.html",
                  {"form": form, "title": "Edit class" if instance else "Add class",
                   "back": "academics:class_list"})


@admin_required
def section_form(request, pk=None):
    instance = get_object_or_404(Section, pk=pk) if pk else None
    form = SectionForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Section saved.")
        return redirect("academics:class_list")
    return render(request, "academics/simple_form.html",
                  {"form": form, "title": "Edit section" if instance else "Add section",
                   "back": "academics:class_list"})


@admin_required
def session_form(request, pk=None):
    instance = get_object_or_404(AcademicSession, pk=pk) if pk else None
    form = SessionForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Session saved.")
        return redirect("academics:class_list")
    return render(request, "academics/simple_form.html",
                  {"form": form, "title": "Academic session", "back": "academics:class_list"})


def _structure_form(request, form_class, instance, title):
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{title} saved.")
        return redirect("academics:class_list")
    return render(request, "academics/simple_form.html",
                  {"form": form, "title": title, "back": "academics:class_list"})


@admin_required
def shift_form(request, pk=None):
    return _structure_form(request, ShiftForm,
                           get_object_or_404(Shift, pk=pk) if pk else None, "Shift")


@admin_required
def version_form(request, pk=None):
    return _structure_form(request, VersionForm,
                           get_object_or_404(Version, pk=pk) if pk else None, "Version")


@admin_required
def group_form(request, pk=None):
    return _structure_form(request, GroupForm,
                           get_object_or_404(Group, pk=pk) if pk else None, "Group")


# --------------------------------------------------------------- students ---

def visible_students(user):
    queryset = Student.objects.select_related(
        "section", "section__school_class", "section__shift",
        "section__version", "section__group")
    allowed = user.allowed_section_ids()
    if allowed is None:
        return queryset
    return queryset.filter(section_id__in=allowed)


@staff_required
def student_list(request):
    queryset = visible_students(request.user)
    search = request.GET.get("q", "").strip()
    section_id = request.GET.get("section")
    class_id = request.GET.get("school_class")
    shift_id = request.GET.get("shift")
    version_id = request.GET.get("version")
    group_id = request.GET.get("group")
    unmapped = request.GET.get("unmapped")
    inactive = request.GET.get("inactive")
    face = request.GET.get("face")

    if search:
        queryset = queryset.filter(
            Q(full_name__icontains=search) | Q(admission_no__icontains=search)
            | Q(roll_no__icontains=search) | Q(device_user_id__icontains=search)
            | Q(guardian_phone__icontains=search) | Q(guardian_name__icontains=search))
    if section_id:
        queryset = queryset.filter(section_id=section_id)
    if class_id:
        queryset = queryset.filter(section__school_class_id=class_id)
    if shift_id:
        queryset = queryset.filter(section__shift_id=shift_id)
    if version_id:
        queryset = queryset.filter(section__version_id=version_id)
    if group_id:
        queryset = queryset.filter(section__group_id=group_id)
    if unmapped:
        queryset = queryset.filter(device_user_id="")
    if face == "todo":
        queryset = queryset.exclude(face_status=FaceStatus.DONE)
    elif face == "waiting":
        queryset = queryset.filter(face_status=FaceStatus.PENDING)
    elif face == "done":
        queryset = queryset.filter(face_status=FaceStatus.DONE)
    queryset = queryset.filter(is_active=not inactive)

    if request.GET.get("export"):
        return csv_response(
            "students.csv",
            ["Admission", "Roll", "Name", "Class", "Section", "Shift", "Version", "Group",
             "Guardian", "Guardian phone", "Student phone", "Device user id",
             "RFID", "Face", "Active"],
            [[s.admission_no, s.roll_no, s.full_name, s.section.school_class.name,
              s.section.name,
              s.section.shift.name if s.section.shift_id else "",
              s.section.version.name if s.section.version_id else "",
              s.section.group.name if s.section.group_id else "",
              s.guardian_name, s.guardian_phone, s.student_phone, s.device_user_id,
              s.rfid_number, s.get_face_status_display(),
              "yes" if s.is_active else "no"]
             for s in queryset])

    sections = Section.objects.select_related("school_class", "shift", "version", "group")
    allowed = request.user.allowed_section_ids()
    if allowed is not None:
        sections = sections.filter(id__in=allowed)

    return render(request, "academics/student_list.html", {
        "page": paginate(request, queryset),
        "sections": sections,
        "classes": SchoolClass.objects.all(),
        "shifts": Shift.objects.all(),
        "versions": Version.objects.all(),
        "groups": Group.objects.all(),
        "search": search,
        "section_id": section_id,
        "class_id": class_id,
        "shift_id": shift_id,
        "version_id": version_id,
        "group_id": group_id,
        "unmapped": unmapped,
        "inactive": inactive,
        "face": face,
        "total": queryset.count(),
        "summary": face_summary(visible_students(request.user).filter(is_active=True)),
        "devices": student_devices(),
        "leave_reasons": LeaveReason.choices,
    })


@staff_required
def student_form(request, pk=None):
    instance = get_object_or_404(Student, pk=pk) if pk else None
    if instance and not request.user.is_admin_level:
        allowed = request.user.allowed_section_ids() or []
        if instance.section_id not in allowed:
            messages.error(request, "That student is not in one of your classes.")
            return redirect("academics:student_list")
    form = StudentForm(request.POST or None, request.FILES or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Student saved.")
        return redirect("academics:student_list")
    return render(request, "academics/simple_form.html",
                  {"form": form, "title": "Edit student" if instance else "Add student",
                   "back": "academics:student_list"})


# ------------------------------------------------- roster quick actions ---
# Reachable by an admin for any class and by a teacher for their own. All of
# them answer JSON, because they are fired from the roster table without
# leaving the page — the person doing this is usually standing at a terminal
# with a phone in one hand.

def _roster_target(request, pk):
    """Fetch the student and check this user is allowed to change them."""
    student = get_object_or_404(
        Student.objects.select_related("section", "section__school_class"), pk=pk)
    if not can_edit_student(request.user, student):
        return None, JsonResponse(
            {"ok": False, "error": "That student is not in one of your classes."},
            status=403)
    return student, None


def _row_state(student):
    """Everything the table row needs to redraw itself."""
    return {
        "id": student.pk,
        "device_user_id": student.device_user_id,
        "rfid": student.rfid_number,
        "face": student.face_status,
        "face_label": student.get_face_status_display(),
        "face_detail": student.face_detail,
        "face_at": (timezone.localtime(student.face_enrolled_at).strftime("%d %b, %H:%M")
                    if student.face_enrolled_at else ""),
        "active": student.is_active,
    }


@staff_required
def student_set_rfid(request, pk):
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    student, denied = _roster_target(request, pk)
    if denied:
        return denied

    ok, message = set_rfid(student, request.POST.get("rfid", ""))
    student.refresh_from_db()
    payload = {"ok": ok, "message": message, **_row_state(student)}
    if not ok:
        # Keep failures in one predictable place rather than making every caller
        # remember that a refusal arrives under "message".
        payload["error"] = message
    return JsonResponse(payload, status=200 if ok else 409)


@staff_required
def student_face(request, pk):
    """Start, cancel or clear a face enrolment."""
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    student, denied = _roster_target(request, pk)
    if denied:
        return denied

    action = request.POST.get("action", "start")

    if action == "start":
        devices = list(student_devices())
        if not devices:
            return JsonResponse(
                {"ok": False,
                 "error": "No active terminal is registered, so there is nowhere to "
                          "enrol a face. Add a device first."},
                status=409)
        user_id, devices = start_face_enrolment(student, requested_by=request.user.username)
        student.refresh_from_db()
        return JsonResponse({
            "ok": True,
            "message": (f"Ready. Send {student.full_name} to a terminal and enrol the "
                        f"face against user id {user_id}."),
            "user_id": user_id,
            "devices": [d.name for d in devices],
            **_row_state(student),
        })

    if action == "cancel":
        cancel_face_enrolment(student)
        student.refresh_from_db()
        return JsonResponse({"ok": True, "message": "Cancelled.", **_row_state(student)})

    if action == "reset":
        reset_face(student)
        student.refresh_from_db()
        return JsonResponse({"ok": True, "message": "Face cleared — enrol again.",
                             **_row_state(student)})

    return JsonResponse({"ok": False, "error": "Unknown action"}, status=400)


@staff_required
def student_set_active(request, pk):
    """Take a student off the roll, or put them back."""
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    student, denied = _roster_target(request, pk)
    if denied:
        return denied

    if request.POST.get("active") == "1":
        reactivate(student, by=request.user)
        message = f"{student.full_name} is back on the roll."
    else:
        removed = deactivate(
            student,
            reason=request.POST.get("reason", ""),
            note=request.POST.get("note", ""),
            by=request.user,
            remove_from_devices=request.POST.get("keep_on_devices") != "1",
        )
        message = f"{student.full_name} taken off the roll"
        message += (f", and queued for removal from {removed} terminal"
                    f"{'s' if removed != 1 else ''}." if removed else ".")

    student.refresh_from_db()
    return JsonResponse({"ok": True, "message": message, **_row_state(student)})


@staff_required
def student_states(request):
    """
    Poll a handful of rows.

    The roster page asks for this every few seconds while any student is
    waiting at a terminal, so the tick appears without anyone refreshing.
    """
    raw = request.GET.get("ids", "")
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()][:200]
    students = Student.objects.filter(pk__in=ids)
    allowed = request.user.allowed_section_ids()
    if allowed is not None:
        students = students.filter(section_id__in=allowed)
    return JsonResponse({"students": [_row_state(s) for s in students]})


@admin_required
def student_import(request):
    """
    Upload a spreadsheet and hand it to a background worker.

    The request only saves the file and queues a job — a 5,000 row import must
    not run inside a web request on a small server, where it would hold a
    worker for minutes and time out behind nginx.
    """
    form = BulkStudentImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        job = form.save(commit=False)
        job.original_name = form.cleaned_data["upload"].name[:200]
        job.created_by = request.user
        job.save()
        _dispatch(request, job)
        return redirect("academics:import_progress", pk=job.pk)

    return render(request, "academics/student_import.html", {
        "form": form,
        "jobs": ImportJob.objects.select_related("created_by")[:8],
        "classes": SchoolClass.objects.count(),
        "sections": Section.objects.count(),
        "students": Student.objects.filter(is_active=True).count(),
    })


def _dispatch(request, job):
    """Queue the job, and say plainly what happened if the broker is not there."""
    from .tasks import run_import_job

    try:
        result = run_import_job.apply_async(args=[job.pk])
        ImportJob.objects.filter(pk=job.pk).update(task_id=getattr(result, "id", "") or "")
        messages.success(request, "File received. The import is running in the background.")
    except Exception as exc:  # noqa: BLE001 - broker down is an expected state
        log.warning("could not queue import job %s: %s", job.pk, exc)
        messages.warning(
            request,
            "The file is saved, but no background worker could be reached, so nothing has "
            "been imported yet. Press “Run it now” below to import it straight away, or "
            "start Celery and press “Queue again”. From the server: "
            f"python manage.py run_import {job.pk}")


@admin_required
def import_progress(request, pk):
    job = get_object_or_404(ImportJob.objects.select_related("created_by"), pk=pk)
    return render(request, "academics/import_progress.html", {"job": job})


@admin_required
def import_status(request, pk):
    """Tiny JSON endpoint the progress page polls."""
    job = get_object_or_404(ImportJob, pk=pk)
    return JsonResponse({
        "status": job.status,
        "status_label": job.get_status_display(),
        "percent": job.percent,
        "running": job.is_running,
        "total": job.total_rows,
        "processed": job.processed,
        "created": job.created,
        "updated": job.updated,
        "skipped": job.skipped,
        "deactivated": job.deactivated,
        "error": job.error,
        "problems": job.problems[:50],
        "new": {
            "classes": job.new_classes, "sections": job.new_sections,
            "shifts": job.new_shifts, "versions": job.new_versions,
            "groups": job.new_groups,
        },
    })


@admin_required
def import_retry(request, pk):
    """Hand the job to a worker again."""
    job = get_object_or_404(ImportJob, pk=pk)
    if job.status == ImportStatus.RUNNING:
        messages.info(request, "That job is already running.")
    else:
        _reset(job)
        _dispatch(request, job)
    return redirect("academics:import_progress", pk=job.pk)


@admin_required
def import_run_now(request, pk):
    """
    Run the import here, in this request, instead of waiting for a worker.

    This is the escape hatch for when there is no Celery — a laptop with no
    Redis, or a server where the worker has stopped. The old Run now button
    only re-queued to the same broker that was already unreachable, which
    looked like it did nothing at all.

    It blocks until finished. 5,000 rows takes a second or two; a very large
    file on a slow disk could take longer, and a proxy in front with a short
    timeout may cut the browser off — the import itself still completes.
    """
    from .imports import run_job

    job = get_object_or_404(ImportJob, pk=pk)
    if job.status == ImportStatus.RUNNING:
        messages.info(request, "That job is already running somewhere.")
        return redirect("academics:import_progress", pk=job.pk)

    _reset(job)
    try:
        run_job(job)
    except Exception as exc:  # noqa: BLE001 - the job row carries the detail
        log.exception("inline import %s failed", job.pk)
        job.refresh_from_db()
        job.status = ImportStatus.FAILED
        job.error = str(exc)[:2000]
        job.finished_at = timezone.now()
        job.save()

    job.refresh_from_db()
    if job.status == ImportStatus.FAILED:
        messages.error(request, job.error or "The import failed.")
    elif job.dry_run:
        messages.success(request,
                         f"Checked {job.total_rows} row(s). Nothing was saved.")
    else:
        messages.success(
            request,
            f"{job.created} added, {job.updated} updated, {job.skipped} skipped.")
    return redirect("academics:import_progress", pk=job.pk)


def _reset(job):
    job.status = ImportStatus.PENDING
    job.error = ""
    job.processed = job.created = job.updated = job.skipped = job.deactivated = 0
    job.problems = []
    job.save()


@admin_required
def import_sample(request):
    """Download the blank workbook, pre-filled with this school's structure."""
    from .sample import build_sample_workbook

    content = build_sample_workbook()
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="student-import-{stamp}.xlsx"'
    return response


# --------------------------------------------------------------- teachers ---

@admin_required
def teacher_list(request):
    queryset = Teacher.objects.all()
    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(
            Q(full_name__icontains=search) | Q(employee_code__icontains=search)
            | Q(device_user_id__icontains=search))
    return render(request, "academics/teacher_list.html",
                  {"page": paginate(request, queryset), "search": search,
                   "total": queryset.count()})


@admin_required
def teacher_form(request, pk=None):
    instance = get_object_or_404(Teacher, pk=pk) if pk else None
    form = TeacherForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Teacher saved.")
        return redirect("academics:teacher_list")
    return render(request, "academics/simple_form.html",
                  {"form": form, "title": "Edit teacher" if instance else "Add teacher",
                   "back": "academics:teacher_list"})


@admin_required
def access_list(request):
    if request.method == "POST":
        form = ClassAccessForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Access granted.")
            return redirect("academics:access_list")
    else:
        form = ClassAccessForm()
    return render(request, "academics/access_list.html", {
        "form": form,
        "grants": ClassAccess.objects.select_related("teacher", "school_class", "section"),
    })


@admin_required
def access_delete(request, pk):
    ClassAccess.objects.filter(pk=pk).delete()
    messages.success(request, "Access removed.")
    return redirect("academics:access_list")


# --------------------------------------------------------------- holidays ---

@admin_required
def holiday_list(request):
    if request.method == "POST":
        form = HolidayForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Holiday saved.")
            return redirect("academics:holiday_list")
    else:
        form = HolidayForm()
    return render(request, "academics/holiday_list.html",
                  {"form": form, "holidays": Holiday.objects.all()[:200]})


@admin_required
def holiday_delete(request, pk):
    Holiday.objects.filter(pk=pk).delete()
    return redirect("academics:holiday_list")
