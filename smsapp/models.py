from django.db import models

from academics.models import Section, Teacher
from attendance.models import AttendanceStatus, TimeSlot


class SmsTemplate(models.Model):
    name = models.CharField(max_length=80, unique=True)
    body = models.TextField(
        help_text="Placeholders: {name} {roll} {class} {section} {date} {time} "
                  "{status} {slot} {guardian} {school}")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def render(self, context: dict) -> str:
        text = self.body
        for key, value in context.items():
            text = text.replace("{" + key + "}", "" if value is None else str(value))
        return text


class Audience(models.TextChoices):
    GUARDIAN = "GUARDIAN", "Student guardian"
    STUDENT = "STUDENT", "Student's own number"
    TEACHER = "TEACHER", "Teacher"


class SmsSchedule(models.Model):
    """
    An SMS "box". Drag classes into it, and at `send_time` every matching
    student in those classes gets one message.
    """
    name = models.CharField(max_length=80, help_text="e.g. Absent alert, 9:30")
    audience = models.CharField(max_length=10, choices=Audience.choices,
                                default=Audience.GUARDIAN)
    template = models.ForeignKey(SmsTemplate, on_delete=models.PROTECT, related_name="schedules")
    time_slot = models.ForeignKey(
        TimeSlot, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Report on this slot only. Leave empty to cover every slot that day.")
    statuses = models.JSONField(
        default=list, blank=True,
        help_text="Which statuses trigger a message, e.g. [\"ABSENT\", \"LATE\"]. Empty means all.")
    send_time = models.TimeField(help_text="Messages go out at this time")
    days = models.JSONField(default=list, blank=True,
                            help_text="Weekday numbers, Monday=0. Empty means every day.")
    colour = models.CharField(max_length=7, default="#8a5a2b")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "send_time", "name"]

    def __str__(self):
        return f"{self.name} @ {self.send_time:%H:%M}"

    def runs_on(self, weekday: int) -> bool:
        if not self.days:
            return True
        return weekday in [int(d) for d in self.days]

    @property
    def status_label(self):
        if not self.statuses:
            return "Every status"
        labels = dict(AttendanceStatus.choices)
        return ", ".join(labels.get(s, s) for s in self.statuses)

    @property
    def days_label(self):
        from attendance.models import WEEKDAY_SHORT
        if not self.days:
            return "Every day"
        return ", ".join(WEEKDAY_SHORT[int(d)] for d in sorted(int(x) for x in self.days))

    @property
    def section_count(self):
        return self.sections.count()


class SmsScheduleSection(models.Model):
    schedule = models.ForeignKey(SmsSchedule, on_delete=models.CASCADE, related_name="sections")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="sms_schedules")

    class Meta:
        unique_together = [("schedule", "section")]
        ordering = ["section__school_class__order", "section__name"]


class SmsScheduleTeacher(models.Model):
    schedule = models.ForeignKey(SmsSchedule, on_delete=models.CASCADE, related_name="teacher_targets")
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="sms_schedules")

    class Meta:
        unique_together = [("schedule", "teacher")]


class SmsStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    SKIPPED = "SKIPPED", "Skipped"


class SmsLog(models.Model):
    msisdn = models.CharField(max_length=20, db_index=True)
    body = models.TextField()
    csms_id = models.CharField(max_length=24, unique=True)
    status = models.CharField(max_length=8, choices=SmsStatus.choices, default=SmsStatus.QUEUED)
    schedule = models.ForeignKey(SmsSchedule, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="logs")
    student = models.ForeignKey("academics.Student", on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="sms_logs")
    teacher = models.ForeignKey(Teacher, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="sms_logs")
    for_date = models.DateField(null=True, blank=True, db_index=True)
    reference_id = models.CharField(max_length=64, blank=True)
    error = models.CharField(max_length=250, blank=True)
    response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.msisdn} {self.status}"


class SmsRun(models.Model):
    """One execution of a schedule, so a restart cannot double-send."""
    schedule = models.ForeignKey(SmsSchedule, on_delete=models.CASCADE, related_name="runs")
    for_date = models.DateField()
    started_at = models.DateTimeField(auto_now_add=True)
    total = models.IntegerField(default=0)
    sent = models.IntegerField(default=0)
    failed = models.IntegerField(default=0)
    skipped = models.IntegerField(default=0)
    detail = models.TextField(blank=True)

    class Meta:
        unique_together = [("schedule", "for_date")]
        ordering = ["-started_at"]
