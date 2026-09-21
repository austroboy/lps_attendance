#!/usr/bin/env bash
# =============================================================================
#  LPS Attendance — install on server.dreamspotglobal.com as project #2
# =============================================================================
#
#  Run as root, after the code is cloned to /home/lps/app:
#
#      bash /home/lps/app/deploy/install.sh                  # no domain yet
#      bash /home/lps/app/deploy/install.sh <domain> <email> # once you have one
#
#  Without a domain the site is served at http://<server-ip>:8009. Port 80 is
#  Dream Spot's default server, so the bare IP on port 80 would land there.
#  When a domain arrives, run it again with the domain and then
#  enable-https.sh; the 8009 listener is removed so log-ins are never plain
#  HTTP once HTTPS exists.
#
#  Safe to run more than once. It never overwrites an existing .env or database
#  password, and it never touches anything named dreamspot*.
#
#  What it sets up, all isolated from Dream Spot Global:
#      user lps · /home/lps/{app,venv} · database lpsdb / role lpsuser
#      /run/lps/lps.sock · lps.service · lps-celery.service
#      /etc/nginx/conf.d/lps.conf · /etc/cron.d/lps · /etc/cron.d/lps-backup
#      Redis (new, loopback only) · 2 GB swap if none exists
#
#  Why the terminals get their own port
#  ------------------------------------
#  Dream Spot is the default server on ports 80 and 443. A terminal configured
#  with the bare IP sends "Host: 103.29.180.40", matches no server_name, and
#  lands on Dream Spot — every punch would be a 400 from the wrong site.
#  nginx also reads underscores_in_headers from the default server, so dev_id
#  and request_code could be dropped before our vhost is even chosen.
#  On port 8008 this app is the only server, so it is the default, both
#  problems disappear, and Dream Spot never sees a single device request.
# =============================================================================
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"

APP_USER=lps
HOME_DIR=/home/lps
APP_DIR=$HOME_DIR/app
VENV=$HOME_DIR/venv
RUN_DIR=/run/lps
SOCK=$RUN_DIR/lps.sock
DB_NAME=lpsdb
DB_USER=lpsuser
DB_URL_FILE=/root/lps_db_url.txt
PSQL=/usr/pgsql-16/bin/psql
PY=python3.12
DEVICE_PORT=8008
UI_PORT=8009
SERVER_IP="${SERVER_IP:-103.29.180.40}"
ACME_ROOT=/var/www/lps-acme
NGINX_CONF=/etc/nginx/conf.d/lps.conf

if [ -z "$DOMAIN" ]; then
    MODE=ip
    SERVER_NAMES="_"
    HOSTS="$SERVER_IP"
    ORIGINS="http://$SERVER_IP:$UI_PORT"
    COOKIE_SECURE=False          # plain HTTP: a Secure cookie would never be sent back
    SITE_URL="http://$SERVER_IP:$UI_PORT"
else
    MODE=domain
    # One label for both the apex and www only when the domain is an apex.
    # attendance.example.com should not grow a www.attendance.example.com.
    if [ "$(tr -cd '.' <<<"$DOMAIN" | wc -c)" -eq 1 ]; then
        SERVER_NAMES="$DOMAIN www.$DOMAIN"
        HOSTS="$DOMAIN,www.$DOMAIN"
        ORIGINS="https://$DOMAIN,https://www.$DOMAIN"
    else
        SERVER_NAMES="$DOMAIN"
        HOSTS="$DOMAIN"
        ORIGINS="https://$DOMAIN"
    fi
    COOKIE_SECURE=True
    SITE_URL="https://$DOMAIN"
fi

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mSTOPPED: %s\033[0m\n' "$*" >&2; exit 1; }
as_app() { sudo -u "$APP_USER" -H bash -c "cd '$APP_DIR' && $*"; }

# -----------------------------------------------------------------------------
say "0. Preflight — nothing is changed until these all pass"
# -----------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "run as root"
id "$APP_USER" >/dev/null 2>&1 || die "user $APP_USER does not exist — create it and clone first (see the commands you were given)"
[ -f "$APP_DIR/manage.py" ] || die "$APP_DIR/manage.py not found — clone the repository there first"
command -v "$PY" >/dev/null || die "$PY not found"
[ -x "$PSQL" ] || die "$PSQL not found"
systemctl is-active --quiet nginx || die "nginx is not running"
systemctl is-active --quiet postgresql-16 || die "postgresql-16 is not running"

for port in "$DEVICE_PORT" "$UI_PORT"; do
    if ss -tlnp | grep -q ":$port "; then
        if ! grep -qs "listen $port" "$NGINX_CONF"; then
            die "port $port is already in use by something else"
        fi
    fi
done

DREAMSPOT_BEFORE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 https://dreamspotglobal.com/ || echo 000)
ok "Dream Spot answers $DREAMSPOT_BEFORE before we start"
ok "mode: $MODE · site: $SITE_URL · device port: $DEVICE_PORT"

# -----------------------------------------------------------------------------
say "1. Swap (the handoff asks for it before project #2)"
# -----------------------------------------------------------------------------
if swapon --show --noheadings | grep -q .; then
    ok "swap already present: $(swapon --show --noheadings | awk '{print $1, $3}' | head -1)"
else
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "2 GB swap added"
fi

# -----------------------------------------------------------------------------
say "2. Redis for the background import worker (loopback only)"
# -----------------------------------------------------------------------------
if ! rpm -q redis >/dev/null 2>&1; then
    dnf install -y -q redis
    ok "redis installed"
fi
REDIS_CONF=/etc/redis.conf
[ -f /etc/redis/redis.conf ] && REDIS_CONF=/etc/redis/redis.conf
if ! grep -Eq '^bind 127\.0\.0\.1( |$)' "$REDIS_CONF"; then
    sed -i 's/^bind .*/bind 127.0.0.1/' "$REDIS_CONF"
    grep -q '^bind ' "$REDIS_CONF" || echo 'bind 127.0.0.1' >> "$REDIS_CONF"
fi
grep -q '^protected-mode yes' "$REDIS_CONF" || sed -i 's/^protected-mode .*/protected-mode yes/' "$REDIS_CONF"
systemctl enable --now redis >/dev/null 2>&1
systemctl is-active --quiet redis || die "redis did not start"
redis-cli -h 127.0.0.1 ping | grep -q PONG || die "redis is not answering on 127.0.0.1"
ok "redis on 127.0.0.1:6379 (this app uses db 1, leaving db 0 alone)"

# -----------------------------------------------------------------------------
say "3. Python environment"
# -----------------------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    sudo -u "$APP_USER" "$PY" -m venv "$VENV"
fi
sudo -u "$APP_USER" "$VENV/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
GUNICORN_VERSION=$(sudo -u "$APP_USER" "$VENV/bin/python" -c 'import gunicorn; print(gunicorn.__version__)')
case "$GUNICORN_VERSION" in
    1[0-9].*|2[01].*) die "gunicorn $GUNICORN_VERSION is too old for --header-map (need 22+)";;
esac
ok "virtualenv ready · gunicorn $GUNICORN_VERSION"

# -----------------------------------------------------------------------------
say "4. Database"
# -----------------------------------------------------------------------------
if [ -f "$DB_URL_FILE" ]; then
    ok "reusing existing credentials in $DB_URL_FILE"
else
    DB_PASS=$(openssl rand -base64 32 | tr -d '/+=\n' | head -c 28)
    if sudo -u postgres "$PSQL" -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" 2>/dev/null | grep -q 1; then
        sudo -u postgres "$PSQL" -qc "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null
    else
        sudo -u postgres "$PSQL" -qc "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null
    fi
    if ! sudo -u postgres "$PSQL" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null | grep -q 1; then
        sudo -u postgres "$PSQL" -qc "CREATE DATABASE $DB_NAME OWNER $DB_USER ENCODING 'UTF8';" 2>/dev/null
    fi
    sudo -u postgres "$PSQL" -qc "ALTER ROLE $DB_USER SET timezone TO 'Asia/Dhaka';" 2>/dev/null
    umask 077
    echo "postgresql://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME?sslmode=disable" > "$DB_URL_FILE"
    umask 022
    chmod 600 "$DB_URL_FILE"
    ok "database $DB_NAME and role $DB_USER created"
fi

# -----------------------------------------------------------------------------
say "5. Environment file"
# -----------------------------------------------------------------------------
ENV_FILE=$APP_DIR/.env
set_env() {   # replace KEY=... or append it; values here never contain '|'
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}
if [ -f "$ENV_FILE" ]; then
    ok "$ENV_FILE exists — secrets kept"
else
    umask 077
    cat > "$ENV_FILE" <<EOF
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=$(openssl rand -base64 64 | tr -d '/+=\n' | head -c 64)
DJANGO_ALLOWED_HOSTS=$HOSTS,$SERVER_IP,127.0.0.1,localhost
CSRF_TRUSTED_ORIGINS=$ORIGINS
DJANGO_TIME_ZONE=Asia/Dhaka
DATABASE_URL=$(cat "$DB_URL_FILE")

CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1

EBKN_PUBLIC_ENDPOINT=http://$SERVER_IP:$DEVICE_PORT/ebkn/
EBKN_AUTO_DISCOVER=True
EBKN_TRAFFIC_LOG=True
EBKN_LOG_LEVEL=INFO

# SSL Wireless — fill these in, then: systemctl restart lps lps-celery
SSLWIRELESS_API_TOKEN=
SSLWIRELESS_SID=
EOF
    umask 022
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok ".env written (mode 600)"
fi
# These follow the mode, so moving from IP to a domain later is one re-run.
set_env DJANGO_ALLOWED_HOSTS "$HOSTS,$SERVER_IP,127.0.0.1,localhost"
set_env CSRF_TRUSTED_ORIGINS "$ORIGINS"
set_env SESSION_COOKIE_SECURE "$COOKIE_SECURE"
set_env CSRF_COOKIE_SECURE "$COOKIE_SECURE"
ok "hosts and cookies set for $MODE mode"

# -----------------------------------------------------------------------------
say "6. Migrate, collect static, sanity check"
# -----------------------------------------------------------------------------
mkdir -p "$APP_DIR/media"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/media"
as_app "$VENV/bin/python manage.py migrate --noinput" | tail -3
as_app "$VENV/bin/python manage.py collectstatic --noinput" | tail -1
as_app "$VENV/bin/python manage.py check" | tail -1
ok "database schema and static files ready"

# -----------------------------------------------------------------------------
say "7. systemd: runtime dir, web service, import worker"
# -----------------------------------------------------------------------------
cat > /etc/tmpfiles.d/lps.conf <<EOF
d $RUN_DIR 0755 $APP_USER $APP_USER -
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/lps.conf

cat > /etc/systemd/system/lps.service <<EOF
[Unit]
Description=LPS Attendance (Gunicorn)
After=network.target postgresql-16.service
Requires=postgresql-16.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
# --header-map dangerous: gunicorn 22+ silently drops every header containing
# an underscore. The attendance terminals identify themselves with dev_id and
# request_code, so the default would discard every punch while answering 200.
# The "danger" is spoofing a proxy-set header by swapping - for _; this app
# trusts no such header.
ExecStart=$VENV/bin/gunicorn config.wsgi:application \\
    --workers 2 --worker-class gthread --threads 4 \\
    --timeout 120 --graceful-timeout 30 \\
    --max-requests 1000 --max-requests-jitter 100 \\
    --header-map dangerous \\
    --bind unix:$SOCK \\
    --error-logfile -
Restart=always
RestartSec=5
# A ceiling so a runaway request can never push Dream Spot into the OOM killer.
MemoryMax=500M
MemoryLimit=500M

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/lps-celery.service <<EOF
[Unit]
Description=LPS Attendance (Celery import worker)
After=network.target redis.service postgresql-16.service
Wants=redis.service
Requires=postgresql-16.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$VENV/bin/celery -A config worker \\
    --loglevel=INFO --concurrency=1 --max-tasks-per-child=50 \\
    --without-gossip --without-mingle --without-heartbeat
Restart=always
RestartSec=10
MemoryMax=400M
MemoryLimit=400M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lps lps-celery >/dev/null 2>&1
systemctl restart lps lps-celery
sleep 4
systemctl is-active --quiet lps || { journalctl -u lps -n 30 --no-pager; die "lps did not start"; }
systemctl is-active --quiet lps-celery || { journalctl -u lps-celery -n 30 --no-pager; die "lps-celery did not start"; }
[ -S "$SOCK" ] || die "socket $SOCK was not created"
# "active" only means the process exists. A worker that cannot reach Redis
# sits in a retry loop and still reports active, which is exactly how a broken
# broker went unnoticed once. Ask the broker, and ask the worker, directly.
if ! as_app "timeout 20 $VENV/bin/python -c 'from config.celery import app; app.connection().ensure_connection(max_retries=2); print(\"broker ok\")'" >/dev/null 2>&1; then
    as_app "timeout 20 $VENV/bin/python -c 'from config.celery import app; app.connection().ensure_connection(max_retries=1)'" 2>&1 | tail -3
    die "the app cannot reach Redis — imports would never leave PENDING"
fi
# Send a real task and wait for its answer: broker in, worker runs it, result
# back. (celery inspect ping uses a separate remote-control channel and can
# stay silent while tasks run fine, so it proves nothing either way.)
# A dead worker does not stop the site — imports fall back to "Run it now" —
# so this warns rather than aborting a deploy that is otherwise good.
WORKER_OK=no
for _ in 1 2 3; do
    if as_app "timeout 45 $VENV/bin/python -c 'from config.celery import debug_task; debug_task.delay().get(timeout=30)'" >/dev/null 2>&1; then
        WORKER_OK=yes; break
    fi
    sleep 5
done
if [ "$WORKER_OK" = yes ]; then
    ok "import worker took a task and answered"
else
    warn "import worker did not answer a test task — imports will wait until you press Run it now"
    warn "  see why: journalctl -u lps-celery -n 40 --no-pager"
fi
ok "lps and lps-celery running · socket $SOCK"

# -----------------------------------------------------------------------------
say "8. nginx"
# -----------------------------------------------------------------------------
mkdir -p "$ACME_ROOT"
restorecon -R "$ACME_ROOT" 2>/dev/null || true

HAVE_CERT=no
[ -n "$DOMAIN" ] && [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] && HAVE_CERT=yes

render_nginx() {
cat <<'NGINX'
# LPS Attendance — generated by deploy/install.sh. Edits are overwritten.

# ---------------------------------------------------------------------------
# Terminals. Plain HTTP on a port nothing else uses, so this is the default
# server here: a device that sends the bare IP as Host still arrives, and
# underscores_in_headers is honoured (nginx reads it from the default server).
# Only protocol traffic is forwarded; everything else is a 404, so the admin
# site is never reachable over plain HTTP.
# ---------------------------------------------------------------------------
server {
    listen __DEVICE_PORT__;
    server_name _;
    underscores_in_headers on;
    client_max_body_size 5M;
    access_log /var/log/nginx/lps-devices.access.log;
    error_log  /var/log/nginx/lps-devices.error.log;

    # Reachability check from a phone on the school wifi, and the canonical path.
    location = /ebkn/ {
        proxy_pass http://unix:__SOCK__;
        include /etc/nginx/lps_proxy_params;
    }
    # Some firmware posts to / or its own path. Recognise it by the protocol
    # header rather than the URL.
    location / {
        if ($http_request_code = "") { return 404; }
        proxy_pass http://unix:__SOCK__;
        include /etc/nginx/lps_proxy_params;
    }
}
NGINX

if [ "$MODE" = ip ]; then
cat <<'NGINX'

# ---------------------------------------------------------------------------
# People, no domain yet. Its own port, because Dream Spot is the default
# server on 80 and would answer the bare IP. Plain HTTP until a domain exists;
# port 80 is not touched at all in this mode.
# ---------------------------------------------------------------------------
server {
    listen __UI_PORT__;
    server_name _;
    client_max_body_size 20M;
    access_log /var/log/nginx/lps.access.log;
    error_log  /var/log/nginx/lps.error.log;

    location /media/ { return 404; }
    location / {
        proxy_pass http://unix:__SOCK__;
        include /etc/nginx/lps_proxy_params;
        proxy_read_timeout 120s;
    }
}
NGINX
elif [ "$HAVE_CERT" = yes ]; then
cat <<'NGINX'

# ---------------------------------------------------------------------------
# People: HTTP only exists to answer Let's Encrypt and to send them to HTTPS.
# ---------------------------------------------------------------------------
server {
    listen 80;
    server_name __SERVER_NAMES__;
    access_log /var/log/nginx/lps.access.log;
    error_log  /var/log/nginx/lps.error.log;

    location /.well-known/acme-challenge/ { root __ACME_ROOT__; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name __SERVER_NAMES__;

    ssl_certificate     /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/__DOMAIN__/privkey.pem;
__SSL_OPTIONS__

    client_max_body_size 20M;
    access_log /var/log/nginx/lps.access.log;
    error_log  /var/log/nginx/lps.error.log;

    # Uploaded spreadsheets hold guardian phone numbers. They are never served.
    location /media/ { return 404; }

    location / {
        proxy_pass http://unix:__SOCK__;
        include /etc/nginx/lps_proxy_params;
        proxy_read_timeout 120s;
    }
}
NGINX
else
cat <<'NGINX'

# ---------------------------------------------------------------------------
# People, before the certificate exists. Log-in will not work over plain HTTP
# on purpose (secure cookies); run deploy/enable-https.sh once DNS resolves.
# ---------------------------------------------------------------------------
server {
    listen 80;
    server_name __SERVER_NAMES__;
    client_max_body_size 20M;
    access_log /var/log/nginx/lps.access.log;
    error_log  /var/log/nginx/lps.error.log;

    location /.well-known/acme-challenge/ { root __ACME_ROOT__; }
    location /media/ { return 404; }
    location / {
        proxy_pass http://unix:__SOCK__;
        include /etc/nginx/lps_proxy_params;
    }
}
NGINX
fi
}

cat > /etc/nginx/lps_proxy_params <<'EOF'
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_redirect off;
EOF

SSL_OPTIONS=""
if [ -f /etc/letsencrypt/options-ssl-nginx.conf ]; then
    SSL_OPTIONS="    include /etc/letsencrypt/options-ssl-nginx.conf;"
fi
if [ -f /etc/letsencrypt/ssl-dhparams.pem ]; then
    SSL_OPTIONS="$SSL_OPTIONS
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;"
fi

TMP_CONF=$(mktemp)
render_nginx \
  | sed -e "s|__DEVICE_PORT__|$DEVICE_PORT|g" \
        -e "s|__UI_PORT__|$UI_PORT|g" \
        -e "s|__SOCK__|$SOCK|g" \
        -e "s|__SERVER_NAMES__|$SERVER_NAMES|g" \
        -e "s|__DOMAIN__|$DOMAIN|g" \
        -e "s|__ACME_ROOT__|$ACME_ROOT|g" \
  | awk -v opts="$SSL_OPTIONS" '{ if ($0 == "__SSL_OPTIONS__") print opts; else print }' \
  > "$TMP_CONF"

BACKUP_CONF=""
if [ -f "$NGINX_CONF" ]; then
    BACKUP_CONF=$(mktemp)
    cp "$NGINX_CONF" "$BACKUP_CONF"
fi
cp "$TMP_CONF" "$NGINX_CONF"
rm -f "$TMP_CONF"

# Rule 4 of the handoff: a broken vhost takes BOTH sites down. Test first, and
# put the previous file back if the test fails.
if ! nginx -t 2>/tmp/lps-nginx-test.log; then
    cat /tmp/lps-nginx-test.log
    if [ -n "$BACKUP_CONF" ]; then cp "$BACKUP_CONF" "$NGINX_CONF"; else rm -f "$NGINX_CONF"; fi
    die "nginx -t failed — the previous config was restored, nothing was reloaded"
fi
systemctl reload nginx
[ -n "$BACKUP_CONF" ] && rm -f "$BACKUP_CONF"
sleep 2

PORTS="$DEVICE_PORT"
[ "$MODE" = ip ] && PORTS="$PORTS $UI_PORT"
for port in $PORTS; do
    if ! ss -tlnp | grep -q ":$port "; then
        warn "nginx is not listening on $port. If SELinux blocked it:"
        warn "  semanage port -a -t http_port_t -p tcp $port && systemctl reload nginx"
        die "port $port not open"
    fi
done
ok "nginx reloaded · devices :$DEVICE_PORT · site $SITE_URL"

# -----------------------------------------------------------------------------
say "9. Scheduled jobs and backups"
# -----------------------------------------------------------------------------
cat > /etc/cron.d/lps <<EOF
# LPS Attendance — generated by deploy/install.sh
SHELL=/bin/bash
PATH=/usr/bin:/bin
# Resolve late punches and mark absences once each slot closes.
*/5 * * * * $APP_USER cd $APP_DIR && flock -n /tmp/lps-attendance.lock $VENV/bin/python manage.py run_attendance 2>&1 | systemd-cat -t lps-attendance
# Send any SMS schedule that has come due. Offset by two minutes so the two
# jobs never share the single CPU.
2-59/5 * * * * $APP_USER cd $APP_DIR && flock -n /tmp/lps-sms.lock $VENV/bin/python manage.py send_scheduled_sms 2>&1 | systemd-cat -t lps-sms
EOF
chmod 644 /etc/cron.d/lps

mkdir -p /var/backups/lps
chmod 700 /var/backups/lps
cat > /etc/cron.d/lps-backup <<'EOF'
# LPS Attendance nightly dump — 01:45, clear of Dream Spot's 01:30 dump.
45 1 * * * root PGPASSWORD=$(cut -d: -f3 /root/lps_db_url.txt | cut -d@ -f1) /usr/pgsql-16/bin/pg_dump -h 127.0.0.1 -U lpsuser lpsdb | gzip > /var/backups/lps/db-$(date +\%F).sql.gz && find /var/backups/lps -name "db-*.sql.gz" -mtime +14 -delete
EOF
chmod 644 /etc/cron.d/lps-backup
ok "cron: attendance every 5 min, SMS every 5 min, backup 01:45 (14 days kept)"

# -----------------------------------------------------------------------------
say "10. Proving the device path end to end, on this server"
# -----------------------------------------------------------------------------
# A protocol request goes in through nginx on :8008 and gunicorn, exactly as a
# terminal's would. If dev_id survives both, it shows up as a discovered device.
PROBE=SELFTEST-$RANDOM
as_app "$VENV/bin/python manage.py ebkn_simulate --url http://127.0.0.1:$DEVICE_PORT/ebkn/ --serial $PROBE --poll" >/dev/null 2>&1 || true
SEEN=$(as_app "$VENV/bin/python manage.py shell -c \"from devices.models import UnknownDevice as U; print(U.objects.filter(serial_number='$PROBE').count()); U.objects.filter(serial_number='$PROBE').delete()\"" 2>/dev/null | tail -1)
if [ "$SEEN" = "1" ]; then
    ok "dev_id and request_code survived nginx and gunicorn — terminals will be recognised"
else
    warn "the self-test request did not arrive intact. Look at:"
    warn "  tail -20 /var/log/nginx/lps-devices.error.log ; journalctl -u lps -n 30"
    die "device path is broken"
fi

# -----------------------------------------------------------------------------
say "11. Health — both sites"
# -----------------------------------------------------------------------------
DREAMSPOT_AFTER=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 https://dreamspotglobal.com/ || echo 000)
if [ "$DREAMSPOT_AFTER" != "$DREAMSPOT_BEFORE" ]; then
    warn "Dream Spot answered $DREAMSPOT_BEFORE before and $DREAMSPOT_AFTER now — check it"
else
    ok "Dream Spot still answers $DREAMSPOT_AFTER"
fi
if [ "$MODE" = ip ]; then
    LPS_LOCAL=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$UI_PORT/login/" || echo 000)
    ok "LPS login page on :$UI_PORT → $LPS_LOCAL (200 expected)"
else
    LPS_LOCAL=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: $DOMAIN" http://127.0.0.1/ || echo 000)
    ok "LPS via nginx on :80 → $LPS_LOCAL ($( [ "$HAVE_CERT" = yes ] && echo '301 to HTTPS expected' || echo '302 to login expected'))"
fi
DEV=$(curl -s --max-time 10 "http://127.0.0.1:$DEVICE_PORT/ebkn/" | head -1)
ok "device port says: $DEV"
systemctl is-active nginx postgresql-16 redis dreamspot lps lps-celery crond | paste -sd' ' | sed 's/^/    services: /'
free -m | awk '/Mem:/ {printf "    memory: %s MB used of %s MB, %s MB available\n", $3, $2, $7}'

say "Done"
if [ "$MODE" = ip ]; then
    echo "    Site: $SITE_URL"
elif [ "$HAVE_CERT" = yes ]; then
    echo "    https://$DOMAIN is live."
else
    echo "    Next: point DNS for $DOMAIN at $SERVER_IP, then run"
    echo "        bash $APP_DIR/deploy/enable-https.sh $DOMAIN $EMAIL"
fi
echo "    Terminals: server $SERVER_IP  port $DEVICE_PORT  (path /ebkn/ if the menu asks)"
echo "    Create your admin login:"
echo "        sudo -u $APP_USER -H bash -c 'cd $APP_DIR && $VENV/bin/python manage.py createsuperuser'"
