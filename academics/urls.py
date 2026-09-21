from django.urls import path

from . import views

app_name = "academics"

urlpatterns = [
    path("classes/", views.class_list, name="class_list"),
    path("classes/new/", views.class_form, name="class_new"),
    path("classes/<int:pk>/", views.class_form, name="class_edit"),
    path("sections/new/", views.section_form, name="section_new"),
    path("sections/<int:pk>/", views.section_form, name="section_edit"),
    path("shifts/new/", views.shift_form, name="shift_new"),
    path("shifts/<int:pk>/", views.shift_form, name="shift_edit"),
    path("versions/new/", views.version_form, name="version_new"),
    path("versions/<int:pk>/", views.version_form, name="version_edit"),
    path("groups/new/", views.group_form, name="group_new"),
    path("groups/<int:pk>/", views.group_form, name="group_edit"),
    path("sessions/new/", views.session_form, name="session_new"),
    path("sessions/<int:pk>/", views.session_form, name="session_edit"),

    path("students/", views.student_list, name="student_list"),
    path("students/new/", views.student_form, name="student_new"),
    path("students/<int:pk>/", views.student_form, name="student_edit"),
    path("students/states/", views.student_states, name="student_states"),
    path("students/<int:pk>/rfid/", views.student_set_rfid, name="student_set_rfid"),
    path("students/<int:pk>/face/", views.student_face, name="student_face"),
    path("students/<int:pk>/active/", views.student_set_active, name="student_set_active"),
    path("students/import/", views.student_import, name="student_import"),
    path("students/import/sample.xlsx", views.import_sample, name="import_sample"),
    path("students/import/<int:pk>/", views.import_progress, name="import_progress"),
    path("students/import/<int:pk>/status/", views.import_status, name="import_status"),
    path("students/import/<int:pk>/retry/", views.import_retry, name="import_retry"),
    path("students/import/<int:pk>/run/", views.import_run_now, name="import_run_now"),

    path("teachers/", views.teacher_list, name="teacher_list"),
    path("teachers/new/", views.teacher_form, name="teacher_new"),
    path("teachers/<int:pk>/", views.teacher_form, name="teacher_edit"),

    path("access/", views.access_list, name="access_list"),
    path("access/<int:pk>/delete/", views.access_delete, name="access_delete"),

    path("holidays/", views.holiday_list, name="holiday_list"),
    path("holidays/<int:pk>/delete/", views.holiday_delete, name="holiday_delete"),
]
