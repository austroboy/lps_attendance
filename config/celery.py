"""
Celery for the background work — today that means bulk student imports.

Sized for a small VPS on purpose: two worker processes, one task fetched at a
time, and late acknowledgement so a killed worker leaves the job to be picked
up again rather than losing it silently.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("lps")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    return f"celery is alive: {self.request!r}"
