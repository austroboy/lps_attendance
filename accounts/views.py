import datetime as dt

from django import forms
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.views import LoginView
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from common import StyledFormMixin, paginate

from .decorators import admin_required, login_required_any, superadmin_required
from .models import Role, User


class BrandedLoginForm(StyledFormMixin, AuthenticationForm):
    pass


class BrandedLoginView(LoginView):
    template_name = "accounts/login.html"
    form_class = BrandedLoginForm
    redirect_authenticated_user = True


class UserForm(StyledFormMixin, forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput, required=False,
        help_text="Leave blank to keep the current password.")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "phone", "role", "is_active"]

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
            user.must_change_password = False
        elif not user.pk:
            user.set_password(user.username)
            user.must_change_password = True
        if commit:
            user.save()
        return user


class ChangePasswordForm(StyledFormMixin, PasswordChangeForm):
    pass


@login_required_any
def home(request):
    """Send everyone to the screen that is actually theirs."""
    user = request.user
    if user.is_admin_level:
        return admin_dashboard(request)
    if user.is_teacher:
        return teacher_dashboard(request)
    return redirect("reports:mine")


def admin_dashboard(request):
    from academics.models import Student, Teacher
    from attendance.models import AttendanceRecord, TimeSlot
    from devices.models import Device, Punch, UnknownDevice
    from smsapp.models import SmsLog, SmsStatus

    today = timezone.localdate()
    records = AttendanceRecord.objects.filter(date=today)
    counts = {row["status"]: row["n"]
              for row in records.values("status").annotate(n=Count("id"))}
    total = sum(counts.values())
    present = counts.get("PRESENT", 0) + counts.get("LATE", 0)

    devices = list(Device.objects.all())
    recent = (Punch.objects.select_related("device").order_by("-punch_time")[:12])

    return render(request, "accounts/dashboard_admin.html", {
        "today": today,
        "counts": counts,
        "total_marked": total,
        "present": present,
        "rate": round(100 * present / total, 1) if total else 0,
        "devices": devices,
        "devices_online": sum(1 for d in devices if d.is_online),
        "unknown_devices": UnknownDevice.objects.count(),
        "students": Student.objects.filter(is_active=True).count(),
        "unmapped_students": Student.objects.filter(is_active=True, device_user_id="").count(),
        "teachers": Teacher.objects.filter(is_active=True).count(),
        "slots": TimeSlot.objects.filter(is_active=True).count(),
        "pending_punches": Punch.objects.filter(processed=False).count(),
        "unmatched_punches": Punch.objects.filter(
            process_note__icontains="No student or teacher").count(),
        "recent_punches": recent,
        "sms_today": SmsLog.objects.filter(created_at__date=today).count(),
        "sms_failed": SmsLog.objects.filter(created_at__date=today,
                                            status=SmsStatus.FAILED).count(),
    })


def teacher_dashboard(request):
    from academics.models import Student
    from attendance.models import AttendanceRecord

    teacher = request.user.teacher_profile
    today = timezone.localdate()
    section_ids = list(teacher.allowed_section_ids()) if teacher else []
    records = AttendanceRecord.objects.filter(date=today, student__section_id__in=section_ids)
    counts = {row["status"]: row["n"]
              for row in records.values("status").annotate(n=Count("id"))}

    from academics.models import Section
    return render(request, "accounts/dashboard_teacher.html", {
        "teacher": teacher,
        "today": today,
        "sections": Section.objects.filter(id__in=section_ids).select_related("school_class"),
        "counts": counts,
        "students": Student.objects.filter(section_id__in=section_ids, is_active=True).count(),
        "absent_today": records.filter(status="ABSENT").select_related(
            "student", "student__section")[:30],
        "my_attendance": (teacher.attendance.filter(date=today) if teacher else []),
    })


@admin_required
def user_list(request):
    queryset = User.objects.all()
    role = request.GET.get("role")
    search = request.GET.get("q", "").strip()
    if role:
        queryset = queryset.filter(role=role)
    if search:
        queryset = queryset.filter(
            Q(username__icontains=search) | Q(first_name__icontains=search)
            | Q(last_name__icontains=search))
    return render(request, "accounts/user_list.html",
                  {"page": paginate(request, queryset), "roles": Role.choices,
                   "role": role, "search": search, "total": queryset.count()})


@superadmin_required
def user_form(request, pk=None):
    instance = get_object_or_404(User, pk=pk) if pk else None
    form = UserForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "User saved.")
        return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html",
                  {"form": form, "title": "Edit user" if instance else "Add user",
                   "editing": instance})


@login_required_any
def change_password(request):
    form = ChangePasswordForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        update_session_auth_hash(request, user)
        messages.success(request, "Password changed.")
        return redirect("accounts:home")
    return render(request, "accounts/change_password.html", {"form": form})
