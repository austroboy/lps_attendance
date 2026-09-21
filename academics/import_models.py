"""
Bulk student import: the job record, the parser, and the worker.

5,000 rows on a small VPS is not something to do inside a web request. The
upload only creates an ImportJob and hands it to Celery; the browser then polls
a tiny status endpoint. If Celery happens to be down, the job sits in PENDING
and a management command can run it — nothing is lost and nothing half-applies.
"""
from django.conf import settings
from django.db import models


class ImportStatus(models.TextChoices):
    PENDING = "PENDING", "Waiting for a worker"
    RUNNING = "RUNNING", "Importing"
    DONE = "DONE", "Finished"
    FAILED = "FAILED", "Failed"


class ImportKind(models.TextChoices):
    STUDENTS = "STUDENTS", "Students"
    TEACHERS = "TEACHERS", "Teachers"


class ImportJob(models.Model):
    kind = models.CharField(max_length=10, choices=ImportKind.choices,
                            default=ImportKind.STUDENTS, db_index=True)
    upload = models.FileField(upload_to="imports/%Y/%m/")
    original_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=8, choices=ImportStatus.choices,
                              default=ImportStatus.PENDING, db_index=True)

    dry_run = models.BooleanField(
        default=False,
        help_text="Check the file and report problems without writing anything.")
    create_logins = models.BooleanField(
        default=False,
        help_text="Also create a login for each new row.")
    deactivate_missing = models.BooleanField(
        default=False,
        help_text="Mark anyone not in the file as inactive.")

    total_rows = models.IntegerField(default=0)
    processed = models.IntegerField(default=0)
    created = models.IntegerField(default=0)
    updated = models.IntegerField(default=0)
    skipped = models.IntegerField(default=0)
    deactivated = models.IntegerField(default=0)

    # What the file caused to come into existence, so you can see at a glance
    # that a typo invented "Clas Six" as a new class.
    new_classes = models.JSONField(default=list, blank=True)
    new_sections = models.JSONField(default=list, blank=True)
    new_shifts = models.JSONField(default=list, blank=True)
    new_versions = models.JSONField(default=list, blank=True)
    new_groups = models.JSONField(default=list, blank=True)

    problems = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    task_id = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.original_name or self.upload.name} ({self.status})"

    @property
    def percent(self):
        if not self.total_rows:
            return 0
        return min(100, round(100 * self.processed / self.total_rows))

    @property
    def is_running(self):
        return self.status in (ImportStatus.PENDING, ImportStatus.RUNNING)

    @property
    def is_teachers(self):
        return self.kind == ImportKind.TEACHERS

    @property
    def created_anything(self):
        return any([self.new_classes, self.new_sections, self.new_shifts,
                    self.new_versions, self.new_groups])
