"""
SSL Wireless ISMS Plus v3 client.

Endpoints (POST, JSON, all under {BASE_URL}/api/v3):
    /send-sms          one message, one recipient
    /send-sms/bulk     the same message to up to 100 recipients
    /send-sms/dynamic  up to 100 different messages in one request

Every request carries api_token and sid. `csms_id` is your own reference for a
message and must be unique and at most 20 characters.

Note: SSL Wireless only accepts calls from IP addresses whitelisted in the
ISMS Plus portal, so give them the school server's public IP before testing.
"""
from __future__ import annotations

import logging
import uuid

import requests
from django.conf import settings

log = logging.getLogger(__name__)

SUCCESS_CODES = {200, "200"}


class SmsError(Exception):
    pass


def new_csms_id(prefix="lps") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"[:20]


def normalise_msisdn(number: str) -> str | None:
    """Bangladeshi numbers in, 8801XXXXXXXXX out. Returns None if unusable."""
    if not number:
        return None
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    if digits.startswith("880") and len(digits) == 13:
        return digits
    if digits.startswith("0") and len(digits) == 11:
        return "88" + digits
    if len(digits) == 10 and digits.startswith("1"):
        return "880" + digits
    return None


class SslWirelessClient:
    def __init__(self, config=None):
        config = config or settings.SSLWIRELESS
        self.base_url = config.get("BASE_URL", "").rstrip("/")
        self.api_token = config.get("API_TOKEN", "")
        self.sid = config.get("SID", "")
        self.timeout = config.get("TIMEOUT", 20)

    @property
    def configured(self):
        return bool(self.base_url and self.api_token and self.sid)

    def _post(self, path, payload):
        if not self.configured:
            raise SmsError("SMS gateway is not configured. Add the API token and SID in settings.")
        url = f"{self.base_url}/api/v3{path}"
        payload = {"api_token": self.api_token, "sid": self.sid, **payload}
        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SmsError(f"Could not reach the SMS gateway: {exc}") from exc
        try:
            data = response.json()
        except ValueError:
            raise SmsError(f"Gateway returned a non-JSON reply ({response.status_code})")
        if data.get("status_code") not in SUCCESS_CODES:
            raise SmsError(data.get("error_message") or f"Gateway rejected the request: {data}")
        return data

    def send_one(self, msisdn, text, csms_id=None):
        return self._post("/send-sms", {
            "msisdn": msisdn,
            "sms": text,
            "csms_id": csms_id or new_csms_id(),
        })

    def send_bulk(self, msisdns, text, csms_id=None):
        """Same body to many numbers. Max 100 per call."""
        return self._post("/send-sms/bulk", {
            "msisdn": ",".join(msisdns),
            "sms": text,
            "csms_id": csms_id or new_csms_id(),
        })

    def send_dynamic(self, messages):
        """messages = [{"msisdn": ..., "text": ..., "csms_id": ...}, ...] max 100."""
        return self._post("/send-sms/dynamic", {"messages": messages})


def chunked(items, size=100):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]
