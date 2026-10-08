#!/bin/sh
# One-time setup of the factory's background service on Fly.io (ENG-194).
# Run from the root of the software-factory checkout, on main, after
# `fly auth signup` (or `fly auth login`). Safe to run again: steps already
# done are skipped. It stops and asks only where you must act:
#   1. pasting the read-only GitHub token (hidden, never shown or saved),
#   2. typing the approval code for the qualification contract,
#   3. typing your current usage numbers.
set -eu
# flyctl's own installer puts it here; Homebrew's folders are usually on PATH.
PATH="$HOME/.fly/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
APP=rnavarrete-factory
REGION=iad

if [ ! -f deploy/fly/fly.toml ]; then
    echo "Run this from the root of the software-factory folder." >&2
    exit 1
fi

echo "== App"
# Listing the app's volumes works only if the app exists and is yours.
fly volumes list --app "$APP" >/dev/null 2>&1 || fly apps create "$APP"

echo "== Volume (1 GB, daily snapshots kept 14 days)"
if ! fly volumes list --app "$APP" | grep -q factory_data; then
    fly volumes create factory_data --app "$APP" --region "$REGION" --size 1 \
        --snapshot-retention 14 --yes
fi

echo "== Approval key (made here, never shown)"
if ! fly secrets list --app "$APP" | grep -q FACTORY_APPROVAL_KEY; then
    printf 'FACTORY_APPROVAL_KEY=%s\n' "$(openssl rand -hex 32)" |
        fly secrets import --app "$APP" --stage
fi

echo "== Read-only GitHub token"
if ! fly secrets list --app "$APP" | grep -q FACTORY_GITHUB_TOKEN; then
    echo "Paste the read-only GitHub token and press Enter (nothing will show):"
    stty -echo </dev/tty
    read -r TOKEN </dev/tty
    stty echo </dev/tty
    printf 'FACTORY_GITHUB_TOKEN=%s\n' "$TOKEN" | fly secrets import --app "$APP" --stage
    unset TOKEN
fi

echo "== Deploy (one machine only)"
fly deploy . --app "$APP" --config deploy/fly/fly.toml \
    --dockerfile deploy/fly/Dockerfile --ha=false

echo "== Approve the qualification contract (type the code it shows)"
fly ssh console --app "$APP" --pty \
    -C "/app/factory approve /app/deploy/qualification/contracts/QUAL-1.json"

echo "== Usage reading (claude.ai usage page of the factory account)"
printf "Current session usage %%: "
read -r SESSION </dev/tty
printf "Weekly usage %%: "
read -r WEEKLY </dev/tty
case "$SESSION$WEEKLY" in
    *[!0-9.]* | "") echo "Numbers only, please. Run this script again." >&2; exit 1 ;;
esac
# 0 credits spent: usage credits are turned off on the factory account.
fly ssh console --app "$APP" -C "/app/factory snapshot $SESSION $WEEKLY 0"

echo
echo "Done. You can close the laptop. The service picks up the qualification"
echo "ticket within a few minutes and starts its fake worker once."
