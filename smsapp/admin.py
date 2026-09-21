from django.contrib import admin

from .models import SmsLog, SmsRun, SmsSchedule, SmsScheduleSection, SmsTemplate


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    list_display = ("msisdn", "status", "for_date", "created_at")
    list_filter = ("status", "for_date")
    search_fields = ("msisdn",)


admin.site.register([SmsTemplate, SmsSchedule, SmsScheduleSection, SmsRun])
