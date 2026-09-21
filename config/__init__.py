# Importing the Celery app here means `shared_task` works everywhere without
# each module having to know about the broker.
from .celery import app as celery_app

__all__ = ("celery_app",)
