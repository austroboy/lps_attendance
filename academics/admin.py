from django.contrib import admin

from .import_models import ImportJob
from .models import (
    AcademicSession,
    ClassAccess,
    Group,
    Holiday,
    SchoolClass,
    Section,
    Shift,
    Student,
    Teacher,
    Version,
)

admin.site.register([AcademicSession, SchoolClass, Section, ClassAccess, Holiday,
                     Shift, Version, Group])


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ("id", "original_name", "status", "total_rows", "created",
                    "updated", "skipped", "created_at")
    list_filter = ("status",)


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ("admission_no", "full_name", "section", "device_user_id", "is_active")
    search_fields = ("admission_no", "full_name", "device_user_id", "guardian_phone")
    list_filter = ("section__school_class", "is_active")


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ("employee_code", "full_name", "device_user_id", "is_active")
    search_fields = ("employee_code", "full_name", "device_user_id")
