#!/bin/sh
# Writes a dated tar.gz of all calendar data to backups/ and keeps the last 30.
# Run it nightly from cron, e.g.:
#   15 3 * * * /path/to/Calendar/scripts/backup.sh
set -eu

cd "$(dirname "$0")/.."
mkdir -p backups
umask 077

out="backups/radicale-$(date +%Y%m%d-%H%M%S).tar.gz"
docker compose exec -T radicale tar -C /var/lib/radicale -czf - collections > "$out"
echo "wrote $out"

ls -1t backups/radicale-*.tar.gz | tail -n +31 | xargs -r rm --
