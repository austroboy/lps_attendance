# Update 01 — pagination and interface fixes

No model changes, so **no migration is needed** and your data is untouched.

## The bug this fixes

`templates/_pager.html` built its links as `?{{ qs }}page=2`, but no view ever
set `qs`. Every page-2 link came out as a bare `?page=2`, which silently threw
away whatever filter you were looking at. Search for a student, go to page two,
and you were suddenly browsing all 48 students from the top with the search box
still showing your term. On a real roll of 800 students that is the kind of
thing you would chase for an hour and blame on the data.

Pagination now uses Django's built-in `{% querystring %}` tag, which merges
into the current query string instead of replacing it. Filters, sort and page
size all survive.

Export links had the same shape (`?{{ request.GET.urlencode }}&export=1`) and
now use the same tag, so an export always matches what is on screen.

## Pagination added

These pages previously rendered every row at once:

| Page | Was | Now |
|---|---|---|
| Daily register | every record for the day | 50 per page |
| Teacher register | every record | 50 per page |
| Monthly sheet | every student in the class | 50 per page |
| Low attendance | every student below the threshold | 50 per page |
| Device users | capped at 500, silently | 50 per page, nothing hidden |
| SMS preview | every recipient | 50 per page |

Two things deliberately do **not** page:

- **Totals.** The counts above each table describe the whole filtered set, not
  the fifty rows on screen. A register showing "16 records" means sixteen, even
  when you are on page one of four.
- **Exports.** CSV always covers everything that matches the filter. Exporting
  from page two still gives you the full class.

The old `Device users` page was the worst of these: it cut off at 500 rows with
no pager and no message, so with twelve terminals enrolled you would simply
never see some people, with nothing on screen to tell you.

## Rows per page

Tables over 25 rows now offer 25 / 50 / 100 / 200 in the pager. The choice
rides along in the URL, so a bookmarked or shared link keeps it. An unknown or
silly value falls back to 50 rather than erroring.

## Interface

- **The sidebar now shows where you are.** Highlighting is matched on the
  resolved view name, not the URL text, so Student timetable and Teacher
  timetable highlight separately even though they share a URL.
- **A favicon**, and a `/favicon.ico` route. Every browser requests that file
  whether or not you link one, and each miss was logging a 404 — noise you do
  not want while watching the log for device traffic.
- **Pager styling**: the page-size switcher sits right, the page links left.

## Logging

The "runserver will keep underscore headers" line printed on *every* management
command, including `migrate`, where it appeared above the migration output and
looked like a warning about your database. It is now at DEBUG level and the
`ebkn` logger defaults to INFO, so you still get the lines that matter — an
unregistered terminal calling in, a backfill landing — without the setup
chatter.

Chasing a device that will not talk? Put `EBKN_LOG_LEVEL=DEBUG` in `.env` to
turn it all back on.

## Files changed

```
common.py                      config/settings.py          config/urls.py
devices/apps.py                devices/views.py            attendance/views.py
reports/views.py               smsapp/views.py
static/css/app.css             static/favicon.svg          .env.example
templates/_pager.html          templates/base.html
templates/accounts/login.html
templates/academics/student_list.html
templates/attendance/register.html
templates/attendance/teacher_register.html
templates/devices/enrollment_map.html
templates/devices/punch_list.html
templates/reports/daily.html   templates/reports/defaulters.html
templates/reports/monthly.html templates/reports/teacher.html
templates/smsapp/logs.html     templates/smsapp/preview.html
```

All 29 tests still pass, and all 39 screens were re-checked after the change.
