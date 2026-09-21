"""Small shared bits used across apps: widgets, CSV export, pagination."""
import csv

from django import forms
from django.core.paginator import Paginator
from django.http import HttpResponse


class DateInput(forms.DateInput):
    input_type = "date"


class TimeInput(forms.TimeInput):
    input_type = "time"


class ColourInput(forms.TextInput):
    input_type = "color"


def style_form(form):
    """Give every field a class so one stylesheet can handle all forms."""
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple,
                               forms.RadioSelect)):
            continue
        existing = widget.attrs.get("class", "")
        widget.attrs["class"] = (existing + " field").strip()
    return form


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)


PER_PAGE_CHOICES = (25, 50, 100, 200)


def paginate(request, queryset, per_page=50):
    """
    Page a queryset, honouring a ?per_page= the reader picked.

    get_page swallows a bad page number and returns the first page, which is
    what you want when someone edits the URL or a filter shrinks the result set
    under their feet.
    """
    try:
        requested = int(request.GET.get("per_page") or per_page)
    except (TypeError, ValueError):
        requested = per_page
    if requested not in PER_PAGE_CHOICES:
        requested = per_page
    return Paginator(queryset, requested).get_page(request.GET.get("page"))


def csv_response(filename, header, rows):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return response


WEEKDAY_CHOICES = [
    (0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"),
    (4, "Fri"), (5, "Sat"), (6, "Sun"),
]


class WeekdayField(forms.MultipleChoiceField):
    """Weekday checkboxes backed by a JSONField holding a list of ints."""

    def __init__(self, **kwargs):
        kwargs.setdefault("choices", WEEKDAY_CHOICES)
        kwargs.setdefault("widget", forms.CheckboxSelectMultiple)
        kwargs.setdefault("required", False)
        kwargs.setdefault(
            "help_text", "Tick the days this applies to. Leave all unticked for every day.")
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if value is None:
            return []
        return [str(v) for v in value]

    def clean(self, value):
        value = super().clean(value)
        return sorted(int(v) for v in value)
