# Backend image — serves the API and runs the arq judge worker (DESIGN.md §7.2).
#
# Two build targets share one dependency install:
#   * `base`   — the api + migrate services (plain Python, no Docker access).
#   * `worker` — extends base with the Docker *CLI* so the worker can shell
#     `docker run` against the host daemon (the socket is bind-mounted in compose;
#     "docker-outside-of-docker"). Only the worker needs it, so api stays lean.
FROM python:3.12-slim AS base

# uv provides the venv + locked install (same tool as local dev).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Install deps first (own layer) so source edits don't re-run the resolver.
# --no-install-project: this repo is `package = false`; we run from source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

EXPOSE 8000
# Default command = the API. Overridden per service in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# --- worker: base + the Docker CLI (arch-aware, CLI only, no engine) ---------
FROM base AS worker

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

CMD ["arq", "worker.main.WorkerSettings"]
