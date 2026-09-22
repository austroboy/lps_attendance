from django import forms
from django.contrib.auth import get_user_model

from accounts.models import Role
from common import DateInput, StyledFormMixin

from .import_models import ImportJob
from .models import (
    AcademicSession,
    ClassAccess,
    Group,
    Holiday,
    SchoolClass,
    Section,
    Shift,
    Student,
    Teacher,
    Version,
)

User = get_user_model()


class SessionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = AcademicSession
        fields = ["name", "start_date", "end_date", "is_active"]
        widgets = {"start_date": DateInput(), "end_date": DateInput()}


class SchoolClassForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SchoolClass
        fields = ["name", "order", "session"]


class SectionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Section
        fields = ["school_class", "name", "shift", "version", "group", "room"]


class ShiftForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Shift
        fields = ["name", "start_note", "order"]


class VersionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Version
        fields = ["name", "order"]


class GroupForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Group
        fields = ["name", "order"]


def grouped_section_choices(queryset, blank="Choose a class and section"):
    """
    Sections as <optgroup>s by class.

    A flat list of 149 entries like "Two - Meghna (Bangla, Morning)" is a
    scroll-and-squint exercise; grouped under their class it is a glance.
    Display only — validation still runs against the queryset, so a teacher
    cannot post a section they were not offered.
    """
    groups, current, bucket = [], None, []
    for section in queryset.select_related("school_class", "shift", "version", "group"):
        if section.school_class_id != current:
            if bucket:
                groups.append((bucket_name, bucket))
            current, bucket_name, bucket = section.school_class_id, section.school_class.name, []
        # The full name, not just the part after the class: a closed <select>
        # shows only the option text, and "A" alone could be any class.
        bucket.append((section.pk, str(section)))
    if bucket:
        groups.append((bucket_name, bucket))
    return [("", blank)] + groups


class StudentForm(StyledFormMixin, forms.ModelForm):
    create_login = forms.BooleanField(
        required=False, initial=False,
        label="Create a login for this student",
        help_text="Username and first password: the roll number.")

    class Meta:
        model = Student
        fields = ["full_name", "admission_no", "roll_no", "section",
                  "guardian_name", "guardian_phone", "student_phone",
                  "device_user_id", "rfid_number", "photo",
                  "is_active", "leave_reason", "leave_note"]
        labels = {
            "admission_no": "Admission number",
            "roll_no": "Roll number",
            "section": "Class and section",
            "device_user_id": "Terminal user ID",
            "rfid_number": "Card number (RFID)",
            "is_active": "On the roll",
            "leave_reason": "Reason for leaving",
            "leave_note": "Note",
            "photo": "Upload a new photo",
        }
        help_texts = {
            "roll_no": "Leave blank to use the admission number.",
            "guardian_phone": "Attendance SMS goes to this number. 01XXXXXXXXX.",
            "device_user_id": "Filled in automatically when you enrol the face from the Students page.",
            "rfid_number": "Scan the card here, or type it.",
            "is_active": "Untick for a student who has left. Their history is kept.",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        sections = Section.objects.all()
        allowed = user.allowed_section_ids() if user is not None else None
        if allowed is not None:
            # A teacher only ever sees — and can only ever save — their own sections.
            sections = sections.filter(pk__in=allowed)
        self.fields["section"].queryset = sections
        self.fields["section"].choices = grouped_section_choices(sections)
        self.fields["photo"].widget = forms.FileInput(attrs={"accept": "image/*"})
        self.fields["leave_note"].widget = forms.TextInput(attrs={"class": "field"})
        self.has_login = bool(self.instance.pk and self.instance.user_id)
        if self.has_login:
            del self.fields["create_login"]
        self._was_active = self.instance.is_active if self.instance.pk else True
        self._old_rfid = self.instance.rfid_number if self.instance.pk else ""

    def clean_rfid_number(self):
        value = "".join(ch for ch in (self.cleaned_data.get("rfid_number") or "")
                        if ch.isalnum()).upper()[:32]
        if value:
            clash = (Student.objects.filter(rfid_number=value)
                     .exclude(pk=self.instance.pk).select_related("section").first())
            if clash:
                raise forms.ValidationError(
                    f"That card is already on {clash.full_name} ({clash.section}).")
        return value

    def save(self, commit=True):
        from .roster import deactivate, reactivate, set_rfid

        student = super().save(commit=False)
        now_active = student.is_active
        # Leaving and returning go through the same path as the Students page,
        # so the terminals are updated too — not just this row.
        student.is_active = self._was_active
        new_rfid, student.rfid_number = student.rfid_number, self._old_rfid
        student.save()

        if self._was_active and not now_active:
            deactivate(student, reason=self.cleaned_data.get("leave_reason", ""),
                       note=self.cleaned_data.get("leave_note", ""), by=self.user)
        elif not self._was_active and now_active:
            reactivate(student, by=self.user)
        if new_rfid != self._old_rfid:
            set_rfid(student, new_rfid)

        if self.cleaned_data.get("create_login") and not student.user_id:
            login = student.roll_no or student.admission_no
            user, created = User.objects.get_or_create(
                username=login,
                defaults={"role": Role.STUDENT, "first_name": student.full_name[:150],
                          "must_change_password": True},
            )
            if created:
                user.set_password(login)
                user.save()
            student.user = user
            student.save(update_fields=["user"])
        return student


class TeacherForm(StyledFormMixin, forms.ModelForm):
    create_login = forms.BooleanField(
        required=False, initial=False,
        label="Create a login for this teacher",
        help_text="Username and first password: the employee code.")
    access_classes = forms.MultipleChoiceField(required=False,
                                               widget=forms.CheckboxSelectMultiple)
    access_sections = forms.MultipleChoiceField(required=False,
                                                widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = Teacher
        fields = ["full_name", "employee_code", "designation", "campus", "phone",
                  "joined_on", "device_user_id", "is_active"]
        widgets = {"joined_on": DateInput()}
        labels = {
            "employee_code": "Employee code",
            "joined_on": "Joining date",
            "device_user_id": "Terminal user ID",
            "is_active": "Currently teaching here",
        }
        help_texts = {
            "employee_code": "Stays the same on every upload — it is how this teacher is recognised.",
            "device_user_id": "The number this teacher is enrolled under on the attendance terminal.",
            "campus": "",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sections = list(Section.objects.select_related(
            "school_class", "shift", "version", "group"))
        self.classes = list(SchoolClass.objects.all())
        self.fields["access_classes"].choices = [(str(c.pk), c.name) for c in self.classes]
        self.fields["access_sections"].choices = [(str(s.pk), str(s)) for s in self.sections]
        self.fields["campus"].widget.attrs["list"] = "campus-options"
        self.campus_options = list(Teacher.objects.exclude(campus="")
                                   .values_list("campus", flat=True).distinct().order_by("campus"))

        whole, parts = set(), set()
        if self.instance.pk:
            for grant in self.instance.class_access.all():
                if grant.section_id:
                    parts.add(str(grant.section_id))
                else:
                    whole.add(str(grant.school_class_id))
        if self.is_bound:
            whole = set(self.data.getlist("access_classes"))
            parts = set(self.data.getlist("access_sections"))
        self._whole, self._parts = whole, parts

        self.has_login = bool(self.instance.pk and self.instance.user_id)
        if self.has_login:
            del self.fields["create_login"]

    def access_tree(self):
        """Classes, each with its sections, and what is ticked — for the template."""
        by_class = {}
        for section in self.sections:
            by_class.setdefault(section.school_class_id, []).append(section)
        tree = []
        for cls in self.classes:
            sections = by_class.get(cls.pk, [])
            chosen = [s for s in sections if str(s.pk) in self._parts]
            tree.append({
                "cls": cls,
                "whole": str(cls.pk) in self._whole,
                "sections": [{"section": s, "checked": str(s.pk) in self._parts} for s in sections],
                "count": len(chosen),
            })
        return tree

    def save(self, commit=True):
        teacher = super().save(commit=commit)
        whole = {int(x) for x in self.cleaned_data.get("access_classes", [])}
        parts = {int(x) for x in self.cleaned_data.get("access_sections", [])}
        section_class = {s.pk: s.school_class_id for s in self.sections}

        existing = list(teacher.class_access.all())
        keep = set()
        for grant in existing:
            key = ("s", grant.section_id) if grant.section_id else ("c", grant.school_class_id)
            wanted = (grant.section_id in parts) if grant.section_id else (grant.school_class_id in whole)
            if wanted:
                keep.add(key)
            else:
                grant.delete()
        for class_id in whole:
            if ("c", class_id) not in keep:
                ClassAccess.objects.create(teacher=teacher, school_class_id=class_id)
        for section_id in parts:
            if ("s", section_id) not in keep:
                ClassAccess.objects.create(teacher=teacher, section_id=section_id,
                                           school_class_id=section_class[section_id])

        if self.cleaned_data.get("create_login") and not teacher.user_id:
            user, created = User.objects.get_or_create(
                username=teacher.employee_code,
                defaults={"role": Role.TEACHER, "first_name": teacher.full_name[:150],
                          "phone": teacher.phone, "must_change_password": True},
            )
            if created:
                user.set_password(teacher.employee_code)
                user.save()
            teacher.user = user
            teacher.save(update_fields=["user"])
        return teacher


class ClassAccessForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ClassAccess
        fields = ["teacher", "school_class", "section", "can_edit_attendance", "can_send_sms"]


class HolidayForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ["date", "name", "applies_to_teachers"]
        widgets = {"date": DateInput()}


MAX_UPLOAD_MB = 15


class BulkStudentImportForm(StyledFormMixin, forms.ModelForm):
    upload = forms.FileField(
        label="Spreadsheet",
        help_text="Excel (.xlsx) or CSV. Class, section, shift, version and group are "
                  "read from the file — you do not need to create them first.")

    class Meta:
        model = ImportJob
        fields = ["upload", "dry_run", "create_logins", "deactivate_missing"]
        labels = {
            "dry_run": "Check the file first, do not save anything",
            "create_logins": "Create a student login for each new row",
            "deactivate_missing": "Mark students missing from this file as inactive",
        }

    def clean_upload(self):
        upload = self.cleaned_data["upload"]
        name = upload.name.lower()
        if not name.endswith((".xlsx", ".xlsm", ".csv")):
            raise forms.ValidationError(
                "That file type will not open here. Save it as .xlsx or .csv and try again.")
        if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(
                f"That file is larger than {MAX_UPLOAD_MB} MB. Split it into two uploads.")
        return upload


class BulkTeacherImportForm(BulkStudentImportForm):
    upload = forms.FileField(
        label="Spreadsheet",
        help_text="Excel (.xlsx) or CSV with an employee_code and full_name column.")

    class Meta(BulkStudentImportForm.Meta):
        labels = {
            "dry_run": "Check the file first, do not save anything",
            "create_logins": "Create a teacher login for each new row",
            "deactivate_missing": "Mark teachers missing from this file as inactive",
        }
        help_texts = {
            "create_logins": "Username and first password: the employee code. Takes a "
                             "few minutes, so it happens in the background.",
            "deactivate_missing": "Only use this with the full staff list.",
        }
