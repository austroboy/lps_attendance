"""
The scheduled job. Run it every few minutes from cron:

    */5 * * * * cd /srv/lps && /srv/lps/venv/bin/python manage.py run_attendance

It resolves any punches that arrived while the timetable was incomplete, then
marks absent anyone whose slot window has closed with no punch.
"""
import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from attendance.services import close_slots, process_pending, reprocess_day


class Command(BaseCommand):
    help = "Resolve pending punches and close finished slots."

    def add_arguments(self, parser):
        parser.add_argument("--date", default="", help="YYYY-MM-DD, defaults to today")
        parser.add_argument("--reprocess", action="store_true",
                            help="Rebuild the day from raw punches first")

    def handle(self, *args, **options):
        day = dt.date.fromisoformat(options["date"]) if options["date"] else timezone.localdate()

        if options["reprocess"]:
            count = reprocess_day(day)
            self.stdout.write(f"replayed {count} punch(es) for {day}")

        resolved = process_pending()
        counts = close_slots(day)
        self.stdout.write(self.style.SUCCESS(
            f"{resolved} punch(es) resolved; absences filled: "
            f"{counts['students']} students, {counts['teachers']} teachers"))
