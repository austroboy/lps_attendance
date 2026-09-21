from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    path("board/", views.board, name="board"),
    path("board/assign/", views.board_assign, name="board_assign"),
    path("board/assign-class/", views.board_assign_class, name="board_assign_class"),
    path("slots/", views.slot_list, name="slot_list"),
    path("slots/new/", views.slot_form, name="slot_new"),
    path("slots/<int:pk>/", views.slot_form, name="slot_edit"),
    path("slots/<int:pk>/delete/", views.slot_delete, name="slot_delete"),
    path("register/", views.register, name="register"),
    path("record/<int:pk>/update/", views.record_update, name="record_update"),
    path("teachers/", views.teacher_register, name="teacher_register"),
    path("teachers/<int:pk>/update/", views.teacher_record_update, name="teacher_record_update"),
    path("maintenance/", views.maintenance, name="maintenance"),
]
