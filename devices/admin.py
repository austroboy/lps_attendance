from django.contrib import admin

from .models import Device, DeviceCommand, DeviceEnrollment, Punch, TrafficLog, UnknownDevice


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("serial_number", "name", "purpose", "is_active", "last_seen", "punch_count")
    search_fields = ("serial_number", "name")


@admin.register(Punch)
class PunchAdmin(admin.ModelAdmin):
    list_display = ("device_user_id", "punch_time", "device", "verify_mode", "processed")
    list_filter = ("device", "processed")
    search_fields = ("device_user_id",)


admin.site.register([DeviceCommand, DeviceEnrollment, TrafficLog, UnknownDevice])
