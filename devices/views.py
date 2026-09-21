import json

from django.conf import settings

from django import forms
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import admin_required, staff_required
from common import StyledFormMixin, csv_response, paginate

from .ebkn import commands as C
from .models import (
    CommandStatus,
    Device,
    DeviceCommand,
    DeviceEnrollment,
    Punch,
    TrafficLog,
    UnknownDevice,
)
from .services import handle_device_request, queue_command


class DeviceForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Device
        fields = ["serial_number", "name", "location", "purpose", "is_active"]
        help_texts = {
            "serial_number": "Exactly the dev_id the terminal sends. Check Devices > "
                             "Discovered if you are not sure.",
        }


class BulkDeviceForm(StyledFormMixin, forms.Form):
    serials = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 12}),
        label="Serial numbers",
        help_text="One per line. Optionally add a name after a comma: "
                  "ENS2025041, Main gate")
    purpose = forms.ChoiceField(choices=Device._meta.get_field("purpose").choices)


class CommandForm(StyledFormMixin, forms.Form):
    cmd_code = forms.ChoiceField(choices=C.CHOICES, label="Command")
    params = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4}), required=False, initial="{}",
        label="Parameters (JSON)")


@admin_required
def device_list(request):
    devices = Device.objects.all()
    return render(request, "devices/device_list.html", {
        "devices": devices,
        "unknown_count": UnknownDevice.objects.count(),
        "endpoint": (getattr(settings, "EBKN_PUBLIC_ENDPOINT", "")
                     or request.build_absolute_uri("/ebkn/")),
        "online": sum(1 for d in devices if d.is_online),
    })


@admin_required
def device_form(request, pk=None):
    instance = get_object_or_404(Device, pk=pk) if pk else None
    form = DeviceForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Device saved.")
        return redirect("devices:list")
    return render(request, "devices/device_form.html",
                  {"form": form, "device": instance,
                   "title": "Edit device" if instance else "Add device"})


@admin_required
def device_bulk_add(request):
    """Register the whole batch in one go — twelve now, more later."""
    form = BulkDeviceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        created = skipped = 0
        for index, line in enumerate(form.cleaned_data["serials"].splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",", 1)]
            serial = parts[0]
            name = parts[1] if len(parts) > 1 and parts[1] else f"Device {index}"
            _obj, was_created = Device.objects.get_or_create(
                serial_number=serial,
                defaults={"name": name, "purpose": form.cleaned_data["purpose"]})
            created += was_created
            skipped += not was_created
        UnknownDevice.objects.filter(
            serial_number__in=Device.objects.values_list("serial_number", flat=True)).delete()
        messages.success(request, f"{created} device(s) added, {skipped} already existed.")
        return redirect("devices:list")
    return render(request, "devices/device_bulk.html", {"form": form})


@admin_required
def device_detail(request, pk):
    device = get_object_or_404(Device, pk=pk)
    form = CommandForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        raw = form.cleaned_data["params"].strip() or "{}"
        try:
            params = json.loads(raw)
        except json.JSONDecodeError as exc:
            form.add_error("params", f"That is not valid JSON: {exc}")
        else:
            queue_command(device, form.cleaned_data["cmd_code"], params,
                          created_by=request.user.username)
            messages.success(
                request,
                "Command queued. The terminal picks it up on its next poll, "
                "usually within a few seconds.")
            return redirect("devices:detail", pk=device.pk)

    return render(request, "devices/device_detail.html", {
        "device": device,
        "form": form,
        "hints": C.HINTS,
        "commands": device.commands.all()[:25],
        "punches": device.punches.all()[:25],
        "enrollments": device.enrollments.all()[:50],
    })


@admin_required
def command_cancel(request, pk):
    DeviceCommand.objects.filter(pk=pk, status=CommandStatus.WAIT).delete()
    messages.success(request, "Queued command removed.")
    return redirect(request.META.get("HTTP_REFERER", "/devices/"))


@admin_required
def device_sync_time(request, pk):
    device = get_object_or_404(Device, pk=pk)
    queue_command(device, C.SET_TIME,
                  {"time": timezone.localtime().strftime("%Y%m%d%H%M%S")},
                  created_by=request.user.username)
    messages.success(request, f"Clock sync queued for {device.name}.")
    return redirect("devices:detail", pk=device.pk)


@admin_required
def device_pull_logs(request, pk):
    device = get_object_or_404(Device, pk=pk)
    begin = request.GET.get("from", "")
    end = request.GET.get("to", "")
    queue_command(device, C.GET_LOG_DATA,
                  {"begin_time": begin, "end_time": end},
                  created_by=request.user.username)
    messages.success(request, "Backfill queued. Punches appear as the device answers.")
    return redirect("devices:detail", pk=device.pk)


@admin_required
def discovered(request):
    """Terminals that called in with a serial we have not registered."""
    if request.method == "POST":
        serial = request.POST.get("serial", "").strip()
        name = request.POST.get("name", "").strip() or serial
        if serial:
            Device.objects.get_or_create(
                serial_number=serial,
                defaults={"name": name, "purpose": request.POST.get("purpose", "STUDENT")})
            UnknownDevice.objects.filter(serial_number=serial).delete()
            messages.success(request, f"{serial} registered. It should go online within a minute.")
        return redirect("devices:discovered")
    return render(request, "devices/discovered.html",
                  {"unknown": UnknownDevice.objects.all(),
                   "purposes": Device._meta.get_field("purpose").choices})


@admin_required
def discovered_forget(request, pk):
    UnknownDevice.objects.filter(pk=pk).delete()
    return redirect("devices:discovered")


@staff_required
def punch_list(request):
    queryset = Punch.objects.select_related("device")
    search = request.GET.get("q", "").strip()
    device_id = request.GET.get("device")
    day = request.GET.get("date")
    unresolved = request.GET.get("unresolved")

    if search:
        queryset = queryset.filter(device_user_id__icontains=search)
    if device_id:
        queryset = queryset.filter(device_id=device_id)
    if day:
        queryset = queryset.filter(punch_time__date=day)
    if unresolved:
        queryset = queryset.filter(process_note__icontains="No student or teacher")

    if request.GET.get("export"):
        return csv_response(
            "punches.csv",
            ["Device", "Device user id", "Time", "Verify", "Note"],
            [[p.device.serial_number, p.device_user_id,
              timezone.localtime(p.punch_time).strftime("%Y-%m-%d %H:%M:%S"),
              p.verify_mode, p.process_note] for p in queryset[:5000]])

    return render(request, "devices/punch_list.html", {
        "page": paginate(request, queryset),
        "devices": Device.objects.all(),
        "search": search, "device_id": device_id, "day": day, "unresolved": unresolved,
        "total": queryset.count(),
    })


@admin_required
def traffic_list(request):
    queryset = TrafficLog.objects.all()
    dev_id = request.GET.get("dev_id", "").strip()
    if dev_id:
        queryset = queryset.filter(dev_id__icontains=dev_id)
    return render(request, "devices/traffic.html",
                  {"page": paginate(request, queryset, 60), "dev_id": dev_id})


@admin_required
def traffic_clear(request):
    TrafficLog.objects.all().delete()
    messages.success(request, "Traffic log cleared.")
    return redirect("devices:traffic")


@admin_required
def enrollment_map(request):
    """
    Side-by-side view of who the device knows about and who we know about.

    This is the screen you live in on installation day: the terminal reports
    user ids, and you attach each one to a student or a teacher.
    """
    from academics.models import Student, Teacher

    device_id = request.GET.get("device")
    enrollments = DeviceEnrollment.objects.select_related("device")
    if device_id:
        enrollments = enrollments.filter(device_id=device_id)

    if request.method == "POST":
        uid = request.POST.get("device_user_id", "").strip()
        target = request.POST.get("target", "")
        if uid and target:
            kind, _, pk = target.partition(":")
            if kind == "student":
                Student.objects.filter(pk=pk).update(device_user_id=uid)
            elif kind == "teacher":
                Teacher.objects.filter(pk=pk).update(device_user_id=uid)
            messages.success(request, f"Device user id {uid} linked.")
        return redirect(request.get_full_path())

    known_students = {s.device_user_id: s for s in
                      Student.objects.exclude(device_user_id="").select_related("section")}
    known_teachers = {t.device_user_id: t for t in Teacher.objects.exclude(device_user_id="")}

    rows = [{
        "enrollment": enrollment,
        "student": known_students.get(enrollment.device_user_id),
        "teacher": known_teachers.get(enrollment.device_user_id),
    } for enrollment in enrollments]

    return render(request, "devices/enrollment_map.html", {
        "page": paginate(request, rows),
        "total": len(rows),
        "devices": Device.objects.all(),
        "device_id": device_id,
        "students": Student.objects.filter(is_active=True).select_related(
            "section", "section__school_class")[:800],
        "teachers": Teacher.objects.filter(is_active=True),
    })


@csrf_exempt
def device_endpoint(request):
    """
    The URL you type into the terminal.

    In practice the middleware catches device traffic on any path, but this
    gives the devices a canonical address and gives you something to open in a
    browser to confirm the server is reachable from the device's network.
    """
    if request.method == "POST":
        return handle_device_request(request)
    from django.http import HttpResponse
    return HttpResponse(
        "LPS attendance server is up. Point the terminal's server address here.\n",
        content_type="text/plain")
