"""
The three things the office does to a student record all day.

Setting a card number, getting a face onto the terminals, and taking a student
off the roll when they leave. All three touch the devices as well as the
database, and all three are reachable by a class teacher for their own classes
as well as by an admin for everyone.
"""
from __future__ import annotations

import logging

from django.db.models import Q
from django.utils import timezone

from devices.ebkn import commands as C
from devices.models import Device, DevicePurpose
from devices.services import queue_command

from .models import FaceStatus, Student, Teacher

log = logging.getLogger(__name__)

# Which biometric slots a terminal uses for a face. Fingerprints are
# conventionally 0-9; faces sit above that, but the exact number varies by
# firmware, so this is a setting rather than a constant and we record whatever
# actually arrives.
FACE_BACKUP_NUMBERS = {10, 11, 12, 50, 51}


def can_edit_student(user, student) -> bool:
    """Admins get everyone; a teacher gets the sections granted to them."""
    if not user.is_authenticated:
        return False
    if user.is_admin_level:
        return True
    if not user.is_teacher:
        return False
    allowed = user.allowed_section_ids()
    return allowed is not None and student.section_id in allowed


def student_devices():
    """Terminals a student could reasonably enrol at."""
    return Device.objects.filter(
        is_active=True,
        purpose__in=[DevicePurpose.STUDENT, DevicePurpose.BOTH])


# ---------------------------------------------------------------- user ids --

def next_device_user_id() -> str:
    """
    Pick the next free numeric id for a terminal.

    A student with no device user id cannot be matched to a punch, so one is
    assigned automatically the moment face enrolment starts rather than being
    left as another thing to remember.
    """
    used = set()
    for values in (Student.objects.exclude(device_user_id="")
                   .values_list("device_user_id", flat=True),
                   Teacher.objects.exclude(device_user_id="")
                   .values_list("device_user_id", flat=True)):
        for value in values:
            if str(value).isdigit():
                used.add(int(value))
    candidate = max(used) + 1 if used else 1
    while candidate in used:
        candidate += 1
    return str(candidate)


def ensure_device_user_id(student: Student) -> str:
    if student.device_user_id:
        return student.device_user_id
    student.device_user_id = next_device_user_id()
    student.save(update_fields=["device_user_id"])
    return student.device_user_id


# ------------------------------------------------------------------- rfid --

def set_rfid(student: Student, raw: str):
    """
    Save a card number and push it to the terminals.

    Returns (ok, message). A card already on someone else is refused — two
    students sharing a card number means every tap is credited to whichever
    record the device happens to match first.
    """
    value = "".join(ch for ch in str(raw or "") if ch.isalnum()).upper()[:32]

    if value:
        clash = (Student.objects
                 .filter(rfid_number=value)
                 .exclude(pk=student.pk)
                 .select_related("section", "section__school_class")
                 .first())
        if clash:
            return False, f"That card is already on {clash.full_name} ({clash.section})."

    student.rfid_number = value
    student.save(update_fields=["rfid_number"])

    if value:
        user_id = ensure_device_user_id(student)
        for device in student_devices():
            queue_command(device, C.SET_USER_INFO,
                          {"user_id": user_id, "user_name": student.full_name[:24],
                           "card_number": value},
                          created_by="rfid")
        return True, "Card saved and queued to the terminals."
    return True, "Card cleared."


# ------------------------------------------------------------------- face --

def start_face_enrolment(student: Student, requested_by=""):
    """
    Put a student into "waiting at the terminal" and prepare the devices.

    There is no command in this protocol that makes a terminal start capturing
    a face on demand — the capture is done at the machine. What we can do is
    make sure the user id exists on every terminal with the right name showing,
    so whoever is standing there enrols the right person.
    """
    user_id = ensure_device_user_id(student)

    student.face_status = FaceStatus.PENDING
    student.face_requested_at = timezone.now()
    student.save(update_fields=["face_status", "face_requested_at"])

    devices = list(student_devices())
    for device in devices:
        params = {"user_id": user_id, "user_name": student.full_name[:24]}
        if student.rfid_number:
            params["card_number"] = student.rfid_number
        queue_command(device, C.SET_USER_INFO, params, created_by=str(requested_by)[:80])

    return user_id, devices


def cancel_face_enrolment(student: Student):
    student.face_status = (FaceStatus.DONE if student.face_enrolled_at
                           else FaceStatus.NONE)
    student.save(update_fields=["face_status"])


def reset_face(student: Student):
    """Forget the enrolment here so it can be done again."""
    student.face_status = FaceStatus.NONE
    student.face_enrolled_at = None
    student.face_device = None
    student.face_detail = ""
    student.save(update_fields=["face_status", "face_enrolled_at", "face_device",
                                "face_detail"])


def describe_backups(numbers) -> str:
    """Turn the terminal's backup numbers into something a person can read."""
    if not numbers:
        return "enrolled"
    clean = []
    for value in numbers:
        try:
            clean.append(int(value))
        except (TypeError, ValueError):
            continue
    if not clean:
        return "enrolled"
    faces = [n for n in clean if n in FACE_BACKUP_NUMBERS]
    fingers = [n for n in clean if 0 <= n <= 9]
    bits = []
    if faces:
        bits.append("face")
    if fingers:
        bits.append(f"{len(fingers)} fingerprint{'s' if len(fingers) > 1 else ''}")
    others = [n for n in clean if n not in faces and n not in fingers]
    if others:
        bits.append("slots " + ", ".join(str(n) for n in others))
    return ", ".join(bits) if bits else "enrolled"


def student_for_device_user(device_user_id):
    if not device_user_id:
        return None
    return Student.objects.filter(device_user_id=str(device_user_id)).first()


# ------------------------------------------------------------ leaving roll --

def deactivate(student: Student, reason="", note="", by=None, remove_from_devices=True):
    """
    Take a student off the roll.

    Their history stays — reports for last month must still be right. What
    changes is that they stop appearing on registers, stop receiving SMS, and,
    unless you say otherwise, are deleted from the terminals so a transferred
    student cannot walk in and mark themselves present.
    """
    student.is_active = False
    student.left_on = timezone.localdate()
    student.leave_reason = reason or ""
    student.leave_note = (note or "")[:200]
    student.deactivated_by = by if (by and by.is_authenticated) else None
    student.save(update_fields=["is_active", "left_on", "leave_reason", "leave_note",
                                "deactivated_by"])

    removed = 0
    if remove_from_devices and student.device_user_id:
        for device in student_devices():
            queue_command(device, C.DELETE_USER, {"user_id": student.device_user_id},
                          created_by=getattr(by, "username", "") or "system")
            removed += 1
    return removed


def reactivate(student: Student, by=None):
    student.is_active = True
    student.left_on = None
    student.leave_reason = ""
    student.leave_note = ""
    student.deactivated_by = None
    student.save(update_fields=["is_active", "left_on", "leave_reason", "leave_note",
                                "deactivated_by"])

    # They were deleted from the terminals on the way out, so put them back.
    if student.device_user_id:
        for device in student_devices():
            params = {"user_id": student.device_user_id,
                      "user_name": student.full_name[:24]}
            if student.rfid_number:
                params["card_number"] = student.rfid_number
            queue_command(device, C.SET_USER_INFO, params,
                          created_by=getattr(by, "username", "") or "system")
        if student.face_status == FaceStatus.DONE:
            # The template itself lived on the device and went with the record.
            reset_face(student)


def face_summary(queryset):
    """Little headline numbers for the roster screen."""
    return {
        "done": queryset.filter(face_status=FaceStatus.DONE).count(),
        "pending": queryset.filter(face_status=FaceStatus.PENDING).count(),
        "none": queryset.filter(Q(face_status=FaceStatus.NONE)).count(),
        "carded": queryset.exclude(rfid_number="").count(),
    }
