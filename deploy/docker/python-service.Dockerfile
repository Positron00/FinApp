# syntax=docker/dockerfile:1
# One image template for every Python service (platform LLD §10.1; PAY-NFR-13).
# Build from the repo root:
#   docker build -f deploy/docker/python-service.Dockerfile \
#     --build-arg PACKAGE=payroll --build-arg SERVICE_DIR=payroll -t payrun/payroll:dev .
# PACKAGE is the uv workspace member and import package (identity, payroll, payout_worker,
# bank_stub); SERVICE_DIR is its folder under services/ (the same names today).
# Base images are pinned by version (§10.3); CI adds the digest when it pins for release.
ARG PYTHON_IMAGE=python:3.14.9-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.13

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
ARG PACKAGE
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# uv checks the lock against every workspace member's pyproject, so libs/ and services/ both come in.
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN --mount=type=cache,target=/root/.cache/uv \
    test -n "${PACKAGE}" && \
    uv sync --locked --no-dev --package "${PACKAGE}" --no-editable

FROM ${PYTHON_IMAGE}
ARG PACKAGE
ARG SERVICE_DIR
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="payrun-${PACKAGE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/Positron00/FinApp"
# Apply Debian security updates and drop pip from the runtime image: the app never installs at
# runtime, and pip's vendored urllib3/msgpack/setuptools carry fixable CVEs (Trivy gate, S9).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall --yes --no-input pip \
 && useradd --system --uid 10001 --gid 0 --no-create-home app
WORKDIR /app
# Same path as the build stage, so the venv's console scripts (alembic) keep working.
COPY --from=build /app/.venv /app/.venv
COPY services/${SERVICE_DIR}/alembic.ini /app/alembic.ini
COPY services/${SERVICE_DIR}/migrations /app/migrations
COPY contracts /app/contracts
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OTEL_SEMCONV_STABILITY_OPT_IN=http \
    PYCOMMON_APP=${PACKAGE}.main:app PORT=8000 \
    APP_VERSION=${VERSION}
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "pycommon.serve"]
