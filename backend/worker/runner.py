"""Selects the judge sandbox backend at runtime (DESIGN.md §5.5).

Two implementations share one interface — `run_in_container(...)` returning a
`ContainerResult`, plus `sweep_orphans()`:

* `docker_runner` — `docker run` against the host daemon. The default; used by the
  compose stack and a single-VPS deploy.
* `k8s_runner` — a per-submission Kubernetes Pod, for running under Kubernetes
  where there is no host Docker socket (and mounting one would be a privilege-
  escalation hole for untrusted code).

Callers import *this* module, never a concrete runner, so the judge code is
identical regardless of backend. `JUDGE_RUNNER` (config) picks the implementation.
"""
from app.config import get_settings

from worker import docker_runner

settings = get_settings()


def _impl():
    """Return the configured runner module (imported lazily so the docker path
    never imports the kubernetes client, and vice versa)."""
    if settings.judge_runner == "k8s":
        from worker import k8s_runner
        return k8s_runner
    return docker_runner


async def run_in_container(payload_json, **kwargs):
    """Run one judge payload in the configured sandbox; returns a ContainerResult."""
    return await _impl().run_in_container(payload_json, **kwargs)


async def sweep_orphans():
    """Reap sandboxes orphaned by a previous crash (worker startup + sweep cron)."""
    return await _impl().sweep_orphans()
