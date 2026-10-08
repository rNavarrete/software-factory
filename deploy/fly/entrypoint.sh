#!/bin/sh
# Runs at every machine start. Moves the Fly secrets out of the environment
# into private files, starts the signer (the only process that keeps the
# approval key) and runs the service as its own user. Nothing here prints a
# secret.
set -eu
umask 077
# FACTORY_MODE picks what the service runs. Unset, it is the qualification
# fixture with the fake worker, which never starts a real worker. `live`
# reads Todo moves, drafts contracts and posts progress through Linear, with
# its own ledger in /data/factory; the pilot's onboarding file keeps intake
# off until that file says otherwise.
case "${FACTORY_MODE:-qualification}" in
qualification)
    HOME=/data/qualification
    ONBOARDING=/app/deploy/qualification/onboarding.json
    set -- --fixtures /app/deploy/qualification
    ;;
live)
    HOME=/data/factory
    ONBOARDING=/app/deploy/pilot/onboarding.json
    set -- --live --real-runtime
    ;;
*)
    echo "unknown FACTORY_MODE: use qualification or live" >&2
    exit 1
    ;;
esac
export HOME
mkdir -p "$HOME/.software-factory"
# Rolando's commands over `fly ssh console` (/app/factory) use the same home.
printf '%s\n' "$HOME" >/run/factory-home
# Root's copy, for Rolando's commands over `fly ssh console` and the signer.
python3 -s -m controller.service install-secrets "$FACTORY_SECRETS_DIR"
# The service's copy: never the approval key.
python3 -s -m controller.service install-secrets /run/factory-service-secrets \
    --only routine-token,github-token,linear-key --owner factory
for name in APPROVAL_KEY ROUTINE_TOKEN GITHUB_TOKEN LINEAR_KEY; do
    unset "FACTORY_$name"
done
chown -R factory:factory "$HOME"
# The one onboarding file both read ($ONBOARDING, above). It is in the image,
# owned by root, so the service can't widen what the signer will approve.
python3 -s -m controller.service signer --socket /run/factory-signer/signer.sock \
    --onboarding "$ONBOARDING" --state /data/signer/moves.json &
signer=$!
i=0
until [ -S /run/factory-signer/signer.sock ]; do
    i=$((i + 1))
    if [ "$i" -gt 100 ] || ! kill -0 "$signer" 2>/dev/null; then
        echo "the signer did not start" >&2
        exit 1
    fi
    sleep 0.1
done
env FACTORY_SECRETS_DIR=/run/factory-service-secrets \
    FACTORY_SIGNER_SOCKET=/run/factory-signer/signer.sock \
    python3 -s -m controller.service run --user factory \
    --onboarding "$ONBOARDING" "$@" &
service=$!
# A stop request lets the service finish its round. If either process ends,
# both stop and the machine exits, so Fly starts it again with both.
trap 'kill -TERM "$service" 2>/dev/null; wait "$service"; kill -TERM "$signer" 2>/dev/null; exit 0' TERM INT
while kill -0 "$signer" 2>/dev/null && kill -0 "$service" 2>/dev/null; do
    sleep 5 &
    wait $!
done
echo "the signer or the service stopped; stopping both" >&2
kill -TERM "$service" "$signer" 2>/dev/null || true
wait
exit 1
