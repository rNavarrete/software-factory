# The factory's background service on Fly.io (ENG-194). See docs/service.md.
# Standard library only: no pip install, no other packages.
FROM python:3.13-slim

# HOME holds the ledger (~/.software-factory). The qualification run gets its
# own folder so its fake worker never counts against the real fire limits;
# the real service will use /data/factory.
ENV PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data/qualification \
    FACTORY_SECRETS_DIR=/run/factory-secrets

# PYTHONNOUSERSITE (and -s in the scripts): root's Python never loads code
# from $HOME, which belongs to the service's user.
# Two users (ENG-174): the service runs as `factory`; the approval key lives
# only in the signer process, which runs as `factory-signer`.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin factory \
 && useradd --system --no-create-home --shell /usr/sbin/nologin factory-signer

WORKDIR /app
COPY controller /app/controller
COPY deploy/qualification /app/deploy/qualification
COPY deploy/fly/entrypoint.sh /app/entrypoint.sh
COPY --chmod=755 deploy/fly/factory /app/factory

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
