"""
Catch terminal traffic no matter which path it was aimed at.

The device menu only lets you type a server address and a port — on some
firmware builds you cannot set a path at all, so the terminal POSTs to "/".
Rather than guess, we identify device traffic by its `request_code` header and
hand it straight to the protocol handler, skipping CSRF, sessions and auth.
"""
import logging

from django.utils.deprecation import MiddlewareMixin

from .ebkn import protocol as p

log = logging.getLogger("ebkn")


class EbknDeviceMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.method != "POST":
            return None

        request_code = (request.headers.get(p.HEADER_REQUEST_CODE)
                        or request.headers.get("request-code"))
        dev_id = request.headers.get(p.HEADER_DEV_ID) or request.headers.get("dev-id")
        if not request_code and not dev_id:
            return None

        # Avoid hijacking a browser form that happens to post to /ebkn/.
        if request.content_type and request.content_type.startswith(
                ("application/x-www-form-urlencoded", "multipart/form-data")):
            return None

        from .services import handle_device_request
        return handle_device_request(request)
