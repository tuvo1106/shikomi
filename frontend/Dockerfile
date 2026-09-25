# Web image — builds the SPA, then serves it via Caddy (DESIGN.md §9).
#
# Stage 1 compiles the static bundle with pnpm; stage 2 is just Caddy + the built
# files + the Caddyfile. Caddy serves the SPA and reverse-proxies /api/* to the
# api service, so the browser talks to a single origin (no CORS, cookies "just
# work") and Caddy handles TLS in prod.
FROM node:22-slim AS build

WORKDIR /app

# Deps first (own layer) — only re-installs when the lockfile changes. Corepack
# installs the exact pnpm pinned in package.json's packageManager field.
COPY package.json pnpm-lock.yaml ./
RUN corepack enable && corepack install && pnpm install --frozen-lockfile

COPY . .
# The SPA calls a relative /api/v1 (see src/api/client.ts), so there's no
# build-time API URL to bake in — Caddy decides where /api goes at runtime.
RUN pnpm build


FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
