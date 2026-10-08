#!/bin/sh
# Runs at every machine start. Moves the Fly secrets out of the environment
# into private files, then runs the service. Nothing here prints a secret.
set -eu
umask 077
mkdir -p "$HOME"
python3 -m controller.service install-secrets "$FACTORY_SECRETS_DIR"
for name in APPROVAL_KEY ROUTINE_TOKEN GITHUB_TOKEN LINEAR_KEY; do
    unset "FACTORY_$name"
done
# Until the Linear integrations land, the service runs the qualification
# fixture with the fake worker: it never starts a real worker.
exec python3 -m controller.service run --fixtures /app/deploy/qualification \
    --onboarding /app/deploy/qualification/onboarding.json
