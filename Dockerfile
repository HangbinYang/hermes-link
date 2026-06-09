# syntax=docker/dockerfile:1.7

# Multi-stage build of hermes-link as a self-contained image for self-hosters.
# Runtime: FastAPI service started via `hermes-link start --foreground`.
# Listens on the port configured in the link config (default in code).

FROM python:3.11-slim AS builder

WORKDIR /build

# Install build deps for native wheels (cryptography etc).
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

# Build a wheel + collect dependency wheels into /wheels for the runtime stage.
RUN pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.11-slim AS runtime

# Non-root by default. Override at runtime with HERMES_UID/HERMES_GID via the
# container runtime (--user) if the operator needs a specific uid/gid.
RUN useradd --create-home --uid 10001 --user-group hermes

# curl for image-level healthchecks; ca-certificates for upstream relay TLS.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

# Persistent state goes here; mount a volume to keep pairing/tokens across restarts.
ENV HERMES_LINK_INSTALL_ROOT=/home/hermes/.local/share/hermes-link \
    PYTHONUNBUFFERED=1

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels hermes-link \
    && rm -rf /wheels

USER hermes
WORKDIR /home/hermes

# Default companion-service port. Override via the link config if you've changed it.
EXPOSE 8765

# Foreground mode keeps PID 1 as the service so container lifecycle works.
ENTRYPOINT ["hermes-link"]
CMD ["start", "--foreground"]
