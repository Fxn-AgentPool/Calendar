#!/bin/sh
# Generate a new API key for the ingest endpoint.
#
# Prints the key (for your client) and its SHA-256 hash (for .env). Only the
# hash is stored on the server. After changing INGEST_API_KEY_HASH, run
# `docker compose up -d ingest` to apply it.
set -eu

key=$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 43)
hash=$(printf '%s' "$key" | sha256sum | cut -d' ' -f1)

echo "API key (put this in your client, shown once):"
echo "  $key"
echo
echo "Put this in .env:"
echo "  INGEST_API_KEY_HASH=$hash"
