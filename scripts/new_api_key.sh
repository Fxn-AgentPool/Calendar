#!/bin/sh
# Generate a new API key.
#
#   scripts/new_api_key.sh ingest    # key for clients sending text (default)
#   scripts/new_api_key.sh helper    # key for the desktop review helper
#
# Prints the key once (for the client) and writes only its SHA-256 hash to
# secrets/ingest.env. Run `docker compose up -d ingest` to apply. A new key
# replaces the old one.
set -eu

cd "$(dirname "$0")/.."
. scripts/lib.sh

case "${1:-ingest}" in
    ingest) var=INGEST_API_KEY_HASH ;;
    helper) var=HELPER_API_KEY_HASH ;;
    *) echo "usage: $0 [ingest | helper]" >&2; exit 2 ;;
esac

key=$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 43)
hash=$(printf '%s' "$key" | sha256sum | cut -d' ' -f1)
set_secret ingest.env "$var" "$hash"

echo "API key (put this in your ${1:-ingest} client, shown once):"
echo "  $key"
echo
echo "Wrote $var to secrets/ingest.env. Apply with: docker compose up -d ingest"
