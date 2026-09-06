#!/bin/bash
# Erzeugt die Squid-Passwortdatei aus PROXY_USER/PROXY_PASS und startet Squid.
set -euo pipefail

: "${PROXY_USER:?PROXY_USER fehlt}"
: "${PROXY_PASS:?PROXY_PASS fehlt}"

# htpasswd ist im ubuntu/squid-Image nicht enthalten — Python (auch nicht) …
# deshalb portable: openssl erzeugt einen APR1/MD5-Hash, den basic_ncsa_auth versteht.
HASH="$(openssl passwd -apr1 "$PROXY_PASS")"
printf '%s:%s\n' "$PROXY_USER" "$HASH" > /etc/squid/passwd
chmod 640 /etc/squid/passwd
chown root:proxy /etc/squid/passwd 2>/dev/null || true

# Cache-Verzeichnisse braucht Squid auch bei „cache deny all" (für Swap-State).
squid -N -z 2>/dev/null || true

echo "adse-proxy: Squid startet auf :3128 (Benutzer: ${PROXY_USER})"
exec squid -N -d 1
