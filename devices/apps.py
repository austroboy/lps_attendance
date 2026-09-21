import logging

from django.apps import AppConfig
from django.conf import settings

log = logging.getLogger("ebkn")


def allow_underscore_headers():
    """
    Stop the development server from deleting the terminal's headers.

    Django's runserver drops every header whose name contains an underscore.
    So do nginx and Apache 2.4+ by default. The reasoning is sound in general:
    WSGI normalises both `X-Real-IP` and `X_Real_IP` to `HTTP_X_REAL_IP`, so a
    client could forge a header that a trusted proxy was supposed to set.

    It is fatal here. Every header in this protocol uses underscores —
    dev_id, request_code, trans_id, cmd_code, blk_no — and the firmware will
    not send anything else. Stripped headers mean every request arrives
    anonymous and every punch is discarded, while a cheerful 200 OK goes back
    to the device, so nothing looks broken from either end.

    The risk this reintroduces is narrow: it only matters if something in front
    of this app sets a trusted header an attacker could spoof by swapping a
    dash for an underscore. This app trusts no such header. If you put it
    behind a proxy that does, restrict the device endpoint to the LAN rather
    than turning this back off.

    In production, run gunicorn (which does not strip) behind nginx with
    `underscores_in_headers on;`. See DEVICE_SETUP.md.
    """
    from django.core.servers.basehttp import WSGIRequestHandler

    if getattr(WSGIRequestHandler, "_ebkn_patched", False):
        return

    def get_environ(self):
        # Deliberately skipping Django's underscore strip; the rest of the
        # parent implementation is untouched.
        return super(WSGIRequestHandler, self).get_environ()

    WSGIRequestHandler.get_environ = get_environ
    WSGIRequestHandler._ebkn_patched = True
    # Debug level: this fires on every management command, and a line about
    # header parsing on every `migrate` buries the output that matters.
    log.debug("runserver will keep underscore headers so terminals can be identified")


class DevicesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "devices"

    def ready(self):
        if getattr(settings, "EBKN_ALLOW_UNDERSCORE_HEADERS", True):
            allow_underscore_headers()
