#!/bin/sh
# Create or replace a Radicale account in secrets/radicale-users.
#
#   scripts/radicale-passwd.sh <username>          # generates a random password
#   scripts/radicale-passwd.sh <username> --prompt # asks for a password
#
# The password is hashed with bcrypt inside the radicale image, so nothing
# needs to be installed on the host. A generated password is printed once.
set -eu

cd "$(dirname "$0")/.."

usage() {
    echo "usage: $0 <username> [--prompt]" >&2
    exit 2
}

[ $# -ge 1 ] || usage
user=$1
mode=${2:-generate}

if ! printf '%s' "$user" | grep -Eq '^[a-z0-9][a-z0-9_-]{0,31}$'; then
    echo "username must match ^[a-z0-9][a-z0-9_-]{0,31}\$" >&2
    exit 1
fi

case "$mode" in
    generate)
        password=$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 32)
        ;;
    --prompt)
        stty -echo
        printf 'Password for %s: ' "$user"; read -r password; echo
        printf 'Repeat: '; read -r password2; echo
        stty echo
        [ "$password" = "$password2" ] || { echo "passwords differ" >&2; exit 1; }
        [ ${#password} -ge 16 ] || { echo "use at least 16 characters" >&2; exit 1; }
        ;;
    *)
        usage
        ;;
esac

docker image inspect calendar-radicale:local >/dev/null 2>&1 || docker compose build -q radicale

# Password goes in on stdin so it never shows up in the process list.
hash=$(printf '%s' "$password" | docker run --rm -i --network none --entrypoint python \
    calendar-radicale:local -c \
    'import sys, bcrypt; print(bcrypt.hashpw(sys.stdin.buffer.read(), bcrypt.gensalt(12)).decode())')

mkdir -p secrets
umask 077
touch secrets/radicale-users
tmp=$(mktemp secrets/.radicale-users.XXXXXX)
grep -v "^${user}:" secrets/radicale-users > "$tmp" || true
printf '%s:%s\n' "$user" "$hash" >> "$tmp"
mv "$tmp" secrets/radicale-users
# Readable by the container's radicale user (uid 10001), not by others.
chmod 644 secrets/radicale-users
chmod 700 secrets

echo "Saved account '$user' in secrets/radicale-users."
if [ "$mode" = generate ]; then
    echo "Password (shown once): $password"
fi
echo "Radicale reads the file on each login; no restart needed."
