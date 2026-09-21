"""Background jobs for the school data."""
import logging

from celery import shared_task

log = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0, time_limit=60 * 60, soft_time_limit=55 * 60)
def run_import_job(self, job_id):
    """
    Import one uploaded student file.

    Deliberately no retries. A half-finished import that starts again from row
    one would double-count its own progress, and the job is safe to re-queue by
    hand from the UI once you know why it failed.
    """
    from django.utils import timezone

    from .import_models import ImportJob, ImportStatus
    from .imports import run_job

    try:
        job = ImportJob.objects.get(pk=job_id)
    except ImportJob.DoesNotExist:
        log.warning("import job %s vanished before the worker got to it", job_id)
        return None

    job.task_id = self.request.id or ""
    job.save(update_fields=["task_id"])

    try:
        run_job(job)
    except Exception as exc:  # noqa: BLE001 - the job row is the error report
        log.exception("import job %s failed", job_id)
        job.refresh_from_db()
        job.status = ImportStatus.FAILED
        job.error = str(exc)[:2000]
        job.finished_at = timezone.now()
        job.save()
        raise

    return {"job": job.pk, "created": job.created, "updated": job.updated,
            "skipped": job.skipped}
