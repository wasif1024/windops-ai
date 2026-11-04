# Build from the repository root, because services share the uv workspace:
#   docker build -f services/sample-caller-service/Dockerfile -t windops-sample-caller:local .
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS build
RUN pip install --no-cache-dir uv==0.12.18
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY shared shared
COPY services services
RUN uv sync --locked --no-dev --no-editable --package windops-sample-caller

FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS runtime
# The service never installs packages, so remove the base image's installers and their CVEs.
RUN python -m pip uninstall --yes --quiet --root-user-action=ignore pip setuptools wheel
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 50051 8080
CMD ["python", "-m", "windops_sample_caller.main"]
