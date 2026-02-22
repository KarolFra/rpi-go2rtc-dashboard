#!/bin/sh
set -eu

IFACE="${1:-tailscale0}"
STATE_DIR="/var/lib/usage-counter"
OUT_JSON="/var/www/html/usage.json"

mkdir -p "$STATE_DIR"

rx=$(cat "/sys/class/net/$IFACE/statistics/rx_bytes" 2>/dev/null || echo 0)
tx=$(cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null || echo 0)
total=$((rx + tx))

month=$(date +%Y-%m)
cur_month_file="$STATE_DIR/month"
offset_file="$STATE_DIR/offset"

if [ ! -f "$cur_month_file" ] || [ "$(cat "$cur_month_file")" != "$month" ]; then
  echo "$month" > "$cur_month_file"
  echo "$total" > "$offset_file"
fi

offset=$(cat "$offset_file" 2>/dev/null || echo 0)
used=$((total - offset))
[ "$used" -lt 0 ] && used=0

tmp="$(mktemp)"
cat > "$tmp" <<EOF
{"iface":"$IFACE","month":"$month","total_bytes":$used,"ts":$(date +%s)}
EOF
mv "$tmp" "$OUT_JSON"
