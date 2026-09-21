from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    SUPERADMIN = "SUPERADMIN", "Super admin"
    ADMIN = "ADMIN", "Admin"
    TEACHER = "TEACHER", "Teacher"
    STUDENT = "STUDENT", "Student"


class User(AbstractUser):
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.STUDENT)
    phone = models.CharField(max_length=20, blank=True)
    must_change_password = models.BooleanField(default=False)

    class Meta:
        ordering = ["username"]

    def __str__(self):
        full = self.get_full_name()
        return f"{full or self.username} ({self.get_role_display()})"

    def save(self, *args, **kwargs):
        # `manage.py createsuperuser` knows nothing about roles, so the first
        # admin on a fresh server would otherwise be labelled "Student" at the
        # top of every page.
        if self.is_superuser and self.role == Role.STUDENT:
            self.role = Role.SUPERADMIN
        super().save(*args, **kwargs)

    # -- role helpers ------------------------------------------------------
    @property
    def is_superadmin(self):
        return self.role == Role.SUPERADMIN or self.is_superuser

    @property
    def is_admin_level(self):
        """Super admin or admin — full run of the admin portal."""
        return self.is_superadmin or self.role == Role.ADMIN

    @property
    def is_teacher(self):
        return self.role == Role.TEACHER

    @property
    def is_student(self):
        return self.role == Role.STUDENT

    @property
    def teacher_profile(self):
        return getattr(self, "teacher", None)

    @property
    def student_profile(self):
        return getattr(self, "student", None)

    def allowed_section_ids(self):
        """Section ids this user may manage. None means 'everything'."""
        if self.is_admin_level:
            return None
        teacher = self.teacher_profile
        if not teacher:
            return []
        return list(teacher.allowed_section_ids())
