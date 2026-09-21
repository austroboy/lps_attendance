"""
Everything that happens when a terminal POSTs to us.

Entry point is `handle_device_request(request)`. It is deliberately tolerant:
an unregistered device is recorded rather than rejected, an unreadable body is
logged rather than swallowed, and we always answer 200 OK so a device never
gets stuck retrying a punch it already delivered.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone

from .ebkn import protocol as p
from .models import (
    CommandBlock,
    CommandStatus,
    Device,
    DeviceCommand,
    DeviceEnrollment,
    Punch,
    TrafficLog,
    UnknownDevice,
)

log = logging.getLogger("ebkn")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _log_traffic(direction, dev_id, request_code, headers, body: bytes):
    """
    Keep a copy of the wire traffic.

    This is diagnostics. It must never be the reason a punch is lost, so any
    failure here is logged and swallowed — the first production version wrote
    this row before doing anything else, and a NUL byte PostgreSQL would not
    store turned every device request into a 500.
    """
    if not getattr(settings, "EBKN_TRAFFIC_LOG", True):
        return
    try:
        preview = p.clean_text(body[:1500].decode("utf-8", errors="replace"),
                               marker=p.NUL_MARKER) if body else ""
        TrafficLog.objects.create(
            direction=direction,
            dev_id=p.clean_text(dev_id, 64),
            request_code=p.clean_text(request_code, 40),
            headers=p.scrub(headers or {}),
            body_preview=preview,
            body_size=len(body or b""),
            hex_preview=binascii.hexlify(body[:120]).decode() if body else "",
        )
        keep = getattr(settings, "EBKN_TRAFFIC_KEEP", 2000)
        if keep and TrafficLog.objects.count() > keep * 1.2:
            cutoff = TrafficLog.objects.values_list("id", flat=True)[keep:keep + 1]
            if cutoff:
                TrafficLog.objects.filter(id__lt=list(cutoff)[0]).delete()
    except Exception:  # noqa: BLE001 - diagnostics never outrank a punch
        log.exception("could not write traffic log row for %s", dev_id)


def _response(payload=None, binaries=None, response_code="OK", extra=None, status=200):
    body = p.build_body(payload, binaries) if (payload is not None or binaries) else b""
    resp = HttpResponse(body, content_type="application/octet-stream", status=status)
    resp[p.HEADER_RESPONSE_CODE] = response_code
    resp["blk_no"] = "0"
    resp["blk_len"] = str(len(body))
    for key, value in (extra or {}).items():
        resp[key] = str(value)
    _log_traffic("OUT", (extra or {}).get(p.HEADER_DEV_ID, ""), "", dict(resp.items()), body)
    return resp


def _aware(parts):
    naive = dt.datetime(*parts)
    tz = timezone.get_current_timezone()
    return timezone.make_aware(naive, tz) if timezone.is_naive(naive) else naive


def _maybe_bytes(parsed: p.ParsedBody, value):
    """A JSON field may be a BIN_n reference or an inline base64 string."""
    if not value:
        return None
    blob = parsed.binary(value)
    if blob:
        return blob
    if isinstance(value, str) and len(value) > 32 and not value.startswith("BIN_"):
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def handle_device_request(request) -> HttpResponse:
    headers = {k: p.clean_text(v) for k, v in p.header_map(request).items()}
    dev_id = headers.get(p.HEADER_DEV_ID, "")
    request_code = headers.get(p.HEADER_REQUEST_CODE, "")
    raw = request.body or b""

    _log_traffic("IN", dev_id, request_code, headers, raw)

    if not dev_id:
        return _response(response_code="ERROR", extra={"error": "missing dev_id"})

    try:
        parsed = p.parse_body(raw)
    except p.ProtocolError as exc:
        log.warning("unparseable body from %s: %s", dev_id, exc)
        parsed = p.ParsedBody()
    # Binaries stay binary; everything textual is made safe for PostgreSQL once,
    # here, so no handler below can forget.
    parsed.data = p.scrub(parsed.data)

    device = Device.objects.filter(serial_number=dev_id).first()
    if device is None:
        _record_unknown(dev_id, request_code, parsed, _client_ip(request))
        if not getattr(settings, "EBKN_AUTO_DISCOVER", True):
            return _response(response_code="ERROR", extra={"error": "device not registered"})
        # Still answer OK so the terminal keeps talking while you register it.
        return _response(extra={p.HEADER_DEV_ID: dev_id})

    Device.objects.filter(pk=device.pk).update(
        last_seen=timezone.now(), ip_address=_client_ip(request) or device.ip_address)

    handlers = {
        p.RC_RECEIVE_CMD: _handle_receive_cmd,
        p.RC_SEND_CMD_RESULT: _handle_cmd_result,
        p.RC_REALTIME_GLOG: _handle_glog,
        p.RC_REALTIME_ENROLL: _handle_enroll,
    }
    handler = handlers.get(request_code)
    if handler is None:
        log.info("unhandled request_code=%r from %s", request_code, dev_id)
        return _response(extra={p.HEADER_DEV_ID: dev_id})

    try:
        return handler(device, headers, parsed)
    except Exception:  # pragma: no cover - never 500 at a terminal
        log.exception("handler %s blew up for %s", request_code, dev_id)
        return _response(response_code="ERROR", extra={p.HEADER_DEV_ID: dev_id})


def _record_unknown(dev_id, request_code, parsed, ip):
    payload = p.clean_text(json.dumps(parsed.data, ensure_ascii=False), 4000)
    obj, created = UnknownDevice.objects.get_or_create(
        serial_number=dev_id,
        defaults={"last_request_code": request_code, "last_payload": payload, "ip_address": ip},
    )
    if not created:
        UnknownDevice.objects.filter(pk=obj.pk).update(
            hit_count=obj.hit_count + 1,
            last_request_code=request_code,
            last_payload=payload,
            ip_address=ip,
            last_seen=timezone.now(),
        )
    log.info("unregistered terminal %s (%s) — see Devices > Discovered", dev_id, request_code)


# ---------------------------------------------------------------------------
# request handlers
# ---------------------------------------------------------------------------

def _handle_receive_cmd(device: Device, headers, parsed: p.ParsedBody):
    """Device polling for work. Also the only place it tells us who it is."""
    info = parsed.data.get("fk_info") or {}
    updates = {}
    if parsed.data.get("fk_name") and parsed.data["fk_name"] != device.fk_name:
        updates["fk_name"] = p.clean_text(parsed.data["fk_name"], 64)
    if info.get("firmware"):
        updates["firmware"] = p.clean_text(info["firmware"], 64)
    if info.get("fk_bin_data_lib"):
        updates["fk_bin_data_lib"] = p.clean_text(info["fk_bin_data_lib"], 64)
    if info.get("supported_enroll_data"):
        updates["supported_enroll_data"] = info["supported_enroll_data"]
    if parsed.data.get("fk_time"):
        updates["last_device_time"] = p.clean_text(parsed.data["fk_time"], 20)
    if updates:
        Device.objects.filter(pk=device.pk).update(**updates)

    if not device.is_active:
        return _response(extra={p.HEADER_DEV_ID: device.serial_number})

    command = (DeviceCommand.objects
               .filter(device=device, status=CommandStatus.WAIT)
               .order_by("created_at")
               .first())
    if command is None:
        return _response(extra={p.HEADER_DEV_ID: device.serial_number})

    DeviceCommand.objects.filter(pk=command.pk).update(
        status=CommandStatus.RUN, sent_at=timezone.now())

    binaries = [bytes(command.cmd_binary)] if command.cmd_binary else None
    return _response(
        payload=command.cmd_param or {},
        binaries=binaries,
        extra={
            p.HEADER_TRANS_ID: command.pk,
            p.HEADER_CMD_CODE: command.cmd_code,
            p.HEADER_DEV_ID: device.serial_number,
        },
    )


def _handle_cmd_result(device: Device, headers, parsed: p.ParsedBody):
    """Result of a queued command, possibly split across several blocks."""
    trans_id = headers.get(p.HEADER_TRANS_ID)
    return_code = headers.get(p.HEADER_CMD_RETURN_CODE, "")
    try:
        blk_no = int(headers.get(p.HEADER_BLK_NO, "0") or 0)
    except ValueError:
        blk_no = 0

    command = DeviceCommand.objects.filter(pk=trans_id, device=device).first() if trans_id else None
    if command is None:
        log.warning("result for unknown trans_id=%r from %s", trans_id, device.serial_number)
        return _response(extra={p.HEADER_DEV_ID: device.serial_number})

    payload_bytes = json.dumps(parsed.data, ensure_ascii=False).encode() if parsed.data else b""
    blob = parsed.binaries[0] if parsed.binaries else payload_bytes

    if blk_no != 0:
        CommandBlock.objects.update_or_create(
            command=command, blk_no=blk_no, defaults={"data": blob})
        return _response(extra={p.HEADER_TRANS_ID: command.pk,
                                p.HEADER_DEV_ID: device.serial_number})

    # blk_no == 0: final block. Stitch everything together.
    pieces = list(CommandBlock.objects.filter(command=command).order_by("blk_no"))
    assembled = b"".join(bytes(piece.data) for piece in pieces) + blob
    CommandBlock.objects.filter(command=command).delete()

    result = parsed.data
    if pieces:
        try:
            result = p.scrub(json.loads(
                assembled.rstrip(b"\x00").decode("utf-8", errors="replace")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            result = {"_raw_size": len(assembled)}

    ok = (return_code or "OK").upper() == "OK"
    DeviceCommand.objects.filter(pk=command.pk).update(
        status=CommandStatus.DONE if ok else CommandStatus.ERROR,
        return_code=p.clean_text(return_code, 64),
        result_json=result if isinstance(result, (dict, list)) else {"value": result},
        finished_at=timezone.now(),
    )
    _apply_command_result(device, command.cmd_code, result)
    return _response(extra={p.HEADER_TRANS_ID: command.pk,
                            p.HEADER_DEV_ID: device.serial_number})


def _apply_command_result(device, cmd_code, result):
    """Fold useful command output back into our own tables."""
    from .ebkn import commands as C

    if cmd_code == C.GET_LOG_DATA and isinstance(result, (list, dict)):
        rows = result if isinstance(result, list) else result.get("log_data") or result.get("data") or []
        created = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            parts = p.parse_device_time(row.get("io_time") or row.get("time"))
            uid = str(row.get("user_id") or "").strip()
            if not parts or not uid:
                continue
            if _save_punch(device, uid, _aware(parts),
                           row.get("verify_mode", ""), row.get("io_mode", ""), row):
                created += 1
        log.info("GET_LOG_DATA from %s: %s new punches", device.serial_number, created)

    elif cmd_code == C.GET_USER_ID_LIST and isinstance(result, (list, dict)):
        ids = result if isinstance(result, list) else result.get("user_id_array") or []
        for uid in ids or []:
            uid = str(uid.get("user_id") if isinstance(uid, dict) else uid).strip()
            if uid:
                DeviceEnrollment.objects.get_or_create(device=device, device_user_id=uid)


def _save_punch(device, uid, when, verify_mode, io_mode, raw, image_bytes=None):
    try:
        with transaction.atomic():
            punch = Punch.objects.create(
                device=device,
                device_user_id=p.clean_text(uid, 32),
                punch_time=when,
                verify_mode=p.clean_text(verify_mode, 24),
                io_mode=p.clean_text(io_mode, 16),
                raw=p.scrub(raw) if isinstance(raw, dict) else {},
            )
    except IntegrityError:
        return None  # already have this exact punch
    if image_bytes:
        name = f"{device.serial_number}_{uid}_{when:%Y%m%d%H%M%S}.jpg"
        punch.image.save(name, ContentFile(image_bytes), save=True)
    Device.objects.filter(pk=device.pk).update(punch_count=device.punch_count + 1)
    return punch


def _handle_glog(device: Device, headers, parsed: p.ParsedBody):
    """A live punch. This is the one that matters day to day."""
    data = parsed.data
    uid = str(data.get("user_id") or "").strip()
    parts = p.parse_device_time(data.get("io_time"))
    if not uid or not parts:
        log.warning("glog without user_id/io_time from %s: %s", device.serial_number, data)
        return _response(extra={p.HEADER_DEV_ID: device.serial_number})

    image = _maybe_bytes(parsed, data.get("log_image"))
    punch = _save_punch(device, uid, _aware(parts),
                        data.get("verify_mode", ""), data.get("io_mode", ""), data, image)

    if punch:
        # Resolve it immediately so the dashboard is live, not batch-delayed.
        try:
            from attendance.services import process_punch
            process_punch(punch)
        except Exception:  # pragma: no cover
            log.exception("attendance processing failed for punch %s", punch.pk)

    return _response(extra={p.HEADER_DEV_ID: device.serial_number})


def _handle_enroll(device: Device, headers, parsed: p.ParsedBody):
    """Someone was just enrolled on the terminal itself."""
    data = parsed.data
    uid = str(data.get("user_id") or "").strip()
    if not uid:
        return _response(extra={p.HEADER_DEV_ID: device.serial_number})

    backups = [b.get("backup_number") for b in (data.get("enroll_data_array") or [])
               if isinstance(b, dict)]
    enrollment, _ = DeviceEnrollment.objects.update_or_create(
        device=device, device_user_id=uid,
        defaults={
            "user_name": p.clean_text(data.get("user_name"), 120),
            "privilege": p.clean_text(data.get("user_privilege"), 24),
            "backup_numbers": backups,
        },
    )
    photo = _maybe_bytes(parsed, data.get("user_photo"))
    if photo:
        enrollment.photo.save(f"{device.serial_number}_{uid}.jpg", ContentFile(photo), save=True)

    # The terminal has just stored biometrics for this user id. If that id
    # belongs to a student, their face column can tick over immediately —
    # which is the whole point of standing at the machine with the roster open
    # on a phone.
    try:
        from academics.roster import describe_backups, student_for_device_user

        student = student_for_device_user(uid)
        if student is not None:
            student.mark_face_enrolled(device=device, detail=describe_backups(backups))
    except Exception:  # pragma: no cover - never break the device reply
        log.exception("could not mark face enrolment for user %s", uid)

    return _response(extra={p.HEADER_DEV_ID: device.serial_number})


# ---------------------------------------------------------------------------
# queueing helper used by the UI
# ---------------------------------------------------------------------------

def queue_command(device, cmd_code, params=None, created_by="", binary=None):
    return DeviceCommand.objects.create(
        device=device,
        cmd_code=cmd_code,
        cmd_param=params or {},
        cmd_binary=binary,
        created_by=str(created_by)[:80],
    )
