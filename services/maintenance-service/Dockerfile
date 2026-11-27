# Build from the repository root, because services share the uv workspace:
#   docker build -f services/maintenance-service/Dockerfile -t windops-maintenance:local .
FROM python:3.13-slim-bookworm@sha256:413041be39649714885528a06586416279db85744b6dcdf290af0ae557d782f6 AS build
RUN pip install --no-cache-dir uv==0.12.18
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY shared shared
COPY services services
COPY simulator simulator
COPY mcp-servers mcp-servers
RUN uv sync --locked --no-dev --no-editable --package windops-maintenance

FROM python:3.13-slim-bookworm@sha256:413041be39649714885528a06586416279db85744b6dcdf290af0ae557d782f6 AS runtime
# Debian's security fixes, which the pinned base image may not have yet.
RUN apt-get update && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
# The service never installs packages, so remove the base image's installers and their CVEs.
RUN python -m pip uninstall --yes --quiet --root-user-action=ignore pip setuptools wheel
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 50051 8080
CMD ["python", "-m", "windops_maintenance.main"]
