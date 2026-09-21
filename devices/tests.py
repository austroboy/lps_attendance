"""
End-to-end tests for the terminal protocol and the attendance engine.

Run with:  python manage.py test

These use Django's test client, which does not strip underscore headers. The
real dev server does — see devices/apps.py for the patch that stops it, and
test_underscore_headers_are_not_stripped below for the guard.
"""
import datetime as dt
import json
import struct

from django.test import Client, TestCase
from django.utils import timezone

from academics.models import SchoolClass, Section, Student, Teacher
from attendance.models import (
    AttendanceRecord,
    AttendanceStatus,
    SlotSection,
    SlotTeacher,
    SlotType,
    TimeSlot,
)
from attendance.services import close_slots, reprocess_day
from devices.ebkn import protocol as p
from devices.models import Device, DeviceCommand, DeviceEnrollment, Punch, UnknownDevice


def frame(payload, binaries=()):
    out = bytearray()
    head = json.dumps(payload, separators=(",", ":")).encode() + b"\x00"
    out += struct.pack("<I", len(head)) + head
    for blob in binaries:
        out += struct.pack("<I", len(blob)) + blob
    return bytes(out)


class ProtocolCodecTests(TestCase):
    def test_round_trip(self):
        body = p.build_body({"user_id": "7"}, [b"\x01\x02\x03"])
        parsed = p.parse_body(body)
        self.assertEqual(parsed.data["user_id"], "7")
        self.assertEqual(parsed.binaries, [b"\x01\x02\x03"])

    def test_bin_reference_resolves(self):
        parsed = p.parse_body(frame({"log_image": "BIN_1"}, [b"jpegbytes"]))
        self.assertEqual(parsed.binary("BIN_1"), b"jpegbytes")
        self.assertIsNone(parsed.binary("BIN_9"))

    def test_unframed_json_is_accepted(self):
        parsed = p.parse_body(b'{"user_id":"3"}')
        self.assertFalse(parsed.framed)
        self.assertEqual(parsed.data["user_id"], "3")

    def test_empty_body(self):
        self.assertEqual(p.parse_body(b"").data, {})

    def test_device_time_formats(self):
        self.assertEqual(p.parse_device_time("20260914074500"),
                         (2026, 9, 14, 7, 45, 0))
        self.assertEqual(p.parse_device_time("260914074500"),
                         (2026, 9, 14, 7, 45, 0))
        self.assertIsNone(p.parse_device_time("nonsense"))

    def test_split_blocks_numbers_last_as_zero(self):
        chunks = list(p.split_blocks(b"x" * 20000, size=8192))
        self.assertEqual([n for n, _ in chunks], [1, 2, 0])


class DeviceTrafficTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.device = Device.objects.create(serial_number="SIM001", name="Gate",
                                            purpose="BOTH")

    def post(self, serial, request_code, payload=None, binaries=(), **extra):
        headers = {"dev_id": serial, "request_code": request_code, **extra}
        return self.client.post(
            "/ebkn/", data=frame(payload or {}, binaries),
            content_type="application/octet-stream", headers=headers)

    def test_poll_records_device_details(self):
        response = self.post("SIM001", "receive_cmd", {
            "fk_name": "FK725HS001", "fk_time": "260914074500",
            "fk_info": {"firmware": "v2.0", "fk_bin_data_lib": "FKDataHS001",
                        "supported_enroll_data": ["FACE", "FP"]},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["response_code"], "OK")
        self.device.refresh_from_db()
        self.assertEqual(self.device.fk_name, "FK725HS001")
        self.assertEqual(self.device.firmware, "v2.0")
        self.assertIsNotNone(self.device.last_seen)

    def test_queued_command_is_handed_over_then_completed(self):
        command = DeviceCommand.objects.create(
            device=self.device, cmd_code="SET_TIME",
            cmd_param={"time": "20260914074500"})

        response = self.post("SIM001", "receive_cmd", {"fk_name": "x"})
        self.assertEqual(response["cmd_code"], "SET_TIME")
        self.assertEqual(response["trans_id"], str(command.pk))
        returned = p.parse_body(response.content)
        self.assertEqual(returned.data["time"], "20260914074500")

        command.refresh_from_db()
        self.assertEqual(command.status, "RUN")

        self.post("SIM001", "send_cmd_result", {"status": "ok"},
                  trans_id=str(command.pk), cmd_return_code="OK", blk_no="0")
        command.refresh_from_db()
        self.assertEqual(command.status, "DONE")

    def test_fragmented_result_is_reassembled(self):
        command = DeviceCommand.objects.create(device=self.device, cmd_code="GET_LOG_DATA")
        big = json.dumps([{"user_id": str(i)} for i in range(400)]).encode()
        first, second = big[:5000], big[5000:]

        self.client.post("/ebkn/", data=struct.pack("<I", len(first)) + first,
                         content_type="application/octet-stream",
                         headers={"dev_id": "SIM001", "request_code": "send_cmd_result",
                                  "trans_id": str(command.pk), "blk_no": "1",
                                  "cmd_return_code": "OK"})
        self.client.post("/ebkn/", data=struct.pack("<I", len(second)) + second,
                         content_type="application/octet-stream",
                         headers={"dev_id": "SIM001", "request_code": "send_cmd_result",
                                  "trans_id": str(command.pk), "blk_no": "0",
                                  "cmd_return_code": "OK"})
        command.refresh_from_db()
        self.assertEqual(command.status, "DONE")
        self.assertEqual(len(command.result_json), 400)

    def test_unknown_device_is_recorded_not_rejected(self):
        response = self.post("ENS2025041", "receive_cmd", {"fk_name": "mystery"})
        self.assertEqual(response.status_code, 200)
        unknown = UnknownDevice.objects.get(serial_number="ENS2025041")
        self.assertIn("mystery", unknown.last_payload)

        self.post("ENS2025041", "receive_cmd", {"fk_name": "mystery"})
        unknown.refresh_from_db()
        self.assertEqual(unknown.hit_count, 2)

    def test_duplicate_punch_is_ignored(self):
        payload = {"user_id": "101", "verify_mode": "FACE", "io_mode": 1,
                   "io_time": "20260914074500"}
        self.post("SIM001", "realtime_glog", payload)
        self.post("SIM001", "realtime_glog", payload)
        self.assertEqual(Punch.objects.count(), 1)

    def test_enrolment_push_is_stored(self):
        self.post("SIM001", "realtime_enroll_data", {
            "user_id": "555", "user_name": "Rahim", "user_privilege": "USER",
            "enroll_data_array": [{"backup_number": 0}, {"backup_number": 1}],
        })
        enrolment = DeviceEnrollment.objects.get(device_user_id="555")
        self.assertEqual(enrolment.user_name, "Rahim")
        self.assertEqual(enrolment.backup_numbers, [0, 1])

    def test_missing_dev_id_is_refused_quietly(self):
        response = self.client.post(
            "/ebkn/", data=frame({}), content_type="application/octet-stream",
            headers={"request_code": "realtime_glog"})
        self.assertEqual(response["response_code"], "ERROR")

    def test_underscore_headers_are_not_stripped(self):
        """
        Guard for the trap that costs a whole day of commissioning.

        Django's runserver, nginx and Apache 2.4+ all delete headers containing
        an underscore unless told otherwise. Every header this protocol uses
        has one.
        """
        from django.core.servers.basehttp import WSGIRequestHandler
        self.assertTrue(getattr(WSGIRequestHandler, "_ebkn_patched", False),
                        "The runserver underscore-header patch is not applied. "
                        "No terminal will ever be identified.")


class AttendanceEngineTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.device = Device.objects.create(serial_number="SIM001", name="Gate",
                                            purpose="BOTH")
        school_class = SchoolClass.objects.create(name="Class Six", order=6)
        self.section = Section.objects.create(school_class=school_class, name="A")
        self.on_time = Student.objects.create(
            admission_no="A1", roll_no="1", full_name="Early Student",
            section=self.section, device_user_id="101", guardian_phone="01712345678")
        self.latecomer = Student.objects.create(
            admission_no="A2", roll_no="2", full_name="Late Student",
            section=self.section, device_user_id="102", guardian_phone="01712345679")
        self.absentee = Student.objects.create(
            admission_no="A3", roll_no="3", full_name="Missing Student",
            section=self.section, device_user_id="103", guardian_phone="01712345670")

        self.slot = TimeSlot.objects.create(
            name="Morning entry", slot_type=SlotType.STUDENT,
            start_time=dt.time(7, 0), late_time=dt.time(8, 0), end_time=dt.time(9, 30),
            out_start_time=dt.time(13, 30), out_end_time=dt.time(15, 0))
        SlotSection.objects.create(time_slot=self.slot, section=self.section)

        self.today = timezone.localdate()
        self.stamp = self.today.strftime("%Y%m%d")

    def punch(self, uid, hhmmss, serial="SIM001"):
        return self.client.post(
            "/ebkn/",
            data=frame({"user_id": uid, "verify_mode": "FACE", "io_mode": 1,
                        "io_time": f"{self.stamp}{hhmmss}"}),
            content_type="application/octet-stream",
            headers={"dev_id": serial, "request_code": "realtime_glog"})

    def test_punch_before_cutoff_is_present(self):
        self.punch("101", "074500")
        record = AttendanceRecord.objects.get(student=self.on_time)
        self.assertEqual(record.status, AttendanceStatus.PRESENT)
        self.assertEqual(record.time_slot, self.slot)
        self.assertEqual(record.minutes_late, 0)

    def test_punch_after_cutoff_is_late_with_minutes(self):
        self.punch("102", "082000")
        record = AttendanceRecord.objects.get(student=self.latecomer)
        self.assertEqual(record.status, AttendanceStatus.LATE)
        self.assertEqual(record.minutes_late, 20)

    def test_second_punch_records_a_leaving_time(self):
        self.punch("101", "074500")
        self.punch("101", "140000")
        record = AttendanceRecord.objects.get(student=self.on_time)
        self.assertEqual(record.status, AttendanceStatus.PRESENT)
        self.assertIsNotNone(record.out_time)
        self.assertEqual(timezone.localtime(record.out_time).hour, 14)

    def test_earlier_punch_wins_and_can_rescue_a_late_mark(self):
        self.punch("101", "082000")
        self.assertEqual(AttendanceRecord.objects.get(student=self.on_time).status,
                         AttendanceStatus.LATE)
        self.punch("101", "071000")
        record = AttendanceRecord.objects.get(student=self.on_time)
        self.assertEqual(record.status, AttendanceStatus.PRESENT)

    def test_unknown_device_user_leaves_a_readable_note(self):
        self.punch("99999", "074500")
        punch = Punch.objects.get(device_user_id="99999")
        self.assertTrue(punch.processed)
        self.assertIn("No student or teacher", punch.process_note)
        self.assertEqual(AttendanceRecord.objects.count(), 0)

    def test_closing_the_slot_marks_the_rest_absent(self):
        self.punch("101", "074500")
        self.punch("102", "082000")
        created = close_slots(self.today, now=dt.time(10, 0))
        self.assertEqual(created["students"], 1)
        self.assertEqual(
            AttendanceRecord.objects.get(student=self.absentee).status,
            AttendanceStatus.ABSENT)

    def test_closing_is_safe_to_run_twice(self):
        close_slots(self.today, now=dt.time(10, 0))
        close_slots(self.today, now=dt.time(10, 0))
        self.assertEqual(AttendanceRecord.objects.filter(status="ABSENT").count(), 3)

    def test_slot_that_has_not_closed_is_left_alone(self):
        created = close_slots(self.today, now=dt.time(7, 30))
        self.assertEqual(created["students"], 0)

    def test_manual_edit_survives_a_rebuild(self):
        self.punch("101", "074500")
        record = AttendanceRecord.objects.get(student=self.on_time)
        record.status = AttendanceStatus.LEAVE
        record.is_manual = True
        record.save()

        reprocess_day(self.today)
        record.refresh_from_db()
        self.assertEqual(record.status, AttendanceStatus.LEAVE)

    def test_rebuild_applies_a_corrected_timetable(self):
        self.punch("101", "074500")
        self.assertEqual(AttendanceRecord.objects.get(student=self.on_time).status,
                         AttendanceStatus.PRESENT)

        # The office decides the cut-off was wrong: it should have been 07:30.
        self.slot.late_time = dt.time(7, 30)
        self.slot.save()
        reprocess_day(self.today)

        self.assertEqual(AttendanceRecord.objects.get(student=self.on_time).status,
                         AttendanceStatus.LATE)

    def test_teacher_punch_uses_the_teacher_timetable(self):
        teacher = Teacher.objects.create(employee_code="T-001", full_name="Nasrin",
                                         device_user_id="9001", phone="01712345678")
        staff_slot = TimeSlot.objects.create(
            name="Staff sign-in", slot_type=SlotType.TEACHER,
            start_time=dt.time(6, 30), late_time=dt.time(7, 45), end_time=dt.time(10, 0))
        SlotTeacher.objects.create(time_slot=staff_slot, teacher=teacher)

        self.punch("9001", "070500")
        record = teacher.attendance.get()
        self.assertEqual(record.status, AttendanceStatus.PRESENT)
        self.assertEqual(record.time_slot, staff_slot)

    def test_slot_only_runs_on_its_chosen_days(self):
        self.slot.days = [(self.today.weekday() + 1) % 7]
        self.slot.save()
        self.punch("101", "074500")
        self.assertEqual(AttendanceRecord.objects.count(), 0)
        self.assertIn("No time slot", Punch.objects.get().process_note)


class SmsTests(TestCase):
    def test_number_normalising(self):
        from smsapp.gateway import normalise_msisdn
        self.assertEqual(normalise_msisdn("01712345678"), "8801712345678")
        self.assertEqual(normalise_msisdn("8801712345678"), "8801712345678")
        self.assertEqual(normalise_msisdn("+880 1712-345678"), "8801712345678")
        self.assertIsNone(normalise_msisdn("12345"))
        self.assertIsNone(normalise_msisdn(""))

    def test_template_rendering(self):
        from smsapp.models import SmsTemplate
        template = SmsTemplate(body="Dear {guardian}, {name} was {status} on {date}.")
        rendered = template.render({"guardian": "Karim", "name": "Rahim",
                                    "status": "Absent", "date": "14-09-2026"})
        self.assertEqual(rendered, "Dear Karim, Rahim was Absent on 14-09-2026.")

    def test_absent_schedule_picks_only_absentees(self):
        import datetime
        from smsapp.models import Audience, SmsSchedule, SmsScheduleSection, SmsTemplate
        from smsapp.services import build_messages

        school_class = SchoolClass.objects.create(name="Class Six", order=6)
        section = Section.objects.create(school_class=school_class, name="A")
        present = Student.objects.create(admission_no="B1", full_name="Here",
                                         section=section, guardian_phone="01711111111")
        away = Student.objects.create(admission_no="B2", full_name="Away",
                                      section=section, guardian_phone="01722222222")
        slot = TimeSlot.objects.create(name="Morning", start_time=datetime.time(7, 0),
                                       late_time=datetime.time(8, 0),
                                       end_time=datetime.time(9, 0))
        today = timezone.localdate()
        AttendanceRecord.objects.create(date=today, student=present, time_slot=slot,
                                        status=AttendanceStatus.PRESENT)
        AttendanceRecord.objects.create(date=today, student=away, time_slot=slot,
                                        status=AttendanceStatus.ABSENT)

        template = SmsTemplate.objects.create(name="Absent", body="{name} was {status}")
        schedule = SmsSchedule.objects.create(
            name="Absent alert", audience=Audience.GUARDIAN, template=template,
            statuses=["ABSENT"], send_time=datetime.time(9, 30))
        SmsScheduleSection.objects.create(schedule=schedule, section=section)

        messages = build_messages(schedule, today)
        self.assertEqual(len(messages), 1)
        person, number, text = messages[0]
        self.assertEqual(person, away)
        self.assertEqual(number, "8801722222222")
        self.assertEqual(text, "Away was Absent")


class ProductionDatabaseTests(TestCase):
    """
    Guards for the things SQLite forgives and PostgreSQL does not.

    Every test in this class passed on SQLite before the fix and failed on
    PostgreSQL — which is the database production actually runs.
    """

    def setUp(self):
        self.client = Client()
        self.device = Device.objects.create(serial_number="SIM001", name="Gate",
                                            purpose="BOTH")

    def test_a_framed_body_full_of_nul_bytes_is_stored(self):
        # The length prefix and the JSON terminator are both zero bytes.
        response = self.client.post(
            "/ebkn/", data=frame({"user_id": "101", "io_time": "20260914074500"}),
            content_type="application/octet-stream",
            headers={"dev_id": "SIM001", "request_code": "realtime_glog"})
        self.assertEqual(response["response_code"], "OK")
        self.assertEqual(Punch.objects.count(), 1)

    def test_nul_padded_strings_from_c_firmware_are_trimmed(self):
        self.client.post("/ebkn/", data=frame({
            "user_id": "555\x00\x00", "user_name": "Rahim\x00\x00\x00\x00",
            "user_privilege": "USER\x00",
        }), content_type="application/octet-stream",
            headers={"dev_id": "SIM001", "request_code": "realtime_enroll_data"})
        enrolment = DeviceEnrollment.objects.get()
        self.assertEqual(enrolment.device_user_id, "555")
        self.assertEqual(enrolment.user_name, "Rahim")
        self.assertEqual(enrolment.privilege, "USER")

    def test_the_protocol_log_shows_where_the_nuls_were(self):
        from devices.models import TrafficLog

        self.client.post("/ebkn/", data=frame({"fk_name": "x"}),
                         content_type="application/octet-stream",
                         headers={"dev_id": "SIM001", "request_code": "receive_cmd"})
        row = TrafficLog.objects.filter(direction="IN").first()
        self.assertNotIn("\x00", row.body_preview)
        self.assertIn("\u2400", row.body_preview)

    def test_a_broken_traffic_log_cannot_cost_a_punch(self):
        from unittest import mock

        with mock.patch("devices.services.TrafficLog.objects.create",
                        side_effect=RuntimeError("disk full")):
            response = self.client.post(
                "/ebkn/", data=frame({"user_id": "7", "io_time": "20260914074500"}),
                content_type="application/octet-stream",
                headers={"dev_id": "SIM001", "request_code": "realtime_glog"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Punch.objects.count(), 1)

    def test_database_url_is_parsed_with_its_options(self):
        from config.settings import _database_from_url

        config = _database_from_url(
            "postgresql://lpsuser:p%40ss@127.0.0.1:5432/lpsdb?sslmode=disable")
        self.assertEqual(config["NAME"], "lpsdb")
        self.assertEqual(config["USER"], "lpsuser")
        self.assertEqual(config["PASSWORD"], "p@ss")
        self.assertEqual(config["PORT"], "5432")
        self.assertEqual(config["OPTIONS"], {"sslmode": "disable"})
