#!/bin/sh
# One-time setup of the factory's background service on Fly.io (ENG-194).
# Run from the root of the software-factory checkout, on main, after
# `fly auth signup` (or `fly auth login`). Safe to run again: steps already
# done are skipped. It stops only where you must act:
#   1. pasting the read-only GitHub token (hidden, never shown or saved),
#   2. typing the approval code for the qualification contract.
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
# A clean export of the current commit, so untracked files never reach the image.
BUILD=$(mktemp -d)
git archive --format=tar HEAD | (cd "$BUILD" && tar -xf -)
fly deploy "$BUILD" --app "$APP" --config "$BUILD/deploy/fly/fly.toml" \
    --dockerfile "$BUILD/deploy/fly/Dockerfile" --ha=false
rm -rf "$BUILD"

echo "== Approve the qualification contract (type the code it shows)"
fly ssh console --app "$APP" --pty \
    -C "/app/factory approve /app/deploy/qualification/contracts/QUAL-1.json"

echo "== Usage reading for the practice run"
# The practice run's worker is fake and spends nothing, and its ledger
# (HOME=/data/qualification) is separate from the real one, so it records a
# zero reading instead of asking. Real workers will need a real reading.
fly ssh console --app "$APP" -C "/app/factory snapshot 0 0 0"

echo
echo "Done. You can close the laptop. The service picks up the qualification"
echo "ticket within a few minutes and starts its fake worker once."
