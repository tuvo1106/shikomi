# Judge sandbox image (DESIGN.md §5.5). Stdlib only — no third-party packages
# are available to user code, and the container runs with no network.
FROM python:3.12-slim

RUN useradd -m -u 1000 runner
COPY harness.py /opt/judge/harness.py

USER runner
WORKDIR /home/runner
ENTRYPOINT ["python", "/opt/judge/harness.py"]
