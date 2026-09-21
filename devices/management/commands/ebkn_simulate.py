"""
Pretend to be a terminal so you can exercise the whole pipeline with no hardware.

    python manage.py ebkn_simulate --serial ENS2025041 --user 1 --punch
    python manage.py ebkn_simulate --serial ENS2025041 --poll

Useful tonight, before the device is in front of you.
"""
import json
import struct
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


def frame(payload):
    head = json.dumps(payload, separators=(",", ":")).encode() + b"\x00"
    return struct.pack("<I", len(head)) + head


def post(url, headers, body):
    """
    POST one protocol frame.

    Connection problems are reported in one readable line. A forty-line stack
    trace for "the server is not running" reads like a fault in the software,
    which is the last thing you need while you are standing at a terminal
    wondering whether the device or the server is at fault.
    """
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/octet-stream")
    for key, value in headers.items():
        request.add_header(key, str(value))
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        # The server answered, just not with a 2xx. That is still useful.
        return exc.code, dict(exc.headers), exc.read()
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise CommandError(
            f"Could not reach {url}\n"
            f"  {reason}\n\n"
            f"  Nothing is listening there. Check that the server is running:\n"
            f"    python manage.py runserver 0.0.0.0:8000\n"
            f"  Run it in one terminal tab and this simulator in another — the\n"
            f"  simulator is an HTTP client and needs the server already up.\n"
            f"  Pointing at another machine? Pass --url http://<ip>:8000/ebkn/"
        ) from None
    except TimeoutError:
        raise CommandError(
            f"{url} accepted the connection but never answered.\n"
            f"  Something is listening on that port, but it is not this server."
        ) from None


class Command(BaseCommand):
    help = "Send fake EBKN protocol requests at this server."

    def add_arguments(self, parser):
        parser.add_argument("--url", default="http://127.0.0.1:8000/ebkn/")
        parser.add_argument("--serial", default="SIM001")
        parser.add_argument("--user", default="1", help="device user id")
        parser.add_argument("--poll", action="store_true", help="send receive_cmd")
        parser.add_argument("--punch", action="store_true", help="send realtime_glog")
        parser.add_argument("--enroll", action="store_true", help="send realtime_enroll_data")
        parser.add_argument("--at", default="", help="punch time as YYYYMMDDHHMMSS")

    def handle(self, *args, **options):
        url, serial = options["url"], options["serial"]

        from devices.models import Device
        if not Device.objects.filter(serial_number=serial).exists():
            self.stdout.write(self.style.WARNING(
                f"Note: {serial} is not registered. The server will log it under "
                f"Devices > Discovered rather than storing punches."))

        if options["poll"] or not (options["punch"] or options["enroll"]):
            status, headers, body = post(url, {
                "dev_id": serial, "request_code": "receive_cmd",
            }, frame({
                "fk_name": f"SIM-{serial}",
                "fk_time": timezone.localtime().strftime("%y%m%d%H%M%S"),
                "fk_info": {"supported_enroll_data": ["FACE", "FP", "CARD"],
                            "fk_bin_data_lib": "FKDataHS001", "firmware": "sim-1.0"},
            }))
            self.stdout.write(f"receive_cmd -> {status} {headers.get('cmd_code', 'no command')}")
            if headers.get("cmd_code"):
                self.stdout.write(f"  body: {body[4:].rstrip(chr(0).encode())!r}")
                post(url, {"dev_id": serial, "request_code": "send_cmd_result",
                           "trans_id": headers.get("trans_id"), "cmd_return_code": "OK",
                           "blk_no": "0"}, frame({"status": "ok"}))
                self.stdout.write("  reported result back")

        if options["punch"]:
            when = options["at"] or timezone.localtime().strftime("%Y%m%d%H%M%S")
            status, _h, _b = post(url, {"dev_id": serial, "request_code": "realtime_glog"},
                                  frame({"user_id": options["user"], "verify_mode": "FACE",
                                         "io_mode": 1, "io_time": when}))
            self.stdout.write(f"realtime_glog user={options['user']} at {when} -> {status}")

        if options["enroll"]:
            status, _h, _b = post(url, {"dev_id": serial, "request_code": "realtime_enroll_data"},
                                  frame({"user_id": options["user"],
                                         "user_name": f"Sim user {options['user']}",
                                         "user_privilege": "USER",
                                         "enroll_data_array": [{"backup_number": 0}]}))
            self.stdout.write(f"realtime_enroll_data -> {status}")
