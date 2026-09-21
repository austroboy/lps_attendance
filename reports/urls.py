from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("daily/", views.daily, name="daily"),
    path("monthly/", views.monthly, name="monthly"),
    path("low-attendance/", views.defaulters, name="defaulters"),
    path("teachers/", views.teacher_report, name="teachers"),
    path("devices/", views.device_report, name="devices"),
    path("mine/", views.my_attendance, name="mine"),
]
