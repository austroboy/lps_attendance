"""
EBKN / FK-series push protocol codec.

How the terminal talks to us
----------------------------
The device is the HTTP *client*. It POSTs to a single URL over and over. Nothing
is ever initiated from our side; to make the device do something we park a
command in a queue and hand it back on the device's next poll.

Everything interesting lives in custom HTTP headers:

    dev_id           device serial / terminal id (this is what you register in the UI)
    request_code     receive_cmd | send_cmd_result | realtime_glog | realtime_enroll_data
    trans_id         transaction id of the command being answered
    cmd_code         command name (server -> device)
    blk_no           fragment number; 0 means "last / only block"
    cmd_return_code  OK, or an error string
    Content-Type     always application/octet-stream

Body framing
------------
The body is a sequence of length-prefixed blocks::

    <uint32 LE length><block bytes>
    <uint32 LE length><block bytes>
    ...

Block 0 is UTF-8 JSON terminated by a NUL byte. Any blocks after it are the raw
binaries the JSON refers to by name: "BIN_1", "BIN_2", ... (a face photo, a
fingerprint template, and so on).

Some firmware builds — and every simulator/curl test — send the bare JSON with
no framing at all, so the parser sniffs for a leading "{" and falls back.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field

# Request codes the terminal sends.
RC_RECEIVE_CMD = "receive_cmd"
RC_SEND_CMD_RESULT = "send_cmd_result"
RC_REALTIME_GLOG = "realtime_glog"
RC_REALTIME_ENROLL = "realtime_enroll_data"

KNOWN_REQUEST_CODES = {
    RC_RECEIVE_CMD,
    RC_SEND_CMD_RESULT,
    RC_REALTIME_GLOG,
    RC_REALTIME_ENROLL,
}

# Header names, lowercase. Django exposes them as HTTP_DEV_ID etc.
HEADER_DEV_ID = "dev_id"
HEADER_REQUEST_CODE = "request_code"
HEADER_TRANS_ID = "trans_id"
HEADER_CMD_CODE = "cmd_code"
HEADER_BLK_NO = "blk_no"
HEADER_CMD_RETURN_CODE = "cmd_return_code"
HEADER_RESPONSE_CODE = "response_code"

MAX_BLOCK = 8 * 1024


class ProtocolError(ValueError):
    """Raised when a body cannot be decoded as a protocol frame."""


@dataclass
class ParsedBody:
    data: dict = field(default_factory=dict)
    binaries: list[bytes] = field(default_factory=list)
    framed: bool = True

    def binary(self, ref):
        """Resolve a "BIN_n" reference from the JSON to actual bytes."""
        if not isinstance(ref, str) or not ref.startswith("BIN_"):
            return None
        try:
            index = int(ref.split("_", 1)[1]) - 1
        except (ValueError, IndexError):
            return None
        if 0 <= index < len(self.binaries):
            return self.binaries[index]
        return None


def parse_body(raw: bytes) -> ParsedBody:
    """Decode a device request body. Never raises on empty input."""
    if not raw:
        return ParsedBody(data={}, binaries=[], framed=False)

    stripped = raw.lstrip(b" \t\r\n")
    if stripped[:1] == b"{":
        # Unframed JSON (simulator, curl, some firmwares).
        text = stripped.rstrip(b"\x00").decode("utf-8", errors="replace")
        try:
            return ParsedBody(data=json.loads(text), binaries=[], framed=False)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"body looked like JSON but did not parse: {exc}") from exc

    blocks: list[bytes] = []
    offset = 0
    total = len(raw)
    while offset + 4 <= total:
        (length,) = struct.unpack_from("<I", raw, offset)
        offset += 4
        if length > total - offset:
            # Truncated / not really framed. Keep what we have.
            blocks.append(raw[offset:])
            offset = total
            break
        blocks.append(raw[offset:offset + length])
        offset += length

    if not blocks:
        raise ProtocolError("body is not a valid length-prefixed frame")

    head = blocks[0].rstrip(b"\x00")
    try:
        data = json.loads(head.decode("utf-8", errors="replace")) if head else {}
    except json.JSONDecodeError:
        # Not JSON at all — treat the whole body as one opaque binary.
        return ParsedBody(data={}, binaries=blocks, framed=True)

    if not isinstance(data, dict):
        data = {"value": data}
    return ParsedBody(data=data, binaries=blocks[1:], framed=True)


def build_body(payload: dict | None = None, binaries: list[bytes] | None = None) -> bytes:
    """Encode a server -> device body using the same framing the device uses."""
    out = bytearray()
    if payload is not None:
        head = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\x00"
        out += struct.pack("<I", len(head)) + head
    for blob in binaries or []:
        out += struct.pack("<I", len(blob)) + blob
    return bytes(out)


def split_blocks(raw: bytes, size: int = MAX_BLOCK):
    """Yield (blk_no, chunk). The final chunk is numbered 0, as the device does."""
    if not raw:
        yield 0, b""
        return
    chunks = [raw[i:i + size] for i in range(0, len(raw), size)]
    for index, chunk in enumerate(chunks[:-1], start=1):
        yield index, chunk
    yield 0, chunks[-1]


def header_map(request) -> dict:
    """Pull the protocol headers out of a Django request, lowercased."""
    out = {}
    for name in (HEADER_DEV_ID, HEADER_REQUEST_CODE, HEADER_TRANS_ID,
                 HEADER_CMD_CODE, HEADER_BLK_NO, HEADER_CMD_RETURN_CODE):
        value = request.headers.get(name) or request.headers.get(name.replace("_", "-"))
        if value is not None:
            out[name] = value.strip()
    return out


def parse_device_time(value) -> tuple[int, int, int, int, int, int] | None:
    """
    Device timestamps come as YYYYMMDDhhmmss (14 chars) or YYMMDDhhmmss (12).
    Returns a tuple ready for datetime(), or None.
    """
    if value is None:
        return None
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 12:
        digits = "20" + digits
    if len(digits) != 14:
        return None
    try:
        return (int(digits[0:4]), int(digits[4:6]), int(digits[6:8]),
                int(digits[8:10]), int(digits[10:12]), int(digits[12:14]))
    except ValueError:
        return None


def format_device_time(dt) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


# ---------------------------------------------------------------------------
# Making device data safe to store
# ---------------------------------------------------------------------------
# PostgreSQL refuses a NUL byte in any text column and any \u0000 inside jsonb.
# This protocol is full of them: every length prefix has zero bytes, every JSON
# block ends in one, and C firmware pads fixed-width strings like user names
# with them. SQLite accepts all of it, which is why none of this showed up in
# development — on the production database every single device request failed.

NUL_MARKER = "\u2400"   # ␀, visible in the protocol log where a NUL was


def clean_text(value, max_length=None, marker=""):
    """A device-supplied string with every NUL removed (or made visible)."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\x00", marker)
    if max_length is not None:
        text = text[:max_length]
    return text


def scrub(value):
    """Recursively clean anything headed for a JSONField."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {clean_text(k): scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value
