"""
Run an import without Celery.

Use this when Redis is not running, when a job is stuck in PENDING, or when you
would simply rather watch it happen:

    python manage.py run_import --pending
    python manage.py run_import 7
"""
from django.core.management.base import BaseCommand, CommandError

from academics.import_models import ImportJob, ImportStatus
from academics.imports import run_job


class Command(BaseCommand):
    help = "Process a student import job in the foreground."

    def add_arguments(self, parser):
        parser.add_argument("job_id", nargs="?", type=int)
        parser.add_argument("--pending", action="store_true",
                            help="Run every job still waiting for a worker")

    def handle(self, *args, **options):
        if options["pending"]:
            jobs = list(ImportJob.objects.filter(status=ImportStatus.PENDING).order_by("pk"))
            if not jobs:
                self.stdout.write("Nothing is waiting.")
                return
        elif options["job_id"]:
            jobs = list(ImportJob.objects.filter(pk=options["job_id"]))
            if not jobs:
                raise CommandError(f"No import job with id {options['job_id']}.")
        else:
            raise CommandError("Give a job id, or --pending.")

        for job in jobs:
            self.stdout.write(f"Job {job.pk}: {job.original_name} ({job.total_rows or '?'} rows)")
            run_job(job)
            job.refresh_from_db()
            if job.status == ImportStatus.FAILED:
                self.stdout.write(self.style.ERROR(f"  failed: {job.error}"))
                continue
            self.stdout.write(self.style.SUCCESS(
                f"  {job.created} added, {job.updated} updated, "
                f"{job.skipped} skipped, {job.deactivated} deactivated"))
            if job.created_anything:
                for label, values in (("classes", job.new_classes), ("sections", job.new_sections),
                                      ("shifts", job.new_shifts), ("versions", job.new_versions),
                                      ("groups", job.new_groups)):
                    if values:
                        self.stdout.write(f"  new {label}: {', '.join(values[:12])}")
            for problem in job.problems[:15]:
                self.stdout.write(self.style.WARNING(f"  {problem}"))
