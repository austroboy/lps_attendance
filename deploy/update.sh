#!/usr/bin/env bash
# =============================================================================
#  Pull the latest code and restart LPS Attendance. Dream Spot is not touched.
#
#      bash /home/lps/app/deploy/update.sh
#
#  Your workflow from now on: test on the Mac → git push → ssh dreamspot →
#  run this. No more zip files on the server.
# =============================================================================
set -euo pipefail

APP_USER=lps
APP_DIR=/home/lps/app
VENV=/home/lps/venv

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mSTOPPED: %s\033[0m\n' "$*" >&2; exit 1; }
as_app() { sudo -u "$APP_USER" -H bash -c "cd '$APP_DIR' && $*"; }

[ "$(id -u)" -eq 0 ] || die "run as root"

say "1. Backup before touching anything"
STAMP=$(date +%F-%H%M)
mkdir -p /var/backups/lps
PGPASSWORD=$(cut -d: -f3 /root/lps_db_url.txt | cut -d@ -f1) \
    /usr/pgsql-16/bin/pg_dump -h 127.0.0.1 -U lpsuser lpsdb \
    | gzip > "/var/backups/lps/pre-update-$STAMP.sql.gz"
ok "/var/backups/lps/pre-update-$STAMP.sql.gz"

say "2. Code"
BEFORE=$(as_app "git rev-parse --short HEAD")
as_app "git pull --ff-only"
AFTER=$(as_app "git rev-parse --short HEAD")
if [ "$BEFORE" = "$AFTER" ]; then
    ok "already at $AFTER — restarting anyway"
else
    ok "$BEFORE → $AFTER"
    as_app "git log --oneline $BEFORE..$AFTER" | sed 's/^/      /'
fi

say "3. Dependencies, schema, static"
sudo -u "$APP_USER" "$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
as_app "$VENV/bin/python manage.py migrate --noinput" | tail -3
as_app "$VENV/bin/python manage.py collectstatic --noinput" | tail -1
as_app "$VENV/bin/python manage.py check" | tail -1

say "4. Restart LPS only"
systemctl restart lps lps-celery
sleep 4
systemctl is-active --quiet lps || { journalctl -u lps -n 40 --no-pager; die "lps did not come back"; }
systemctl is-active --quiet lps-celery || { journalctl -u lps-celery -n 40 --no-pager; die "lps-celery did not come back"; }
ok "lps and lps-celery running"

say "5. Health"
DOMAIN=$(grep -E '^DJANGO_ALLOWED_HOSTS=' "$APP_DIR/.env" | cut -d= -f2 | cut -d, -f1)
curl -s -o /dev/null -w "    dreamspotglobal.com  %{http_code}\n" https://dreamspotglobal.com/ || true
curl -s -o /dev/null -w "    $DOMAIN  %{http_code}\n" "https://$DOMAIN/" || true
printf "    device port: "; curl -s --max-time 10 http://127.0.0.1:8008/ebkn/ | head -1
free -m | awk '/Mem:/ {printf "    memory: %s MB available\n", $7}'
