from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from academics.models import Section, Teacher

WEEKDAYS = [
    (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
    (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
]
WEEKDAY_SHORT = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


class SlotType(models.TextChoices):
    STUDENT = "STUDENT", "Student"
    TEACHER = "TEACHER", "Teacher"


class AttendanceStatus(models.TextChoices):
    PRESENT = "PRESENT", "Present"
    LATE = "LATE", "Late"
    ABSENT = "ABSENT", "Absent"
    LEAVE = "LEAVE", "On leave"
    HOLIDAY = "HOLIDAY", "Holiday"


class TimeSlot(models.Model):
    """
    One attendance window — the "box" you drag classes into.

    A punch counts for this slot when it lands between `start_time` and
    `end_time`. On or after `late_time` it is marked late instead of present.
    Anyone with no punch by `end_time` is absent for this slot.
    """
    name = models.CharField(max_length=80, help_text="e.g. Morning assembly")
    slot_type = models.CharField(max_length=10, choices=SlotType.choices,
                                 default=SlotType.STUDENT, db_index=True)
    start_time = models.TimeField(help_text="Window opens — punches start counting")
    late_time = models.TimeField(help_text="On or after this, a punch is marked late")
    end_time = models.TimeField(help_text="Window closes — no punch by now means absent")

    out_start_time = models.TimeField(null=True, blank=True,
                                      help_text="Optional: check-out window opens")
    out_end_time = models.TimeField(null=True, blank=True,
                                    help_text="Optional: check-out window closes")

    days = models.JSONField(default=list, blank=True,
                            help_text="Weekday numbers, Monday=0. Empty means every day.")
    colour = models.CharField(max_length=7, default="#2f6f8f")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "start_time", "name"]

    def __str__(self):
        return f"{self.name} ({self.start_time:%H:%M}-{self.end_time:%H:%M})"

    def clean(self):
        if self.start_time and self.late_time and self.end_time:
            if not (self.start_time <= self.late_time <= self.end_time):
                raise ValidationError(
                    "Times must run in order: window opens, then late cut-off, then window closes.")

    def runs_on(self, weekday: int) -> bool:
        if not self.days:
            return True
        return weekday in [int(d) for d in self.days]

    @property
    def days_label(self):
        if not self.days:
            return "Every day"
        return ", ".join(WEEKDAY_SHORT[int(d)] for d in sorted(int(x) for x in self.days))

    @property
    def section_count(self):
        return self.sections.count()


class SlotSection(models.Model):
    """A class-section dropped into a student slot."""
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name="sections")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="slots")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("time_slot", "section")]
        ordering = ["section__school_class__order", "section__name"]

    def __str__(self):
        return f"{self.section} in {self.time_slot.name}"


class SlotTeacher(models.Model):
    """A teacher dropped into a teacher slot."""
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name="teachers")
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="slots")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("time_slot", "teacher")]
        ordering = ["teacher__full_name"]

    def __str__(self):
        return f"{self.teacher.full_name} in {self.time_slot.name}"


class BaseRecord(models.Model):
    date = models.DateField(db_index=True)
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=AttendanceStatus.choices, db_index=True)
    in_time = models.DateTimeField(null=True, blank=True)
    out_time = models.DateTimeField(null=True, blank=True)
    minutes_late = models.IntegerField(default=0)
    device = models.ForeignKey("devices.Device", on_delete=models.SET_NULL, null=True, blank=True)
    is_manual = models.BooleanField(default=False)
    edited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name="+")
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AttendanceRecord(BaseRecord):
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE,
                                related_name="attendance")

    class Meta:
        unique_together = [("date", "student", "time_slot")]
        ordering = ["-date", "student__section__school_class__order", "student__roll_no"]
        indexes = [models.Index(fields=["date", "status"])]

    def __str__(self):
        return f"{self.student.full_name} {self.date} {self.status}"


class TeacherAttendanceRecord(BaseRecord):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="attendance")

    class Meta:
        unique_together = [("date", "teacher", "time_slot")]
        ordering = ["-date", "teacher__full_name"]

    def __str__(self):
        return f"{self.teacher.full_name} {self.date} {self.status}"
