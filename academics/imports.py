"""
Reading a spreadsheet of students and turning it into rows in the database.

Design notes
------------
Two passes, both in memory. 5,000 rows of text is a couple of megabytes, which
is cheaper to hold than to query row by row. Pass one resolves every class,
section, shift, version and group with a cache so a 5,000-row file does a few
dozen lookups instead of 25,000. Pass two splits students into new and existing
and uses bulk_create / bulk_update.

The naive version — update_or_create per row — is about 10,000 queries and
several minutes on a small VPS with SQLite. This is a handful of queries per
chunk and finishes in seconds.
"""
from __future__ import annotations

import csv
import io
import re

from django.db import transaction
from django.utils import timezone

from .import_models import ImportJob, ImportStatus
from .models import Group, SchoolClass, Section, Shift, Student, Version

CHUNK = 500
MAX_PROBLEMS = 200

# Accepted column names, normalised. The left side is what we call it.
COLUMNS = {
    "admission_no": ["admission_no", "admission", "admission no", "admissionno",
                     "admission number", "id", "student id"],
    "roll_no": ["roll_no", "roll", "roll no", "rollno", "roll number"],
    "full_name": ["full_name", "name", "full name", "student name", "student_name"],
    "class": ["class", "class_name", "school_class", "className", "grade"],
    "section": ["section", "section_name", "sec"],
    "shift": ["shift"],
    "version": ["version", "medium"],
    "group": ["group", "group_name", "stream"],
    "guardian_name": ["guardian_name", "guardian", "guardian name", "father_name",
                      "father", "parent", "parent_name"],
    "guardian_phone": ["guardian_phone", "guardian phone", "phone", "mobile",
                       "guardian_mobile", "contact", "guardian contact"],
    "student_phone": ["student_phone", "student phone", "student_mobile"],
    "device_user_id": ["device_user_id", "device user id", "device_id", "deviceid",
                       "machine_id", "device user"],
    "is_active": ["is_active", "active", "status"],
}

REQUIRED = ["admission_no", "full_name", "class", "section"]

HEADER_LOOKUP = {}
for canonical, aliases in COLUMNS.items():
    for alias in aliases:
        HEADER_LOOKUP[alias.lower().strip().replace(" ", "_")] = canonical


class ImportError_(Exception):
    """Something wrong with the file itself, not with one row."""


def _norm_header(value):
    if value is None:
        return ""
    text = str(value).strip().lower().replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]", "", text)
    return HEADER_LOOKUP.get(text, "")


def _clean(value):
    """Trim, and collapse runs of whitespace — copy-paste from Word leaves both."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def _clean_phone(value):
    """Spreadsheets love turning 01712345678 into 1712345678 or 1.71235e+10."""
    text = _clean(value)
    if not text:
        return ""
    if "e+" in text.lower():
        try:
            text = str(int(float(text)))
        except ValueError:
            pass
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10 and digits.startswith("1"):
        digits = "0" + digits
    return digits


def read_rows(file_obj, filename=""):
    """Yield dicts keyed by canonical column name. Handles .xlsx and .csv."""
    name = (filename or getattr(file_obj, "name", "")).lower()

    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        workbook = load_workbook(file_obj, read_only=True, data_only=True)
        sheet = workbook["Students"] if "Students" in workbook.sheetnames else workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            raise ImportError_("That spreadsheet is empty.")
        mapping = [_norm_header(cell) for cell in header]
        _check_header(mapping)
        for index, raw in enumerate(rows, start=2):
            if raw is None or all(cell is None or str(cell).strip() == "" for cell in raw):
                continue
            yield index, {mapping[i]: raw[i] for i in range(min(len(mapping), len(raw)))
                          if mapping[i]}
        workbook.close()
        return

    data = file_obj.read()
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(data))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportError_("That file is empty.")
    mapping = [_norm_header(cell) for cell in header]
    _check_header(mapping)
    for index, raw in enumerate(reader, start=2):
        if not any(str(cell).strip() for cell in raw):
            continue
        yield index, {mapping[i]: raw[i] for i in range(min(len(mapping), len(raw)))
                      if mapping[i]}


def _check_header(mapping):
    missing = [c for c in REQUIRED if c not in mapping]
    if missing:
        raise ImportError_(
            "The file is missing these columns: " + ", ".join(missing)
            + ". Download the sample file and use its header row.")


class LookupCache:
    """
    Resolve — and create on demand — the structures a row refers to.

    Everything is remembered by its cleaned name, so a 5,000 row file touches
    the database once per distinct class, shift, version, group and section.
    """

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.classes = {}
        self.shifts = {}
        self.versions = {}
        self.groups = {}
        self.sections = {}
        self.new = {"classes": [], "sections": [], "shifts": [], "versions": [], "groups": []}
        self._next_order = {}

    def _simple(self, model, cache, bucket, name, order_hint=0):
        if not name:
            return None
        key = name.lower()
        if key in cache:
            return cache[key]
        obj = model.objects.filter(name__iexact=name).first()
        if obj is None:
            if self.dry_run:
                obj = model(name=name, order=order_hint)
            else:
                obj = model.objects.create(name=name, order=order_hint)
            self.new[bucket].append(name)
        cache[key] = obj
        return obj

    def shift(self, name):
        return self._simple(Shift, self.shifts, "shifts", name)

    def version(self, name):
        return self._simple(Version, self.versions, "versions", name)

    def group(self, name):
        return self._simple(Group, self.groups, "groups", name)

    def school_class(self, name):
        if not name:
            return None
        key = name.lower()
        if key in self.classes:
            return self.classes[key]
        obj = SchoolClass.objects.filter(name__iexact=name).first()
        if obj is None:
            order = _guess_class_order(name)
            if self.dry_run:
                obj = SchoolClass(name=name, order=order)
            else:
                obj = SchoolClass.objects.create(name=name, order=order)
            self.new["classes"].append(name)
        self.classes[key] = obj
        return obj

    def section(self, class_name, section_name, shift_name, version_name, group_name):
        # Key on the names, never on primary keys. In a dry run nothing is saved,
        # so every shift and version has pk None — keying on pk collapsed
        # "Six A Morning Bangla" and "Six A Day English" into one entry and the
        # preview under-reported what the file would create.
        key = (class_name.lower(), section_name.lower(), shift_name.lower(),
               version_name.lower(), group_name.lower())
        if key in self.sections:
            return self.sections[key]

        school_class = self.school_class(class_name)
        shift = self.shift(shift_name)
        version = self.version(version_name)
        group = self.group(group_name)
        label = _section_label(class_name, section_name, shift_name, version_name, group_name)

        unsaved_parent = (school_class.pk is None
                          or (shift and shift.pk is None)
                          or (version and version.pk is None)
                          or (group and group.pk is None))

        if self.dry_run and unsaved_parent:
            # A parent does not exist yet, so the section cannot exist either.
            obj = Section(school_class=school_class, name=section_name)
            self.new["sections"].append(label)
            self.sections[key] = obj
            return obj

        obj = Section.objects.filter(school_class=school_class, name__iexact=section_name,
                                     shift=shift, version=version, group=group).first()
        if obj is None:
            if self.dry_run:
                obj = Section(school_class=school_class, name=section_name,
                              shift=shift, version=version, group=group)
            else:
                obj = Section.objects.create(school_class=school_class, name=section_name,
                                             shift=shift, version=version, group=group)
            self.new["sections"].append(label)
        self.sections[key] = obj
        return obj


def _section_label(class_name, section_name, shift_name, version_name, group_name):
    bits = [class_name]
    if group_name:
        bits.append(group_name)
    bits.append(section_name)
    label = " - ".join(bits)
    tags = [t for t in (version_name, shift_name) if t]
    return f"{label} ({', '.join(tags)})" if tags else label


CLASS_WORDS = {
    "play": 0, "nursery": 1, "kg": 2, "one": 3, "1": 3, "two": 4, "2": 4,
    "three": 5, "3": 5, "four": 6, "4": 6, "five": 7, "5": 7, "six": 8, "6": 8,
    "seven": 9, "7": 9, "eight": 10, "8": 10, "nine": 11, "9": 11, "ten": 12,
    "10": 12, "eleven": 13, "11": 13, "twelve": 14, "12": 14,
}


def _guess_class_order(name):
    """Sort Class Ten after Class Nine without making anyone type a number."""
    for word in re.split(r"[^a-z0-9]+", name.lower()):
        if word in CLASS_WORDS:
            return CLASS_WORDS[word]
    return 50


TRUE_WORDS = {"1", "true", "yes", "y", "active", "a"}


def run_job(job: ImportJob, progress_every=CHUNK):
    """Do the whole import. Safe to call from Celery or from a shell."""
    job.status = ImportStatus.RUNNING
    job.started_at = timezone.now()
    job.processed = job.created = job.updated = job.skipped = job.deactivated = 0
    job.problems = []
    job.error = ""
    job.save()

    problems = []

    def note(line, message):
        if len(problems) < MAX_PROBLEMS:
            problems.append(f"Row {line}: {message}")

    try:
        job.upload.open("rb")
        rows = list(read_rows(job.upload, job.original_name or job.upload.name))
        job.upload.close()
    except ImportError_ as exc:
        job.status = ImportStatus.FAILED
        job.error = str(exc)
        job.finished_at = timezone.now()
        job.save()
        return job
    except Exception as exc:  # noqa: BLE001 - surface anything openpyxl throws
        # openpyxl says "File is not a zip file" for anything that is not a real
        # .xlsx, which means nothing to the person who just renamed a .xls.
        detail = str(exc)
        if "zip file" in detail.lower():
            message = ("That file is not a real Excel workbook. If it came from an older "
                       "Excel or from Google Sheets, open it and use File, Save as, and "
                       "pick Excel Workbook (.xlsx) or CSV.")
        else:
            message = f"Could not read that file: {detail}"
        job.status = ImportStatus.FAILED
        job.error = message
        job.finished_at = timezone.now()
        job.save()
        return job

    job.total_rows = len(rows)
    job.save(update_fields=["total_rows"])

    cache = LookupCache(dry_run=job.dry_run)
    parsed = []
    seen = set()

    for line, raw in rows:
        admission = _clean(raw.get("admission_no"))
        name = _clean(raw.get("full_name"))
        class_name = _clean(raw.get("class"))
        section_name = _clean(raw.get("section"))

        if not admission:
            note(line, "no admission number")
            job.skipped += 1
            continue
        if len(admission) > 40 or len(class_name) > 60:
            # A sentence where an identifier belongs means a stray heading or a
            # note row got pasted in. Importing it would invent a class named
            # after the note, so refuse it and say why.
            note(line, "this looks like a heading or a note, not a student — skipped")
            job.skipped += 1
            continue
        if not name:
            note(line, f"{admission} has no name")
            job.skipped += 1
            continue
        if not class_name or not section_name:
            note(line, f"{admission} is missing a class or section")
            job.skipped += 1
            continue
        if admission in seen:
            note(line, f"{admission} appears more than once in this file")
            job.skipped += 1
            continue
        seen.add(admission)

        try:
            section = cache.section(class_name, section_name,
                                    _clean(raw.get("shift")),
                                    _clean(raw.get("version")),
                                    _clean(raw.get("group")))
        except Exception as exc:  # noqa: BLE001
            note(line, f"could not place {admission}: {exc}")
            job.skipped += 1
            continue

        active_raw = _clean(raw.get("is_active"))
        parsed.append({
            "admission_no": admission,
            "full_name": name,
            # Blank roll means "same as the admission number" at this school.
            "roll_no": _clean(raw.get("roll_no")) or admission,
            "section": section,
            "guardian_name": _clean(raw.get("guardian_name")),
            "guardian_phone": _clean_phone(raw.get("guardian_phone")),
            "student_phone": _clean_phone(raw.get("student_phone")),
            "device_user_id": _clean(raw.get("device_user_id")),
            "is_active": True if not active_raw else active_raw.lower() in TRUE_WORDS,
        })

    for bucket, field in (("classes", "new_classes"), ("sections", "new_sections"),
                          ("shifts", "new_shifts"), ("versions", "new_versions"),
                          ("groups", "new_groups")):
        setattr(job, field, sorted(set(cache.new[bucket]))[:200])

    if job.dry_run:
        # Report the same split the real run would produce, so the preview is
        # something you can actually check against expectations rather than a
        # single "5000 rows" number.
        admissions = [row["admission_no"] for row in parsed]
        known = set()
        for start in range(0, len(admissions), CHUNK):
            known.update(Student.objects
                         .filter(admission_no__in=admissions[start:start + CHUNK])
                         .values_list("admission_no", flat=True))
        job.processed = len(rows)
        job.created = sum(1 for a in admissions if a not in known)
        job.updated = len(admissions) - job.created
        job.problems = problems
        job.status = ImportStatus.DONE
        job.finished_at = timezone.now()
        job.save()
        return job

    existing = {}
    admissions = [row["admission_no"] for row in parsed]
    for start in range(0, len(admissions), CHUNK):
        batch = admissions[start:start + CHUNK]
        for student in Student.objects.filter(admission_no__in=batch):
            existing[student.admission_no] = student

    fields = ["full_name", "roll_no", "section", "guardian_name", "guardian_phone",
              "student_phone", "device_user_id", "is_active"]

    to_create, to_update = [], []
    for row in parsed:
        student = existing.get(row["admission_no"])
        if student is None:
            to_create.append(Student(**row))
        else:
            for field in fields:
                setattr(student, field, row[field])
            to_update.append(student)

    for start in range(0, len(to_create), CHUNK):
        with transaction.atomic():
            Student.objects.bulk_create(to_create[start:start + CHUNK],
                                        batch_size=CHUNK, ignore_conflicts=True)
        job.created += len(to_create[start:start + CHUNK])
        job.processed = min(job.total_rows, job.created + job.updated + job.skipped)
        job.save(update_fields=["created", "processed"])

    for start in range(0, len(to_update), CHUNK):
        with transaction.atomic():
            Student.objects.bulk_update(to_update[start:start + CHUNK],
                                        fields, batch_size=CHUNK)
        job.updated += len(to_update[start:start + CHUNK])
        job.processed = min(job.total_rows, job.created + job.updated + job.skipped)
        job.save(update_fields=["updated", "processed"])

    if job.create_logins:
        _create_logins(to_create)

    if job.deactivate_missing and seen:
        touched_sections = {row["section"].pk for row in parsed}
        stale = Student.objects.filter(section_id__in=touched_sections,
                                       is_active=True).exclude(admission_no__in=seen)
        job.deactivated = stale.update(is_active=False)

    job.processed = job.total_rows
    job.problems = problems
    job.status = ImportStatus.DONE
    job.finished_at = timezone.now()
    job.save()
    return job


def _create_logins(students):
    """One login per new student, in bulk. Existing usernames are left alone."""
    from django.contrib.auth import get_user_model

    from accounts.models import Role

    User = get_user_model()
    wanted = {s.admission_no: s for s in students if s.pk and not s.user_id}
    if not wanted:
        wanted = {s.admission_no: s for s in
                  Student.objects.filter(admission_no__in=[x.admission_no for x in students],
                                         user__isnull=True)}
    if not wanted:
        return

    taken = set(User.objects.filter(username__in=list(wanted)).values_list("username", flat=True))
    fresh = []
    for admission, student in wanted.items():
        if admission in taken:
            continue
        user = User(username=admission, role=Role.STUDENT,
                    first_name=student.full_name[:30], must_change_password=True)
        user.set_password(admission)
        fresh.append(user)

    for start in range(0, len(fresh), CHUNK):
        User.objects.bulk_create(fresh[start:start + CHUNK], ignore_conflicts=True)

    users = {u.username: u for u in User.objects.filter(username__in=list(wanted))}
    linked = []
    for admission, student in wanted.items():
        user = users.get(admission)
        if user and not student.user_id:
            student.user = user
            linked.append(student)
    for start in range(0, len(linked), CHUNK):
        Student.objects.bulk_update(linked[start:start + CHUNK], ["user"], batch_size=CHUNK)
