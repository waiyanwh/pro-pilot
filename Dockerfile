# syntax=docker/dockerfile:1.7

# Python 3.13.16 slim Bookworm, multi-platform index digest verified 2026-10-07.
ARG PYTHON_IMAGE=python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
# Official Astral uv 0.12.23, multi-platform index digest verified 2026-10-07.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS source

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Keep the builder context limited to package metadata and runtime code. The
# final images copy only the virtual environment from these intermediate stages.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY pro_pilot ./pro_pilot

FROM source AS build-hub

COPY --from=uv /uv /uvx /usr/local/bin/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra hub --no-editable \
    && test -f /app/.venv/lib/python3.13/site-packages/pro_pilot/hub/prompts/system.md.j2

FROM source AS build-spoke

COPY --from=uv /uv /uvx /usr/local/bin/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra spoke --no-editable \
    && test -f /app/.venv/lib/python3.13/site-packages/pro_pilot/hub/prompts/system.md.j2

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/tmp

WORKDIR /app

# Runtime configuration is supplied by a Secret/ConfigMap or a local bind
# mount. No credentials or environment-specific YAML is included in the image.
RUN groupadd --gid 10001 pro-pilot \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin pro-pilot \
    && mkdir -p /etc/pro-pilot \
    && rm -rf \
        /usr/local/bin/pip* \
        /usr/local/lib/python3.13/ensurepip \
        /usr/local/lib/python3.13/site-packages/pip \
        /usr/local/lib/python3.13/site-packages/pip-*.dist-info

FROM runtime AS hub

COPY --from=build-hub /app/.venv /app/.venv
RUN rm -f /app/.venv/bin/pro-pilot-eval

EXPOSE 8765 8080
USER 10001:10001
ENTRYPOINT ["pro-pilot-hub", "--config", "/etc/pro-pilot/config.yaml"]

FROM runtime AS spoke

COPY --from=build-spoke /app/.venv /app/.venv
RUN rm -f /app/.venv/bin/pro-pilot-eval

EXPOSE 8080
USER 10001:10001
ENTRYPOINT ["pro-pilot-spoke", "--config", "/etc/pro-pilot/config.yaml"]
