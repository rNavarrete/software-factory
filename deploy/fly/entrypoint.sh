#!/bin/sh
# Runs at every machine start. Moves the Fly secrets out of the environment
# into private files, starts the signer (the only process that keeps the
# approval key) and runs the service as its own user. Nothing here prints a
# secret.
set -eu
umask 077
mkdir -p "$HOME/.software-factory"
# Root's copy, for Rolando's commands over `fly ssh console` and the signer.
python3 -m controller.service install-secrets "$FACTORY_SECRETS_DIR"
# The service's copy: never the approval key.
python3 -m controller.service install-secrets /run/factory-service-secrets \
    --only routine-token,github-token,linear-key --owner factory
for name in APPROVAL_KEY ROUTINE_TOKEN GITHUB_TOKEN LINEAR_KEY; do
    unset "FACTORY_$name"
done
chown -R factory:factory "$HOME"
python3 -m controller.service signer --socket /run/factory-signer/signer.sock &
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
# Until the Linear integrations land, the service runs the qualification
# fixture with the fake worker: it never starts a real worker.
env FACTORY_SECRETS_DIR=/run/factory-service-secrets \
    FACTORY_SIGNER_SOCKET=/run/factory-signer/signer.sock \
    python3 -m controller.service run --user factory \
    --fixtures /app/deploy/qualification \
    --onboarding /app/deploy/qualification/onboarding.json &
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
