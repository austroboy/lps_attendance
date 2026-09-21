from django.urls import path

from . import views

app_name = "devices"

urlpatterns = [
    path("", views.device_list, name="list"),
    path("new/", views.device_form, name="new"),
    path("bulk/", views.device_bulk_add, name="bulk"),
    path("discovered/", views.discovered, name="discovered"),
    path("discovered/<int:pk>/forget/", views.discovered_forget, name="discovered_forget"),
    path("<int:pk>/", views.device_detail, name="detail"),
    path("<int:pk>/edit/", views.device_form, name="edit"),
    path("<int:pk>/sync-time/", views.device_sync_time, name="sync_time"),
    path("<int:pk>/pull-logs/", views.device_pull_logs, name="pull_logs"),
    path("commands/<int:pk>/cancel/", views.command_cancel, name="command_cancel"),
    path("punches/", views.punch_list, name="punches"),
    path("enrollments/", views.enrollment_map, name="enrollments"),
    path("traffic/", views.traffic_list, name="traffic"),
    path("traffic/clear/", views.traffic_clear, name="traffic_clear"),
]
