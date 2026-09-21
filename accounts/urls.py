from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.BrandedLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("password/", views.change_password, name="change_password"),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_form, name="user_new"),
    path("users/<int:pk>/", views.user_form, name="user_edit"),
]
