from django.urls import path

from . import views

app_name = "smsapp"

urlpatterns = [
    path("board/", views.board, name="board"),
    path("board/assign/", views.board_assign, name="board_assign"),
    path("board/assign-class/", views.board_assign_class, name="board_assign_class"),
    path("schedules/new/", views.schedule_form, name="schedule_new"),
    path("schedules/<int:pk>/", views.schedule_form, name="schedule_edit"),
    path("schedules/<int:pk>/delete/", views.schedule_delete, name="schedule_delete"),
    path("schedules/<int:pk>/preview/", views.schedule_preview, name="schedule_preview"),
    path("schedules/<int:pk>/run/", views.schedule_run, name="schedule_run"),
    path("templates/", views.template_list, name="templates"),
    path("templates/new/", views.template_form, name="template_new"),
    path("templates/<int:pk>/", views.template_form, name="template_edit"),
    path("compose/", views.compose, name="compose"),
    path("logs/", views.logs, name="logs"),
]
