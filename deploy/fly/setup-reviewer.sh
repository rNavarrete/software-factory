#!/bin/sh
# Turns on the factory's independent reviewer (ENG-156, docs/review.md).
# Run from the root of the software-factory checkout, on main, after the
# reviewer's GitHub account and its claude.ai routine exist. Safe to run
# again: steps already done are skipped. It stops only where you must act:
#   1. typing the reviewer's GitHub login and the routine's trig_ id,
#   2. pasting the routine's start key (hidden, never shown or saved).
set -eu
PATH="$HOME/.fly/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
APP=rnavarrete-factory

if [ ! -f deploy/fly/fly.toml ]; then
    echo "Run this from the root of the software-factory folder." >&2
    exit 1
fi

have() { fly secrets list --app "$APP" | grep -q "$1"; }

echo "== Reviewer GitHub account"
if ! have FACTORY_REVIEWER_LOGIN; then
    printf "The reviewer's GitHub login (the new account, not yours or the bot's): "
    read -r LOGIN </dev/tty
    # Checks it's a plain user with no write access to the pilot repo.
    python3 -m controller.review qualify-identity "$LOGIN"
    printf 'FACTORY_REVIEWER_LOGIN=%s\n' "$LOGIN" | fly secrets import --app "$APP" --stage
fi

echo "== Reviewer routine"
if ! have FACTORY_REVIEWER_ROUTINE; then
    printf "The reviewer routine's id (starts with trig_): "
    read -r ROUTINE </dev/tty
    case "$ROUTINE" in
        trig_*) ;;
        *) echo "That doesn't start with trig_." >&2; exit 1 ;;
    esac
    printf 'FACTORY_REVIEWER_ROUTINE=%s\n' "$ROUTINE" | fly secrets import --app "$APP" --stage
fi

echo "== Reviewer start key"
if ! have FACTORY_REVIEWER_TOKEN; then
    echo "Paste the reviewer routine's start key and press Enter (nothing will show):"
    stty -echo </dev/tty
    read -r TOKEN </dev/tty
    stty echo </dev/tty
    printf 'FACTORY_REVIEWER_TOKEN=%s\n' "$TOKEN" | fly secrets import --app "$APP" --stage
    unset TOKEN
fi

echo "== Deploy (one machine only)"
fly deploy . --app "$APP" --config deploy/fly/fly.toml \
    --dockerfile deploy/fly/Dockerfile --ha=false

echo
echo "Done. The reviewer starts on the next worker PR. Nothing has been fired."
