# JS judge sandbox image (DESIGN.md §5.5, §13). Node stdlib only — no
# `npm install`, nothing installed beyond the runtime — and the container runs
# with no network, same lockdown posture as the Python image (judge/Dockerfile).
FROM node:20-slim

# node:20-slim already ships a non-root `node` user at uid/gid 1000 (unlike
# python:3.12-slim) — reuse it instead of creating a colliding one, so
# docker_runner's/k8s's hardcoded `--user 1000:1000` / `runAsUser: 1000` still
# lands on a real, unprivileged account.
COPY harness.js /opt/judge/harness.js

USER node
WORKDIR /home/node
ENTRYPOINT ["node", "/opt/judge/harness.js"]
