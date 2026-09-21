from django.conf import settings
from django.db import models
from django.utils import timezone


class AcademicSession(models.Model):
    name = models.CharField(max_length=40, unique=True, help_text="e.g. 2026")
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.name

    @classmethod
    def current(cls):
        return cls.objects.filter(is_active=True).first()


class Shift(models.Model):
    """Morning, Day, Evening — whatever this school actually runs."""
    name = models.CharField(max_length=40, unique=True)
    start_note = models.CharField(max_length=60, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Version(models.Model):
    """Bangla version, English version."""
    name = models.CharField(max_length=40, unique=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Group(models.Model):
    """Science, Business Studies, Humanities. Usually only class nine and up."""
    name = models.CharField(max_length=60, unique=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class SchoolClass(models.Model):
    name = models.CharField(max_length=60, help_text="e.g. Class Six")
    order = models.PositiveSmallIntegerField(default=0, help_text="Sort order in lists")
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE,
                                related_name="classes", null=True, blank=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = [("name", "session")]
        verbose_name_plural = "School classes"

    def __str__(self):
        return self.name


class Section(models.Model):
    """
    The smallest real group of students, and the thing you drag onto a
    timetable.

    Shift, version and group live here rather than on the student because that
    is how a school actually timetables: morning Science A and day Science A
    are two different rooms of children who arrive at different times, even
    though both are "Class Nine, Science, A".
    """
    school_class = models.ForeignKey(SchoolClass, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=30, help_text="e.g. A")
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, null=True, blank=True,
                              related_name="sections")
    version = models.ForeignKey(Version, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="sections")
    group = models.ForeignKey(Group, on_delete=models.PROTECT, null=True, blank=True,
                              related_name="sections",
                              help_text="Leave empty below class nine.")
    room = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["school_class__order", "shift__order", "version__order",
                    "group__order", "name"]
        unique_together = [("school_class", "name", "shift", "version", "group")]

    def __str__(self):
        bits = [self.school_class.name]
        if self.group_id:
            bits.append(self.group.name)
        bits.append(self.name)
        label = " - ".join(bits)
        tags = [t for t in (self.version.name if self.version_id else None,
                            self.shift.name if self.shift_id else None) if t]
        return f"{label} ({', '.join(tags)})" if tags else label

    @property
    def label(self):
        return str(self)


class Teacher(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="teacher", null=True, blank=True)
    employee_code = models.CharField(max_length=30, unique=True)
    full_name = models.CharField(max_length=120)
    designation = models.CharField(max_length=80, blank=True)
    campus = models.CharField(max_length=60, blank=True, db_index=True,
                              help_text="Which campus or branch, if the school has several.")
    phone = models.CharField(max_length=20, blank=True)
    # The user id that is actually enrolled on the face/finger terminal.
    device_user_id = models.CharField(
        max_length=32, blank=True, db_index=True,
        help_text="User ID enrolled on the attendance terminal (the number shown on the device).")
    is_active = models.BooleanField(default=True)
    # Nullable: an unknown joining date is better left blank than set to the day
    # the spreadsheet happened to be imported.
    joined_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.employee_code})"

    def allowed_section_ids(self):
        ids = set(self.class_access.values_list("section_id", flat=True))
        ids.discard(None)
        # Access rows with no section mean "the whole class".
        class_ids = self.class_access.filter(section__isnull=True).values_list("school_class_id", flat=True)
        if class_ids:
            ids.update(Section.objects.filter(school_class_id__in=class_ids).values_list("id", flat=True))
        return ids


class FaceStatus(models.TextChoices):
    NONE = "NONE", "Not enrolled"
    PENDING = "PENDING", "Waiting at the terminal"
    DONE = "DONE", "Enrolled"


class LeaveReason(models.TextChoices):
    TRANSFERRED = "TRANSFERRED", "Transferred to another school"
    CANCELLED = "CANCELLED", "Admission cancelled"
    PASSED_OUT = "PASSED_OUT", "Passed out"
    OTHER = "OTHER", "Other"


class Student(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                related_name="student", null=True, blank=True)
    admission_no = models.CharField(max_length=30, unique=True)
    roll_no = models.CharField(
        max_length=40, blank=True,
        help_text="Any text — digits, letters, dashes. Left blank, the admission number is used.")
    full_name = models.CharField(max_length=120)
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="students")
    guardian_name = models.CharField(max_length=120, blank=True)
    guardian_phone = models.CharField(max_length=20, blank=True,
                                      help_text="SMS goes here. 01XXXXXXXXX or 8801XXXXXXXXX.")
    student_phone = models.CharField(max_length=20, blank=True)
    device_user_id = models.CharField(
        max_length=32, blank=True, db_index=True,
        help_text="User ID enrolled on the attendance terminal.")
    rfid_number = models.CharField(
        max_length=32, blank=True, db_index=True,
        help_text="Card number, if the student carries one.")

    # Face enrolment. The terminal owns the actual template; all we keep is
    # whether this student has one, so the office can see at a glance who still
    # needs to stand in front of a machine.
    face_status = models.CharField(max_length=8, choices=FaceStatus.choices,
                                   default=FaceStatus.NONE, db_index=True)
    face_requested_at = models.DateTimeField(null=True, blank=True)
    face_enrolled_at = models.DateTimeField(null=True, blank=True)
    face_device = models.ForeignKey("devices.Device", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="face_enrolments")
    face_detail = models.CharField(
        max_length=120, blank=True,
        help_text="What the terminal reported — which biometric types it stored.")

    photo = models.ImageField(upload_to="students/", blank=True, null=True)
    is_active = models.BooleanField(default=True)
    left_on = models.DateField(null=True, blank=True)
    leave_reason = models.CharField(max_length=12, choices=LeaveReason.choices, blank=True)
    leave_note = models.CharField(max_length=200, blank=True)
    deactivated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["section__school_class__order", "section__name", "roll_no", "full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.section})"

    @property
    def school_class(self):
        return self.section.school_class

    def save(self, *args, **kwargs):
        # At this school the admission ID is the roll number. Filling it here
        # covers the add form, the edit form and the admin; the bulk importer
        # bypasses save() and does the same thing itself.
        if not (self.roll_no or "").strip():
            self.roll_no = self.admission_no
        super().save(*args, **kwargs)

    @property
    def sms_number(self):
        return self.guardian_phone or self.student_phone

    @property
    def face_done(self):
        return self.face_status == FaceStatus.DONE

    @property
    def face_waiting(self):
        return self.face_status == FaceStatus.PENDING

    def mark_face_enrolled(self, device=None, detail=""):
        """
        Called when a terminal tells us this student now has a face on file.

        Deliberately not limited to students we are currently waiting on: if
        someone enrols a face directly at the machine without anyone pressing
        anything here, that is still a fact worth recording.
        """
        from django.utils import timezone

        if self.face_status == FaceStatus.DONE and not detail:
            return False
        self.face_status = FaceStatus.DONE
        self.face_enrolled_at = timezone.now()
        if device is not None:
            self.face_device = device
        if detail:
            self.face_detail = detail[:120]
        self.save(update_fields=["face_status", "face_enrolled_at", "face_device",
                                 "face_detail"])
        return True


class ClassAccess(models.Model):
    """Grants a teacher control over one class, or one specific section of it."""
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="class_access")
    school_class = models.ForeignKey(SchoolClass, on_delete=models.CASCADE, related_name="teacher_access")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, null=True, blank=True,
                                help_text="Leave empty to grant the whole class.")
    can_edit_attendance = models.BooleanField(default=True)
    can_send_sms = models.BooleanField(default=False)

    class Meta:
        unique_together = [("teacher", "school_class", "section")]
        verbose_name_plural = "Class access"

    def __str__(self):
        target = self.section or self.school_class
        return f"{self.teacher.full_name} -> {target}"


class Holiday(models.Model):
    date = models.DateField(unique=True)
    name = models.CharField(max_length=120)
    applies_to_teachers = models.BooleanField(default=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date} {self.name}"


# The bulk-import job lives in its own module to keep this file about the school
# rather than about file handling.
from .import_models import ImportJob, ImportStatus  # noqa: E402,F401
