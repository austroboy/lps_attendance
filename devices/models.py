from django.db import models
from django.utils import timezone


class DevicePurpose(models.TextChoices):
    STUDENT = "STUDENT", "Student attendance"
    TEACHER = "TEACHER", "Teacher attendance"
    BOTH = "BOTH", "Both"


class Device(models.Model):
    """One physical terminal. `serial_number` is the dev_id the device sends."""
    serial_number = models.CharField(
        max_length=64, unique=True, db_index=True,
        help_text="The dev_id the terminal sends. Usually the serial printed on the side.")
    name = models.CharField(max_length=80, help_text="e.g. Main Gate")
    location = models.CharField(max_length=120, blank=True)
    purpose = models.CharField(max_length=10, choices=DevicePurpose.choices,
                               default=DevicePurpose.STUDENT)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Filled in automatically from the first receive_cmd the device sends.
    fk_name = models.CharField(max_length=64, blank=True)
    firmware = models.CharField(max_length=64, blank=True)
    fk_bin_data_lib = models.CharField(max_length=64, blank=True)
    supported_enroll_data = models.JSONField(default=list, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_device_time = models.CharField(max_length=20, blank=True)
    punch_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "serial_number"]

    def __str__(self):
        return f"{self.name} [{self.serial_number}]"

    @property
    def is_online(self):
        if not self.last_seen:
            return False
        return (timezone.now() - self.last_seen).total_seconds() < 90

    @property
    def status_label(self):
        if not self.is_active:
            return "Disabled"
        return "Online" if self.is_online else "Offline"


class UnknownDevice(models.Model):
    """
    A terminal that contacted us with a dev_id we have never registered.

    This is the screen you use on day one: power a device up, point it at this
    server, and its real dev_id shows up here. One click turns it into a Device.
    """
    serial_number = models.CharField(max_length=64, unique=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    hit_count = models.PositiveIntegerField(default=1)
    last_request_code = models.CharField(max_length=40, blank=True)
    last_payload = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen"]

    def __str__(self):
        return self.serial_number


class CommandStatus(models.TextChoices):
    WAIT = "WAIT", "Waiting"
    RUN = "RUN", "Sent to device"
    DONE = "DONE", "Completed"
    ERROR = "ERROR", "Failed"


class DeviceCommand(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="commands")
    cmd_code = models.CharField(max_length=40)
    cmd_param = models.JSONField(default=dict, blank=True)
    cmd_binary = models.BinaryField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=CommandStatus.choices,
                              default=CommandStatus.WAIT, db_index=True)
    return_code = models.CharField(max_length=64, blank=True)
    result_json = models.JSONField(null=True, blank=True)
    result_binary = models.BinaryField(null=True, blank=True)
    created_by = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.cmd_code} #{self.pk} ({self.status})"


class CommandBlock(models.Model):
    """Reassembly buffer: results bigger than 8 KB arrive in numbered fragments."""
    command = models.ForeignKey(DeviceCommand, on_delete=models.CASCADE, related_name="blocks")
    blk_no = models.IntegerField()
    data = models.BinaryField()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["blk_no"]
        unique_together = [("command", "blk_no")]


class Punch(models.Model):
    """
    One raw verification event straight off a terminal.

    Nothing here is interpreted yet — no late/absent logic, no class mapping.
    The attendance engine reads unprocessed rows and turns them into records,
    so raw history survives even if you change the timetable later.
    """
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="punches")
    device_user_id = models.CharField(max_length=32, db_index=True)
    punch_time = models.DateTimeField(db_index=True)
    verify_mode = models.CharField(max_length=24, blank=True,
                                   help_text="FACE, FP, CARD, PASSWORD ...")
    io_mode = models.CharField(max_length=16, blank=True, help_text="Device in/out flag")
    image = models.ImageField(upload_to="punches/%Y/%m/", null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False, db_index=True)
    process_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-punch_time"]
        unique_together = [("device", "device_user_id", "punch_time")]
        indexes = [models.Index(fields=["processed", "punch_time"])]

    def __str__(self):
        return f"{self.device_user_id} @ {self.punch_time:%Y-%m-%d %H:%M:%S}"


class DeviceEnrollment(models.Model):
    """Mirror of a user record that lives on a terminal."""
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="enrollments")
    device_user_id = models.CharField(max_length=32)
    user_name = models.CharField(max_length=120, blank=True)
    privilege = models.CharField(max_length=24, blank=True)
    photo = models.ImageField(upload_to="enroll/", null=True, blank=True)
    backup_numbers = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("device", "device_user_id")]
        ordering = ["device", "device_user_id"]

    def __str__(self):
        return f"{self.device_user_id} on {self.device.serial_number}"


class TrafficLog(models.Model):
    """Raw wire log. Priceless while you are still guessing at the protocol."""
    DIRECTION = [("IN", "Device -> server"), ("OUT", "Server -> device")]

    direction = models.CharField(max_length=3, choices=DIRECTION)
    dev_id = models.CharField(max_length=64, blank=True, db_index=True)
    request_code = models.CharField(max_length=40, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    body_preview = models.TextField(blank=True)
    body_size = models.IntegerField(default=0)
    hex_preview = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.direction} {self.dev_id} {self.request_code}"
