"""
Fire any SMS schedule whose time has come. Run from cron alongside the
attendance job:

    */5 * * * * cd /srv/lps && venv/bin/python manage.py send_scheduled_sms

A SmsRun row per schedule per day stops a second run from double-sending.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from smsapp.services import due_schedules, run_schedule


class Command(BaseCommand):
    help = "Send every SMS schedule that is due today and has not run yet."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would go out without sending")
        parser.add_argument("--schedule", type=int, default=None,
                            help="Run one schedule by id, ignoring its send time")
        parser.add_argument("--force", action="store_true",
                            help="Send even if it already ran today")

    def handle(self, *args, **options):
        from smsapp.models import SmsSchedule

        if options["schedule"]:
            schedules = SmsSchedule.objects.filter(pk=options["schedule"])
        else:
            schedules = list(due_schedules())

        if not schedules:
            self.stdout.write("nothing due")
            return

        for schedule in schedules:
            run = run_schedule(schedule, timezone.localdate(),
                               dry_run=options["dry_run"], force=options["force"])
            self.stdout.write(self.style.SUCCESS(
                f"{schedule.name}: total={run.total} sent={run.sent} "
                f"failed={run.failed} skipped={run.skipped}"))
            if options["dry_run"] and run.detail:
                self.stdout.write(run.detail)
