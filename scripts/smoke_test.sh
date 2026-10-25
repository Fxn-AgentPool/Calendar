#!/bin/sh
# Checks that Radicale is up and that the accounts get exactly the access the
# design intends. Creates and then deletes one test event.
#
#   OWNER_PASS=... INGEST_PASS=... AGENDA_PASS=... scripts/smoke_test.sh [base_url]
#
# base_url defaults to http://127.0.0.1:5232; use https://cal.<domain> to test
# through Pangolin. Accounts without a password in the environment are skipped.
set -eu

cd "$(dirname "$0")/.."
[ -f .env ] && . ./.env

base=${1:-http://127.0.0.1:5232}
owner=${CAL_USER:?set CAL_USER (or create .env)}
cal="$base/$owner/personal"
uid="smoke-test-$(date +%s)"
fails=0

status() {
    # status <user> <password> <method> <url> [body]
    curl -s -o /dev/null -w '%{http_code}' -u "$1:$2" -X "$3" \
        -H 'Content-Type: text/calendar; charset=utf-8' -H 'Depth: 0' \
        ${5:+--data-binary "$5"} "$4"
}

expect() {
    # expect <description> <expected codes, e.g. "200 207"> <actual>
    case " $2 " in
        *" $3 "*) echo "ok    $1 ($3)" ;;
        *) echo "FAIL  $1: expected $2, got $3"; fails=$((fails + 1)) ;;
    esac
}

event() {
    printf 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//calendar//smoke-test//EN\r\nBEGIN:VEVENT\r\nUID:%s\r\nDTSTAMP:20260101T000000Z\r\nDTSTART:20300101T100000Z\r\nDTEND:20300101T110000Z\r\nSUMMARY:Smoke test\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n' "$1"
}

expect "no login is refused" "401" \
    "$(curl -s -o /dev/null -w '%{http_code}' -X PROPFIND "$cal/")"
expect "wrong password is refused" "401 403" "$(status "$owner" wrong-password PROPFIND "$cal/")"

if [ -n "${OWNER_PASS:-}" ]; then
    # The first owner login creates the "personal" calendar.
    expect "owner reads principal" "207" "$(status "$owner" "$OWNER_PASS" PROPFIND "$base/$owner/")"
    expect "owner reads personal" "207" "$(status "$owner" "$OWNER_PASS" PROPFIND "$cal/")"
    expect "owner creates event" "201" \
        "$(status "$owner" "$OWNER_PASS" PUT "$cal/$uid-owner.ics" "$(event "$uid-owner")")"
    expect "owner deletes event" "200 204" "$(status "$owner" "$OWNER_PASS" DELETE "$cal/$uid-owner.ics")"
fi

if [ -n "${INGEST_PASS:-}" ]; then
    expect "ingest creates event in personal" "201" \
        "$(status ingest "$INGEST_PASS" PUT "$cal/$uid-ingest.ics" "$(event "$uid-ingest")")"
    expect "ingest cannot write other calendars" "403" \
        "$(status ingest "$INGEST_PASS" PUT "$base/$owner/other/$uid.ics" "$(event "$uid")")"
    expect "ingest lists owner's calendars (conflict check)" "207" \
        "$(status ingest "$INGEST_PASS" PROPFIND "$base/$owner/")"
    expect "ingest cannot delete the calendar" "401 403" \
        "$(status ingest "$INGEST_PASS" DELETE "$cal/")"
fi

if [ -n "${AGENDA_PASS:-}" ]; then
    expect "agenda lists owner's calendars" "207" "$(status agenda "$AGENDA_PASS" PROPFIND "$base/$owner/")"
    expect "agenda reads personal" "207" "$(status agenda "$AGENDA_PASS" PROPFIND "$cal/")"
    expect "agenda cannot create events" "403" \
        "$(status agenda "$AGENDA_PASS" PUT "$cal/$uid-agenda.ics" "$(event "$uid-agenda")")"
    if [ -n "${INGEST_PASS:-}" ]; then
        expect "agenda cannot delete events" "403" \
            "$(status agenda "$AGENDA_PASS" DELETE "$cal/$uid-ingest.ics")"
    fi
fi

# Clean up the ingest test event with the owner account.
if [ -n "${INGEST_PASS:-}" ] && [ -n "${OWNER_PASS:-}" ]; then
    expect "owner removes ingest test event" "200 204" \
        "$(status "$owner" "$OWNER_PASS" DELETE "$cal/$uid-ingest.ics")"
fi

if [ "$fails" -gt 0 ]; then
    echo "$fails check(s) failed"
    exit 1
fi
echo "all checks passed"
