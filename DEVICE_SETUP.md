# Getting the terminals talking

Read this before you touch a device. The order matters, and step 3 is the one
that silently eats a whole day if you skip it.

---

## 1. What the protocol actually is

These terminals use the EBKN / FK-series **push** protocol. That one word
changes everything about your situation:

**The device is the client. It dials you.** There is no port to open on the
device, no SDK to license, no Windows service to install, and nothing the
previous vendor can withhold from you. The terminal repeatedly POSTs to an
address you type into its own menu. If it can reach your server over the
network, you have it — with or without the company that installed it.

Traffic is plain HTTP POST with `Content-Type: application/octet-stream`.
Everything that identifies the request lives in custom headers:

| Header | Sent by | Meaning |
|---|---|---|
| `dev_id` | device | terminal id — this is what you register in the UI |
| `request_code` | device | `receive_cmd`, `send_cmd_result`, `realtime_glog`, `realtime_enroll_data` |
| `trans_id` | both | which queued command this relates to |
| `cmd_code` | server | the command you want run |
| `blk_no` | device | fragment number; **0 means last or only** |
| `cmd_return_code` | device | `OK`, or an error string |

The body is a sequence of length-prefixed blocks:

```
<uint32 little-endian length><block bytes>
<uint32 little-endian length><block bytes>
...
```

Block 0 is UTF-8 JSON terminated by a NUL byte. Blocks after it are the raw
binaries the JSON names as `BIN_1`, `BIN_2` (a face capture, a fingerprint
template). A punch looks like this:

```json
{"user_id":"101","verify_mode":"FACE","io_mode":1,
 "io_time":"20260914074500","log_image":"BIN_1"}
```

Some firmware sends bare JSON with no framing. The parser sniffs for a leading
`{` and handles both.

### Talking back

You cannot push to the device. Instead you queue a command, and it collects it
on its next poll — usually within a few seconds. Queue from **Devices → open a
device → Send a command**. Available: `GET_DEVICE_STATUS`, `SET_TIME`,
`GET_USER_ID_LIST`, `GET_LOG_DATA`, `GET_USER_INFO`, `SET_USER_INFO`,
`SET_USER_NAME`, `SET_USER_PRIVILEGE`, `DELETE_USER`, `SET_ENROLL_DATA`,
`GET_ENROLL_DATA`, `CLEAR_LOG_DATA`, `CLEAR_ENROLL_DATA`, `SET_FK_NAME`.

---

## 2. Start the server

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit it
python manage.py migrate
python manage.py createsuperuser     # set role to Super admin afterwards
python manage.py runserver 0.0.0.0:8000
```

`0.0.0.0` matters. Bound to `127.0.0.1`, the server is invisible to every
device on the network.

Find the machine's LAN address with `ip addr` (Linux) or `ipconfig` (Windows) —
something like `192.168.1.50`. From a phone on the same wifi, open
`http://192.168.1.50:8000/ebkn/`. If you do not see the "server is up" line,
stop here and fix the network. It is almost always the firewall:

```bash
sudo ufw allow 8000/tcp                                      # Linux
netsh advfirewall firewall add rule name="Attendance" ^
      dir=in action=allow protocol=TCP localport=8000        # Windows
```

---

## 3. The underscore trap

**Read this one.** Every header in this protocol contains an underscore.
Django's development server, nginx, and Apache 2.4+ all **silently delete
headers whose names contain underscores.** It is a sensible default against
header spoofing, and it is fatal here.

The failure is nasty because it does not look like a failure. The device
connects. The server answers `200 OK`. The device is satisfied and moves on.
But `dev_id` never arrived, so the request was anonymous, and the punch went
nowhere. Both ends report success. You lose the day.

This project already handles it: `devices/apps.py` patches the dev server on
startup, and `devices/tests.py` has a test that fails loudly if the patch ever
stops being applied. You do not need to do anything in development.

**In production you do — in two places.**

Gunicorn 22 and later **also drop underscore headers by default**
(`--header-map drop`). An earlier version of this guide said gunicorn does not
strip them; that was true of older releases and is wrong now. It must be told:

```bash
gunicorn config.wsgi:application --header-map dangerous --bind unix:/run/lps/lps.sock
```

"dangerous" refers to spoofing a header a trusted proxy sets, by writing it
with an underscore instead of a dash. This app trusts no such header, and the
device port only forwards requests that carry the protocol's own headers.

And if nginx sits in front, this line is not optional:

```nginx
server {
    listen 80;
    server_name attendance.yourschool.edu.bd;

    underscores_in_headers on;      # ← without this, no terminal is ever identified

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

On Apache: `HttpProtocolOptions Unsafe`. On Cloudflare or a similar CDN,
underscore headers may be dropped upstream with no way to re-enable them —
point the terminals at the origin directly, not through the CDN.

---

## 4. Learn the real device ids

You have twelve blank terminals and no paperwork. Do not guess.

1. Power up one device.
2. **Menu → Comm / Network → Server** (wording varies by firmware). Set the
   server address to your machine's IP, the port to `8000`. If it asks for a
   path, `/ebkn/`. If there is a "cloud" or "push" toggle, turn it on.
3. Save and let it sit for a minute.
4. Open **Devices → Discovered** in the app.

The terminal's actual `dev_id` appears there, along with the first payload it
sent. One click registers it. Now you know the id, with no vendor involved.

The barcode on the case (`ENS2025041` on yours) is a reasonable guess for the
serial, but firmware sometimes reports something different. Discovered tells
you the truth.

Once you know the pattern across two or three devices, **Devices → Add many**
takes the rest as a pasted list. Adding a thirteenth device later is the same
screen — nothing is fixed at twelve.

### If Discovered stays empty

Open **Devices → Protocol log**. It records every request that reaches the
server, including malformed ones, with headers and a hex dump.

- **Rows appearing, `dev_id` blank** → the underscore problem in step 3.
- **No rows at all** → nothing is arriving. Network, IP, port, or firewall.
- **Rows arriving with unfamiliar `request_code` values, or a body that is not
  length-prefixed JSON** → your devices are a different protocol family than
  expected. Send me the hex dump; the codec is one small module and adapting it
  is a contained job.

---

## 5. Connect people to device ids

The terminal knows a person as a number. Until that number is attached to a
student, punches are stored but produce no attendance.

- **Devices → Device users** lists what the terminals report and lets you link
  each id to a student or teacher. Queue `GET_USER_ID_LIST` on a device to
  populate it, or just enrol someone on the terminal — new enrolments push to
  us automatically.
- Or type the number straight into each student's record.
- Or bulk load: **Students → Import CSV**, with a `device_user_id` column.

Enrol a face on a device, punch once, and check **Devices → Raw punches**. The
note column tells you exactly what happened, including "No student or teacher
is mapped to device user id 101" when a link is missing.

---

## 6. Build the timetable

**Attendance → Student timetable.** Create a slot, then drag classes into it.

A slot is three times:

| | |
|---|---|
| Opens | punches start counting |
| Late after | on or after this, a punch is late |
| Closes | no punch by now means absent |

A typical morning: opens 07:00, late after 08:00, closes 09:30, optional
check-out 13:30–15:00. Tick the weekdays; leave them all unticked for every day.

Drag a whole class to fill every section at once, or individual sections for
finer control. Drop the same class into several boxes for several sessions a
day. The teacher timetable works identically with teachers instead of classes.

Absences are not instant — they are filled in once a window closes, by the
scheduled job.

---

## 7. Schedule the jobs

```cron
*/5 * * * * cd /srv/lps && venv/bin/python manage.py run_attendance
*/5 * * * * cd /srv/lps && venv/bin/python manage.py send_scheduled_sms
```

`run_attendance` resolves punches that arrived before their rules existed and
marks absent anyone whose window has closed. `send_scheduled_sms` sends any SMS
schedule whose time has passed. Both are safe to run repeatedly — a record per
schedule per day prevents double-sending.

Without cron, **Attendance → Recalculate** does the same by hand.

---

## 8. SMS

Get an API token and SID from SSL Wireless and put them in `.env`:

```
SSLWIRELESS_API_TOKEN=...
SSLWIRELESS_SID=...
```

**Give SSL Wireless your server's public IP.** The API only accepts requests
from whitelisted addresses. Without that, every send fails with an
authorisation error no matter how correct your token is.

Then: write a template (**SMS → Templates**), create a schedule, and drag
classes into it. A working absence alert:

- audience: student guardian
- slot: Morning entry
- statuses: Absent
- sends at: 09:45, Sunday to Thursday

Set the send time *after* the slot's closing time, or absences have not been
worked out yet and the message goes to nobody.

**Always use Preview first.** It shows the exact recipients and the exact text
for a chosen day without sending anything or spending credit.

Note on length: English SMS is 160 characters per part. Bengali is Unicode, so
one part is only 70 characters — a Bangla message is often three parts and
costs three times as much.

---

## 9. Tomorrow, in order

1. `python manage.py runserver 0.0.0.0:8000`
2. Open `http://<server-ip>:8000/ebkn/` from a phone on the school wifi.
   Nothing appears → fix the network before touching a device.
3. Point one terminal at the server. Watch **Devices → Discovered**.
4. Register it. Confirm it shows Online.
5. Queue `SET_TIME` — proves the command path works in both directions.
6. Queue `GET_USER_ID_LIST` — see who is already enrolled.
7. Enrol yourself, punch once, check **Raw punches**.
8. Link your device id to a test student, punch again, check the register.
9. Only when one device works end to end, do the other eleven.

The protocol log is on by default so you can see everything. Turn it off with
`EBKN_TRAFFIC_LOG=False` once the fleet is stable — it grows quickly.

---

## Two things worth saying plainly

**The old vendor cannot block you.** These devices push to whatever address you
give them. Their refusal to help costs you documentation, not capability.

**Verify the protocol before trusting the twelve.** The identification is from
photographs and a matching protocol spec, not from your hardware. The parser
handles the documented EBKN format and several variations of it, and every part
is tested — but your specific firmware is still unconfirmed. The protocol log
is there precisely so that step 4 tomorrow either confirms it in seconds or
shows you exactly what is different. If it is different, it is one module to
change, not the application.
