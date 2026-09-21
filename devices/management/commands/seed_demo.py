"""
Fill an empty database with enough to click around.

    python manage.py seed_demo

Creates an admin login, a few classes and students, two time slots with classes
already dropped in, a teacher, an SMS template and schedule, and one simulated
device. Nothing here touches real hardware — delete the database file and start
again whenever you like.
"""
import datetime as dt

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from academics.models import AcademicSession, ClassAccess, SchoolClass, Section, Student, Teacher
from accounts.models import Role, User
from attendance.models import SlotSection, SlotTeacher, SlotType, TimeSlot
from devices.models import Device, DevicePurpose
from smsapp.models import Audience, SmsSchedule, SmsScheduleSection, SmsTemplate

# Sunday to Thursday, the usual Bangladeshi school week.
SCHOOL_DAYS = [6, 0, 1, 2, 3]


class Command(BaseCommand):
    help = "Create demo data so the app can be explored before the devices arrive."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Allow running with DJANGO_DEBUG=False. Think twice.")

    @transaction.atomic
    def handle(self, *args, **options):
        from django.conf import settings
        from django.core.management.base import CommandError

        if not settings.DEBUG and not options["force"]:
            # It creates admin/admin123. On the live server that is a door left
            # open to the internet, so refuse unless someone really insists.
            raise CommandError(
                "seed_demo creates a login admin / admin123 and fake students. "
                "This looks like production (DJANGO_DEBUG=False), so it will not run. "
                "Use `python manage.py createsuperuser` instead.")

        if Student.objects.exists():
            self.stdout.write(self.style.WARNING(
                "There is already data here. Delete db.sqlite3 first if you want a clean demo."))
            return

        admin, _ = User.objects.get_or_create(
            username="admin",
            defaults={"role": Role.SUPERADMIN, "is_staff": True, "is_superuser": True,
                      "first_name": "School", "last_name": "Admin"})
        admin.set_password("admin123")
        admin.role = Role.SUPERADMIN
        admin.is_staff = admin.is_superuser = True
        admin.save()

        session = AcademicSession.objects.create(
            name=str(timezone.localdate().year),
            start_date=dt.date(timezone.localdate().year, 1, 1),
            end_date=dt.date(timezone.localdate().year, 12, 31))

        sections = []
        for order, name in enumerate(["Class Six", "Class Seven", "Class Eight"], start=6):
            school_class = SchoolClass.objects.create(name=name, order=order, session=session)
            for letter in ("A", "B"):
                sections.append(Section.objects.create(school_class=school_class, name=letter))

        device_uid = 100
        for section in sections:
            for roll in range(1, 9):
                device_uid += 1
                Student.objects.create(
                    admission_no=f"{session.name}-{device_uid}",
                    roll_no=str(roll),
                    full_name=f"Student {roll} of {section.school_class.name} {section.name}",
                    section=section,
                    guardian_name="Guardian",
                    guardian_phone=f"018{device_uid:08d}"[:11],
                    device_user_id=str(device_uid),
                )

        teacher_user = User.objects.create(username="T-001", role=Role.TEACHER,
                                           first_name="Class", last_name="Teacher")
        teacher_user.set_password("T-001")
        teacher_user.save()
        teacher = Teacher.objects.create(
            user=teacher_user, employee_code="T-001", full_name="Nasrin Akter",
            designation="Assistant teacher", phone="01712345678", device_user_id="9001")
        ClassAccess.objects.create(teacher=teacher, school_class=sections[0].school_class)

        morning = TimeSlot.objects.create(
            name="Morning entry", slot_type=SlotType.STUDENT,
            start_time=dt.time(7, 0), late_time=dt.time(8, 0), end_time=dt.time(9, 30),
            out_start_time=dt.time(13, 30), out_end_time=dt.time(15, 0),
            days=SCHOOL_DAYS, colour="#2f6f8f", order=1)
        afternoon = TimeSlot.objects.create(
            name="Afternoon session", slot_type=SlotType.STUDENT,
            start_time=dt.time(12, 30), late_time=dt.time(13, 0), end_time=dt.time(14, 0),
            days=SCHOOL_DAYS, colour="#7a5c3a", order=2)
        for section in sections:
            SlotSection.objects.create(time_slot=morning, section=section)
        for section in sections[:2]:
            SlotSection.objects.create(time_slot=afternoon, section=section)

        staff_slot = TimeSlot.objects.create(
            name="Staff sign-in", slot_type=SlotType.TEACHER,
            start_time=dt.time(6, 30), late_time=dt.time(7, 45), end_time=dt.time(10, 0),
            days=SCHOOL_DAYS, colour="#3f6b4a", order=1)
        SlotTeacher.objects.create(time_slot=staff_slot, teacher=teacher)

        template = SmsTemplate.objects.create(
            name="Absent notice",
            body="Dear {guardian}, {name} ({class}-{section}) was absent on {date}. - {school}")
        schedule = SmsSchedule.objects.create(
            name="Absent alert", audience=Audience.GUARDIAN, template=template,
            time_slot=morning, statuses=["ABSENT"], send_time=dt.time(9, 45),
            days=SCHOOL_DAYS, colour="#8a5a2b")
        for section in sections:
            SmsScheduleSection.objects.create(schedule=schedule, section=section)

        Device.objects.create(serial_number="SIM001", name="Simulated terminal",
                              location="Test bench", purpose=DevicePurpose.BOTH)

        self.stdout.write(self.style.SUCCESS(
            "Demo data ready.\n"
            "  admin / admin123      full access\n"
            "  T-001 / T-001         teacher, Class Six only\n"
            f"  {Student.objects.count()} students across {len(sections)} sections\n"
            "  device SIM001 registered — try:\n"
            "    python manage.py ebkn_simulate --serial SIM001 --user 101 --punch"))
