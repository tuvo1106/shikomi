"""Runs a judge payload as an ephemeral, locked-down Kubernetes Pod.

The Kubernetes analogue of `docker_runner` (DESIGN.md §5.5). Under k8s there is no
host Docker socket to shell `docker run` against — and mounting one into the
worker would hand any container-escape the whole node. So instead the worker uses
the Kubernetes API to create one **short-lived Pod per submission** running the
judge image, then reads its logs and exit status.

Every `docker run` lockdown flag maps to a Pod primitive:

    --network=none          → a deny-all NetworkPolicy on `app=judge` pods
    --memory (swap off)     → resources.limits.memory (cgroup, no swap in k8s)
    --cpus                  → resources.limits.cpu
    --read-only + tmpfs     → readOnlyRootFilesystem + an emptyDir(Memory) at /tmp
    --cap-drop=ALL          → securityContext.capabilities.drop [ALL]
    no-new-privileges       → allowPrivilegeEscalation: false
    --user 1000             → runAsNonRoot / runAsUser 1000
    (host isolation)        → runtimeClassName: gvisor  (prod node pool; see notes)

Payload in: there's no stdin pipe to a Pod, so the payload rides in as a ConfigMap
mounted read-only and the harness reads it via `JUDGE_PAYLOAD_FILE`. Verdict out:
the harness writes its JSON report to stdout, which we read back with the Pod log
API (clean JSON on every real verdict — per-case errors are captured *into* that
JSON, not onto stderr).

The kubernetes client is synchronous, so each call hops to a thread via
`asyncio.to_thread` to avoid blocking the worker's event loop.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.judge_budget import MAX_LIVE_SANDBOX_AGE_S
from worker.docker_runner import MAX_STDOUT_BYTES, ContainerResult

settings = get_settings()

logger = logging.getLogger(__name__)
NAMESPACE = settings.judge_namespace
PAYLOAD_DIR = "/payload"
PAYLOAD_FILE = f"{PAYLOAD_DIR}/payload.json"
JUDGE_LABEL = {"app": "judge"}
POLL_INTERVAL_S = 0.4
# Optional: pin judge pods to an isolated runtime (gVisor/Kata) in prod. Unset
# locally (kind has no such RuntimeClass), so pods use the default runtime.
RUNTIME_CLASS = settings.judge_runtime_class

# Pod/ConfigMap names this process currently has a submission running in —
# mirrors `docker_runner._active` (see its docstring). `sweep_orphans` skips
# anything in here so it doesn't delete a pod that's still legitimately
# judging out from under it. This is the precise, in-process half of the guard; the age rule
# in `_sweep_sync` is the cross-replica half (another replica's pods aren't in here).
_active: set[str] = set()


def _load_config():
    """Load in-cluster credentials (the worker's ServiceAccount), falling back to
    a local kubeconfig for out-of-cluster development."""
    from kubernetes import config
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _build_pod(name, image, cm_name, memory_mb, cpus, wall_timeout_s, tmpfs_size_mb=16):
    """Construct the locked-down judge Pod object (see the module docstring for the
    flag-by-flag mapping from `docker run`). `cm_name` is the payload ConfigMap
    mounted read-only at /payload; `wall_timeout_s` sets the pod-level
    `activeDeadlineSeconds` backstop above the worker's own kill."""
    from kubernetes import client
    container = client.V1Container(
        name="judge",
        image=image,
        image_pull_policy="Never",   # image is loaded into the node, not pulled
        env=[client.V1EnvVar(name="JUDGE_PAYLOAD_FILE", value=PAYLOAD_FILE)],
        resources=client.V1ResourceRequirements(
            requests={"cpu": "50m", "memory": f"{memory_mb}Mi"},
            limits={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
        ),
        security_context=client.V1SecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=client.V1Capabilities(drop=["ALL"]),
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        ),
        volume_mounts=[
            client.V1VolumeMount(name="payload", mount_path=PAYLOAD_DIR, read_only=True),
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
        ],
    )
    spec = client.V1PodSpec(
        restart_policy="Never",
        # The judge must not be able to talk to the API server.
        automount_service_account_token=False,
        # Backstop wall clock (the worker also kills from outside; see run_in_container).
        active_deadline_seconds=int(wall_timeout_s) + 5,
        runtime_class_name=RUNTIME_CLASS,
        containers=[container],
        volumes=[
            client.V1Volume(name="payload", config_map=client.V1ConfigMapVolumeSource(name=cm_name)),
            client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource(
                medium="Memory", size_limit=f"{tmpfs_size_mb}Mi")),
        ],
    )
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, labels=JUDGE_LABEL), spec=spec)


def _terminated_state(v1, name):
    """Return the judge container's terminated state, or None if not finished."""
    pod = v1.read_namespaced_pod_status(name, NAMESPACE)
    statuses = pod.status.container_statuses or []
    for cs in statuses:
        if cs.state and cs.state.terminated:
            return cs.state.terminated
    # A pod that failed to even start (e.g. deadline exceeded) reports at pod level.
    if pod.status.phase in ("Succeeded", "Failed") and not statuses:
        return "no-container"
    return None


def _run_sync(payload_json, image, name, memory_mb, cpus, wall_timeout_s, tmpfs_size_mb=16,
              cancel=None):
    """The blocking body of one judge run (called in a thread): create the payload
    ConfigMap + Pod, poll to termination (or wall-clock timeout), read the verdict
    from the Pod log, and always clean both up. Returns a `ContainerResult` shaped
    exactly like the docker runner's so aggregation is shared.

    `cancel` is set by the async caller when its job is cancelled: a thread can't be cancelled,
    so the polling loop checks this flag and stops early (the `finally` then tears the pod down)."""
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    cancel = cancel or threading.Event()
    _load_config()
    v1 = client.CoreV1Api()
    cm_name = name  # judge-<submission_id>

    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=cm_name, labels=JUDGE_LABEL),
        data={"payload.json": payload_json})
    v1.create_namespaced_config_map(NAMESPACE, cm)

    timed_out = False
    exit_code = -1
    try:
        v1.create_namespaced_pod(NAMESPACE, _build_pod(
            name, image, cm_name, memory_mb, cpus, wall_timeout_s, tmpfs_size_mb))

        deadline = time.monotonic() + wall_timeout_s
        term = None
        while time.monotonic() < deadline and not cancel.is_set():
            term = _terminated_state(v1, name)
            if term is not None:
                break
            time.sleep(POLL_INTERVAL_S)

        if term is None:
            timed_out = True   # our wall clock fired; the pod is deleted in finally
        elif term != "no-container":
            # OOM in k8s surfaces as reason=OOMKilled (exit 137); normalise to the
            # same 137 docker_runner uses so ContainerResult.oom_killed works.
            exit_code = 137 if term.reason == "OOMKilled" else (term.exit_code or 0)

        try:
            # _preload_content=False is load-bearing: the client declares this
            # endpoint's type as `str`, but with preloading it deserializes a
            # JSON-looking log body into a dict and str()s it (single quotes, None)
            # — which then fails json.loads. Read the raw bytes instead.
            resp = v1.read_namespaced_pod_log(name, NAMESPACE, _preload_content=False)
            stdout = resp.data.decode("utf-8", "replace")
        except ApiException:
            stdout = ""   # pod may have been reaped before logs were readable
    finally:
        _delete(v1, name, cm_name)

    encoded = stdout.encode("utf-8", "replace")
    truncated = len(encoded) > MAX_STDOUT_BYTES
    return ContainerResult(
        stdout=encoded[:MAX_STDOUT_BYTES].decode("utf-8", "replace"),
        stderr="",   # pod logs merge streams; real verdicts keep stdout clean
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_truncated=truncated,
    )


def _delete(v1, pod_name, cm_name):
    """Best-effort teardown of a run's Pod + payload ConfigMap (grace 0 = don't
    wait). Swallows NotFound so cleanup is idempotent."""
    from kubernetes.client.rest import ApiException
    for fn, arg in ((v1.delete_namespaced_pod, pod_name),
                    (v1.delete_namespaced_config_map, cm_name)):
        try:
            fn(arg, NAMESPACE, grace_period_seconds=0)
        except ApiException:
            pass


def _delete_run(name):
    """Tear down one run's Pod + payload ConfigMap by name (a fresh client; idempotent).

    Called when the job is cancelled: the polling thread may still be mid-request, and this
    guarantees the sandbox is gone without waiting for it to notice the cancel flag."""
    from kubernetes import client
    _load_config()
    _delete(client.CoreV1Api(), name, name)  # pod name == configmap name (see _run_sync)


async def run_in_container(payload_json, *, image, container_name,
                           memory_mb, cpus, pids_limit, wall_timeout_s, tmpfs_size_mb=16):
    """Run one submission in a per-submission Pod; returns a ContainerResult.

    Signature mirrors `docker_runner.run_in_container` so `worker.runner` can swap
    the two transparently. `pids_limit` has no portable per-Pod field in Kubernetes
    (it's a node/runtime setting), so it's accepted for interface parity and
    enforced at the node/gVisor layer instead. `tmpfs_size_mb` *is* a real per-Pod
    field (the `tmp` emptyDir's `size_limit`) and is honored, not just accepted.
    """
    # Registered for the pod's whole lifetime so `sweep_orphans` can't mistake
    # it for an orphan while it's still ours — see `_active`'s docstring.
    _active.add(container_name)
    cancel = threading.Event()
    try:
        return await asyncio.to_thread(
            _run_sync, payload_json, image, container_name, memory_mb, cpus, wall_timeout_s,
            tmpfs_size_mb, cancel)
    except asyncio.CancelledError:
        # arq cancelled this job (its job_timeout, or the worker is shutting down). Cancelling
        # the awaiting task does not stop the worker *thread*, so it would keep polling (and the
        # pod keep running) until its own deadline. Tell the thread to stop, and delete the pod
        # ourselves right now (shielded so a second cancel can't abort the cleanup).
        cancel.set()
        try:
            await asyncio.shield(asyncio.to_thread(_delete_run, container_name))
        except Exception:
            # The API server being unreachable must not replace the cancellation with an
            # ordinary error (arq and judge_submission both key off CancelledError). The pod
            # is left for the age-based sweep.
            logger.exception("could not delete pod %s on cancel; the sweep will reap it",
                             container_name)
        raise
    finally:
        _active.discard(container_name)


def _sweep_sync():
    """Blocking body of sweep_orphans: delete `app=judge` Pods/ConfigMaps that no live job
    can own (worker startup + cron, §5.7).

    A pod/configmap is reaped only if **both** hold:

    * its name is not in `_active` (this process isn't waiting on it), and
    * it is older than `MAX_LIVE_SANDBOX_AGE_S` (by the API server's `creationTimestamp`).

    The second rule is what makes the sweep safe across worker replicas. Every KEDA-scaled
    replica runs this cron against the *whole namespace*, so `_active` alone (per-process)
    let replica A reap replica B's live pod mid-judge, turning a correct submission into a
    `judge_error`, and more often the more replicas there were. But a live sandbox belongs to a
    job arq cancels at `JUDGE_JOB_TIMEOUT_SECONDS`, and authoring refuses any problem whose run
    could outlast that (app/judge_budget.py), so no live pod is older than the bound; older
    means orphaned, whichever replica created it. No labels, extra RBAC or shared store needed.
    The cost: a pod or configmap leaked by a crashed replica lingers up to about six minutes
    (the pod has its own `activeDeadlineSeconds`, and a configmap is inert). Anything whose age
    can't be determined is left alone: a sweep must never delete what it can't age.

    Lists rather than bulk-deletes by label (`delete_collection_namespaced_*`) so each name can
    be checked first.
    """
    from kubernetes import client
    from kubernetes.client.rest import ApiException
    _load_config()
    v1 = client.CoreV1Api()
    selector = "app=judge"
    now = datetime.now(timezone.utc)
    try:
        pods = v1.list_namespaced_pod(NAMESPACE, label_selector=selector)
        cms = v1.list_namespaced_config_map(NAMESPACE, label_selector=selector)
        # Age by the *newest* of a name's pod and configmap (created back to back), so a young
        # half keeps the pair alive.
        newest: dict[str, datetime | None] = {}
        for item in list(pods.items) + list(cms.items):
            name, created = item.metadata.name, item.metadata.creation_timestamp
            if name not in newest or created is None:
                newest[name] = created
            elif newest[name] is not None:
                newest[name] = max(newest[name], created)
        for name, created in newest.items():
            if name in _active or created is None:
                continue
            if (now - created).total_seconds() > MAX_LIVE_SANDBOX_AGE_S:
                _delete(v1, name, name)  # pod name == configmap name (see _run_sync)
    except ApiException:
        pass


async def sweep_orphans():
    """Delete judge pods/configmaps left by a crashed worker (startup + cron)."""
    return await asyncio.to_thread(_sweep_sync)
