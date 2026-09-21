#!/usr/bin/env bash
# =============================================================================
#  Issue the certificate for LPS Attendance and switch the site to HTTPS.
#
#      bash /home/lps/app/deploy/enable-https.sh <domain> <email>
#
#  Uses certbot's webroot mode, so certbot never edits any nginx file — not
#  ours, and certainly not dreamspot.conf. Renewal is handled by the timer the
#  certbot package already installed; the deploy hook reloads nginx afterwards.
#
#  The terminal port (8008) stays plain HTTP. Cheap terminals rarely speak TLS,
#  and a redirect answered to a POST is a lost punch.
# =============================================================================
set -euo pipefail

DOMAIN="${1:?usage: enable-https.sh <domain> <email>}"
EMAIL="${2:?usage: enable-https.sh <domain> <email>}"
SERVER_IP="${SERVER_IP:-103.29.180.40}"
ACME_ROOT=/var/www/lps-acme
APP_DIR=/home/lps/app

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mSTOPPED: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -f /etc/nginx/conf.d/lps.conf ] || die "run install.sh first"

DOMAINS=(-d "$DOMAIN")
CHECK=("$DOMAIN")
if [ "$(tr -cd '.' <<<"$DOMAIN" | wc -c)" -eq 1 ]; then
    DOMAINS+=(-d "www.$DOMAIN")
    CHECK+=("www.$DOMAIN")
fi

say "1. DNS must already point here, or Let's Encrypt will refuse"
for name in "${CHECK[@]}"; do
    got=$(dig +short "$name" @8.8.8.8 | tail -1)
    [ "$got" = "$SERVER_IP" ] || die "$name resolves to '${got:-nothing}', not $SERVER_IP. Fix the A record and wait for it to propagate."
    ok "$name → $got"
done

say "2. The challenge path answers over HTTP"
mkdir -p "$ACME_ROOT/.well-known/acme-challenge"
echo ok > "$ACME_ROOT/.well-known/acme-challenge/lps-probe"
restorecon -R "$ACME_ROOT" 2>/dev/null || true
code=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: $DOMAIN" \
            "http://127.0.0.1/.well-known/acme-challenge/lps-probe")
rm -f "$ACME_ROOT/.well-known/acme-challenge/lps-probe"
[ "$code" = "200" ] || die "challenge path returned $code, expected 200"
ok "challenge path reachable"

say "3. Certificate — for $DOMAIN only, never for dreamspotglobal.com"
certbot certonly --webroot -w "$ACME_ROOT" "${DOMAINS[@]}" \
    --non-interactive --agree-tos -m "$EMAIL" \
    --deploy-hook "systemctl reload nginx"
[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] || die "certificate was not issued"
ok "certificate issued"

say "4. Rewrite the vhost with HTTPS (install.sh does it, nginx -t guarded)"
# install.sh is idempotent: it sees the certificate and writes the 443 block
# plus the port-80 redirect. It re-runs its own device self-test and both
# sites' health checks at the end.
bash "$APP_DIR/deploy/install.sh" "$DOMAIN" "$EMAIL"

say "5. From outside"
curl -s -o /dev/null -w "    dreamspotglobal.com  %{http_code}\n" https://dreamspotglobal.com/ || true
curl -s -o /dev/null -w "    $DOMAIN  %{http_code}\n" "https://$DOMAIN/" || true
curl -s -o /dev/null -w "    http://$DOMAIN  %{http_code} (301 expected)\n" "http://$DOMAIN/" || true
echo
certbot certificates 2>/dev/null | grep -E "Certificate Name|Domains|Expiry" | sed 's/^/    /'
