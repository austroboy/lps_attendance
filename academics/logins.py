"""
Creating many logins at once.

Each password hash deliberately costs about a third of a second, so a whole
roll takes minutes. That is why this runs from Celery or a management command
and never inside a web request.
"""
from django.contrib.auth.hashers import make_password
from django.db import transaction

from accounts.models import Role, User

BATCH = 100


def _create(rows, role, report=None):
    """
    rows: iterable of (owner_model_instance_pk, username, full_name, model).
    Usernames already taken are skipped and reported, never overwritten.
    """
    taken = set(User.objects.values_list("username", flat=True))
    made = skipped = 0
    batch = []

    def flush():
        nonlocal made
        with transaction.atomic():
            users = User.objects.bulk_create([u for _, _, u in batch])
            for (model, pk, _), user in zip(batch, users):
                model.objects.filter(pk=pk).update(user=user)
        made += len(batch)
        batch.clear()
        if report:
            report(f"  {made} created")

    for model, pk, login, name in rows:
        login = (login or "").strip()
        if not login or login in taken:
            skipped += 1
            if report:
                report(f"  skipped: username '{login}' is empty or already used")
            continue
        taken.add(login)
        batch.append((model, pk, User(username=login, password=make_password(login),
                                      role=role, first_name=(name or "")[:150],
                                      must_change_password=True)))
        if len(batch) >= BATCH:
            flush()
    if batch:
        flush()
    return made, skipped


def create_student_logins(queryset, report=None):
    """Username and first password: the roll number (the admission ID here)."""
    from .models import Student
    rows = ((Student, pk, roll or admission, name) for pk, roll, admission, name in
            queryset.filter(user__isnull=True, is_active=True)
                    .values_list("pk", "roll_no", "admission_no", "full_name"))
    return _create(rows, Role.STUDENT, report)


def create_teacher_logins(queryset, report=None):
    """Username and first password: the employee code, as the Add teacher form does."""
    from .models import Teacher
    rows = ((Teacher, pk, code, name) for pk, code, name in
            queryset.filter(user__isnull=True, is_active=True)
                    .values_list("pk", "employee_code", "full_name"))
    return _create(rows, Role.TEACHER, report)
