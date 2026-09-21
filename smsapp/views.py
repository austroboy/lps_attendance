import datetime as dt

from django import forms
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from academics.models import SchoolClass, Section, Teacher
from accounts.decorators import admin_required
from attendance.models import AttendanceStatus, SlotType, TimeSlot
from common import ColourInput, StyledFormMixin, TimeInput, WeekdayField, csv_response, paginate

from .gateway import SslWirelessClient, normalise_msisdn
from .models import (
    Audience,
    SmsLog,
    SmsRun,
    SmsSchedule,
    SmsScheduleSection,
    SmsScheduleTeacher,
    SmsTemplate,
)
from .services import build_messages, run_schedule, send_free_text


class TemplateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SmsTemplate
        fields = ["name", "body", "is_active"]
        widgets = {"body": forms.Textarea(attrs={"rows": 5})}


class ScheduleForm(StyledFormMixin, forms.ModelForm):
    days = WeekdayField()
    statuses = forms.MultipleChoiceField(
        choices=AttendanceStatus.choices, required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Only students with one of these statuses get a message. "
                  "Leave empty to message everyone.")

    class Meta:
        model = SmsSchedule
        fields = ["name", "audience", "template", "time_slot", "statuses",
                  "send_time", "days", "colour", "order", "is_active"]
        widgets = {"send_time": TimeInput(), "colour": ColourInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["time_slot"].queryset = TimeSlot.objects.filter(is_active=True)


class ComposeForm(StyledFormMixin, forms.Form):
    section = forms.ModelChoiceField(
        queryset=Section.objects.select_related("school_class"), required=False,
        label="Send to a whole class", help_text="Guardian numbers of every active student.")
    numbers = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 4}),
        label="Extra numbers", help_text="Comma or newline separated.")
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), label="Message")

    def clean(self):
        data = super().clean()
        if not data.get("section") and not (data.get("numbers") or "").strip():
            raise forms.ValidationError("Pick a class or type at least one number.")
        return data


# -------------------------------------------------------------- templates ---

@admin_required
def template_list(request):
    return render(request, "smsapp/template_list.html",
                  {"templates": SmsTemplate.objects.all()})


@admin_required
def template_form(request, pk=None):
    instance = get_object_or_404(SmsTemplate, pk=pk) if pk else None
    form = TemplateForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Template saved.")
        return redirect("smsapp:templates")
    return render(request, "smsapp/template_form.html",
                  {"form": form, "title": "Edit template" if instance else "New template"})


# --------------------------------------------------------------- schedule ---

@admin_required
def schedule_form(request, pk=None):
    instance = get_object_or_404(SmsSchedule, pk=pk) if pk else None
    form = ScheduleForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "SMS schedule saved.")
        return redirect("smsapp:board")
    return render(request, "smsapp/schedule_form.html",
                  {"form": form, "schedule": instance,
                   "title": "Edit SMS schedule" if instance else "New SMS schedule",
                   "has_templates": SmsTemplate.objects.exists()})


@admin_required
def schedule_delete(request, pk):
    SmsSchedule.objects.filter(pk=pk).delete()
    messages.success(request, "Schedule deleted.")
    return redirect("smsapp:board")


@admin_required
def board(request):
    """Same drag-and-drop idea as the timetable, but the boxes send messages."""
    schedules = (SmsSchedule.objects.select_related("template", "time_slot")
                 .prefetch_related("sections__section__school_class", "teacher_targets__teacher"))
    return render(request, "smsapp/board.html", {
        "schedules": schedules,
        "pool": Section.objects.select_related("school_class"),
        "teachers": Teacher.objects.filter(is_active=True),
        "classes": SchoolClass.objects.prefetch_related("sections"),
        "gateway_ready": SslWirelessClient().configured,
        "has_templates": SmsTemplate.objects.exists(),
    })


@admin_required
def board_assign(request):
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    schedule = get_object_or_404(SmsSchedule, pk=request.POST.get("slot"))
    action = request.POST.get("action", "add")
    kind = request.POST.get("kind", "section")
    target = request.POST.get("target")

    if kind == "teacher":
        model, field = SmsScheduleTeacher, "teacher_id"
    else:
        model, field = SmsScheduleSection, "section_id"

    if action == "add":
        model.objects.get_or_create(schedule=schedule, **{field: target})
    else:
        model.objects.filter(schedule=schedule, **{field: target}).delete()
    return JsonResponse({"ok": True, "count": schedule.sections.count()})


@admin_required
def board_assign_class(request):
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    schedule = get_object_or_404(SmsSchedule, pk=request.POST.get("slot"))
    school_class = get_object_or_404(SchoolClass, pk=request.POST.get("target"))
    for section in school_class.sections.all():
        SmsScheduleSection.objects.get_or_create(schedule=schedule, section=section)
    return JsonResponse({"ok": True, "count": schedule.sections.count()})


@admin_required
def schedule_preview(request, pk):
    """See exactly who would be messaged, before spending a single taka."""
    schedule = get_object_or_404(SmsSchedule, pk=pk)
    day_text = request.GET.get("date") or timezone.localdate().isoformat()
    try:
        day = dt.date.fromisoformat(day_text)
    except ValueError:
        day = timezone.localdate()
    rows = build_messages(schedule, day)
    return render(request, "smsapp/preview.html",
                  {"schedule": schedule, "day": day,
                   "page": paginate(request, rows), "total": len(rows),
                   "gateway_ready": SslWirelessClient().configured})


@admin_required
def schedule_run(request, pk):
    schedule = get_object_or_404(SmsSchedule, pk=pk)
    day_text = request.POST.get("date") or timezone.localdate().isoformat()
    day = dt.date.fromisoformat(day_text)
    run = run_schedule(schedule, day, force=request.POST.get("force") == "1")
    messages.success(request,
                     f"{schedule.name}: {run.sent} sent, {run.failed} failed, "
                     f"out of {run.total} recipients.")
    return redirect("smsapp:logs")


# ---------------------------------------------------------------- compose ---

@admin_required
def compose(request):
    form = ComposeForm(request.POST or None)
    client = SslWirelessClient()
    if request.method == "POST" and form.is_valid():
        numbers = []
        section = form.cleaned_data["section"]
        if section:
            numbers += [s.sms_number for s in section.students.filter(is_active=True)
                        if s.sms_number]
        extra = form.cleaned_data["numbers"].replace(",", "\n").split("\n")
        numbers += [n.strip() for n in extra if n.strip()]
        result = send_free_text(numbers, form.cleaned_data["body"])
        if result["errors"]:
            messages.error(request, "; ".join(result["errors"])[:300])
        messages.success(
            request,
            f"{result['sent']} sent, {result['failed']} failed, "
            f"{result['invalid']} number(s) were not valid Bangladeshi mobile numbers.")
        return redirect("smsapp:logs")
    return render(request, "smsapp/compose.html",
                  {"form": form, "gateway_ready": client.configured})


@admin_required
def logs(request):
    queryset = SmsLog.objects.select_related("student", "teacher", "schedule")
    search = request.GET.get("q", "").strip()
    status = request.GET.get("status")
    if search:
        queryset = queryset.filter(msisdn__icontains=search)
    if status:
        queryset = queryset.filter(status=status)
    if request.GET.get("export"):
        return csv_response("sms-log.csv", ["Number", "Status", "When", "Message"],
                            [[row.msisdn, row.status,
                              timezone.localtime(row.created_at).strftime("%Y-%m-%d %H:%M"),
                              row.body] for row in queryset[:5000]])
    return render(request, "smsapp/logs.html", {
        "page": paginate(request, queryset),
        "runs": SmsRun.objects.select_related("schedule")[:15],
        "search": search, "status": status,
        "total": queryset.count(),
    })
