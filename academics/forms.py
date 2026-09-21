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


class TeacherForm(StyledFormMixin, forms.ModelForm):
    create_login = forms.BooleanField(
        required=False, initial=True,
        label="Create a login for this teacher",
        help_text="Username is the employee code. First password is the employee code too.")

    class Meta:
        model = Teacher
        fields = ["employee_code", "full_name", "designation", "phone",
                  "device_user_id", "is_active", "joined_on"]
        widgets = {"joined_on": DateInput()}

    def save(self, commit=True):
        teacher = super().save(commit=commit)
        if self.cleaned_data.get("create_login") and not teacher.user_id:
            user, created = User.objects.get_or_create(
                username=teacher.employee_code,
                defaults={"role": Role.TEACHER, "first_name": teacher.full_name[:30],
                          "phone": teacher.phone, "must_change_password": True},
            )
            if created:
                user.set_password(teacher.employee_code)
                user.save()
            teacher.user = user
            teacher.save(update_fields=["user"])
        return teacher


class StudentForm(StyledFormMixin, forms.ModelForm):
    create_login = forms.BooleanField(
        required=False, initial=True,
        label="Create a login for this student",
        help_text="Username is the admission number. First password is the admission number too.")

    class Meta:
        model = Student
        fields = ["admission_no", "roll_no", "full_name", "section", "guardian_name",
                  "guardian_phone", "student_phone", "device_user_id", "photo", "is_active"]

    def save(self, commit=True):
        student = super().save(commit=commit)
        if self.cleaned_data.get("create_login") and not student.user_id:
            user, created = User.objects.get_or_create(
                username=student.admission_no,
                defaults={"role": Role.STUDENT, "first_name": student.full_name[:30],
                          "must_change_password": True},
            )
            if created:
                user.set_password(student.admission_no)
                user.save()
            student.user = user
            student.save(update_fields=["user"])
        return student


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
