from django.contrib import admin

from .models import (
    AttendanceRecord,
    SlotSection,
    SlotTeacher,
    TeacherAttendanceRecord,
    TimeSlot,
)


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ("name", "slot_type", "start_time", "late_time", "end_time", "is_active")
    list_filter = ("slot_type", "is_active")


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ("date", "student", "time_slot", "status", "in_time")
    list_filter = ("status", "date", "time_slot")
    search_fields = ("student__full_name", "student__admission_no")


admin.site.register([SlotSection, SlotTeacher, TeacherAttendanceRecord])
