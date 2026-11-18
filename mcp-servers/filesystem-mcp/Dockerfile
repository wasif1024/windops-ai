# Build from the repository root, because servers share the uv workspace:
#   docker build -f mcp-servers/filesystem-mcp/Dockerfile -t windops-mcp-filesystem:local .
FROM python:3.13-slim-bookworm@sha256:413041be39649714885528a06586416279db85744b6dcdf290af0ae557d782f6 AS build
RUN pip install --no-cache-dir uv==0.12.18
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY shared shared
COPY services services
COPY simulator simulator
COPY mcp-servers mcp-servers
RUN uv sync --locked --no-dev --no-editable --package windops-mcp-filesystem

FROM python:3.13-slim-bookworm@sha256:413041be39649714885528a06586416279db85744b6dcdf290af0ae557d782f6 AS runtime
# The server never installs packages, so remove the base image's installers and their CVEs.
RUN python -m pip uninstall --yes --quiet --root-user-action=ignore pip setuptools wheel
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Its sandboxes, read-only in the image: the synthetic documents and the evaluation fixtures.
COPY simulator/src/windops_simulator/documents /app/fixtures/documents
COPY evaluations /app/fixtures/evaluations
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 50051 8080 8443
CMD ["python", "-m", "windops_mcp_filesystem.main"]
