# LPS Attendance

A Django attendance system for a school running EBKN face-recognition
terminals, built to work without any cooperation from the company that
installed them.

**New here? Read [DEVICE_SETUP.md](DEVICE_SETUP.md) first.** It covers the
device protocol, the header trap that will otherwise cost you a day, and how to
discover a terminal's id when you have no paperwork for it.

---

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py seed_demo          # optional: demo data to click around
python manage.py runserver 0.0.0.0:8000
```

`seed_demo` creates `admin / admin123` (full access) and `T-001 / T-001` (a
teacher limited to Class Six), 48 students, two student slots, a teacher slot,
and an SMS schedule. Delete `db.sqlite3` to start clean.

### Try it without hardware

```bash
python manage.py ebkn_simulate --serial SIM001 --poll
python manage.py ebkn_simulate --serial SIM001 --user 101 --punch
python manage.py ebkn_simulate --serial SIM001 --user 101 --punch --at 20260914082000
python manage.py ebkn_simulate --serial UNKNOWN99 --poll     # lands in Discovered
```

The simulator speaks the real wire format, so the whole path — framing, headers,
command queue, attendance engine — is exercised exactly as a device would.

### Tests

```bash
python manage.py test
```

29 tests covering the protocol codec, device traffic, fragment reassembly, the
attendance engine, and SMS targeting.

---

## What is here

| App | Responsibility |
|---|---|
| `accounts` | Users and four roles: super admin, admin, teacher, student |
| `academics` | Sessions, classes, sections, students, teachers, access grants, holidays |
| `devices` | The EBKN protocol, device registry, command queue, raw punches, wire log |
| `attendance` | Time slots, the drag-and-drop timetable, records, the engine |
| `smsapp` | Templates, scheduled sends, SSL Wireless gateway, logs |
| `reports` | Daily, monthly, low-attendance, teacher, device, and student views |

### Who sees what

- **Super admin** — everything, including user management.
- **Admin** — everything operational; no user management.
- **Teacher** — only the classes granted under Teacher access. Student lists,
  registers and reports are all filtered to those sections; admin screens
  return 403.
- **Student** — one screen, their own attendance.

---

## How a punch becomes attendance

```
terminal  ──POST realtime_glog──▶  middleware ──▶ protocol codec
                                                       │
                                              Punch (raw, kept forever)
                                                       │
                                          device_user_id ──▶ student or teacher
                                                       │
                                       which slots is their class in today?
                                                       │
                                    which slot's window contains this moment?
                                                       │
                          before the late cut-off → Present, after it → Late
                                                       │
                               window closes with no punch → Absent (cron job)
```

Raw punches are never interpreted destructively. Fix a timetable a week later
and **Attendance → Recalculate → Rebuild a day** replays history against the
corrected rules. Records edited by hand are marked manual and survive rebuilds
untouched.

## Design decisions worth knowing

**Unregistered devices are recorded, not rejected.** A terminal with an unknown
serial gets logged to Discovered with its payload and still receives `200 OK`,
so it keeps talking while you register it. That is what makes commissioning
possible without vendor documentation.

**Terminals are identified by header, not by URL.** `EbknDeviceMiddleware` sits
above CSRF and session middleware and routes any POST carrying `request_code`
straight to the protocol handler. Firmware that posts to `/` works exactly like
firmware that posts to `/ebkn/`.

**Device requests never return an error status.** A 500 or a 404 can make
firmware retry a punch forever or drop it. Failures are logged and answered
with `200 OK` plus a `response_code` header.

**One SMS run per schedule per day.** A `SmsRun` row is the guard, so a cron job
that fires twice cannot message every parent twice.

---

## Configuration

Everything lives in `.env` — see `.env.example`. The ones that matter:

| Setting | Why |
|---|---|
| `EBKN_ALLOW_UNDERSCORE_HEADERS` | Leave on. Off means no terminal is ever identified. |
| `EBKN_AUTO_DISCOVER` | Log unknown serials instead of rejecting them. |
| `EBKN_TRAFFIC_LOG` | Full wire log. On for commissioning, off once stable. |
| `SSLWIRELESS_API_TOKEN` / `_SID` | SMS credentials. Whitelist your server IP with them too. |
| `DB_ENGINE` | `sqlite` by default; `postgres` for more than a few hundred students. |

SQLite is fine for one school. Move to Postgres when several terminals push
concurrently — SQLite's single-writer lock starts to bite.

---

## Production

```bash
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3
```

Behind nginx, `underscores_in_headers on;` is mandatory. Set
`DJANGO_DEBUG=False`, a real `DJANGO_SECRET_KEY`, and a specific
`DJANGO_ALLOWED_HOSTS`. Run `collectstatic` and serve `/static/` from nginx.
Back up `db.sqlite3` (or run `pg_dump`) on a schedule — the punch history is the
one thing you cannot recreate.
