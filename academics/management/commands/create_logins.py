"""
Create logins for everyone who does not have one yet.

    python manage.py create_logins --students
    python manage.py create_logins --teachers

Students: username and first password are the roll number.
Teachers: username and first password are the employee code.
Safe to run again — anyone who already has a login is skipped.

It takes a few minutes for a whole school. On the server, run it in the
background so a dropped SSH session cannot stop it:

    nohup nice -n 19 sudo -u lps -H bash -c 'cd /home/lps/app && \\
      /home/lps/venv/bin/python manage.py create_logins --students' > /root/logins.log 2>&1 &
"""
from django.core.management.base import BaseCommand, CommandError

from academics.logins import create_student_logins, create_teacher_logins
from academics.models import Student, Teacher


class Command(BaseCommand):
    help = "Create logins for students and/or teachers who do not have one."

    def add_arguments(self, parser):
        parser.add_argument("--students", action="store_true")
        parser.add_argument("--teachers", action="store_true")

    def handle(self, *args, **options):
        if not (options["students"] or options["teachers"]):
            raise CommandError("Say who: --students, --teachers, or both.")

        def report(line):
            self.stdout.write(line)
            self.stdout.flush()

        if options["teachers"]:
            waiting = Teacher.objects.filter(user__isnull=True, is_active=True).count()
            report(f"teachers without a login: {waiting}")
            made, skipped = create_teacher_logins(Teacher.objects.all(), report)
            self.stdout.write(self.style.SUCCESS(f"DONE teachers: {made} created, {skipped} skipped"))

        if options["students"]:
            waiting = Student.objects.filter(user__isnull=True, is_active=True).count()
            report(f"students without a login: {waiting}")
            made, skipped = create_student_logins(Student.objects.all(), report)
            self.stdout.write(self.style.SUCCESS(f"DONE students: {made} created, {skipped} skipped"))
