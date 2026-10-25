#!/bin/sh
# Renders the rights file for the configured owner, then starts Radicale.
set -eu

: "${CAL_USER:?CAL_USER must be set (the calendar owner's username)}"

# Restrict the owner name so it is safe to paste into the rights regexes and
# cannot collide with the service accounts.
if ! printf '%s' "$CAL_USER" | grep -Eq '^[a-z0-9][a-z0-9_-]{0,31}$'; then
    echo "CAL_USER must match ^[a-z0-9][a-z0-9_-]{0,31}\$" >&2
    exit 1
fi
case "$CAL_USER" in
    ingest|agenda)
        echo "CAL_USER must not be a service account name (ingest, agenda)" >&2
        exit 1
        ;;
esac

if [ ! -r /etc/radicale/users ]; then
    echo "/etc/radicale/users is missing; create accounts with scripts/radicale-passwd.sh" >&2
    exit 1
fi

sed "s/@OWNER@/${CAL_USER}/g" /etc/radicale/rights.template > /tmp/rights

# The service accounts may not create their own principal collections (that
# would also give them a "personal" calendar), so create empty ones here.
# Otherwise Radicale logs a warning on every request they make.
for account in ingest agenda; do
    mkdir -p "/var/lib/radicale/collections/collection-root/${account}"
done

exec "$@"
