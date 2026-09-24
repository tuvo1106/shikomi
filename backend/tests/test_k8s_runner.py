"""The k8s judge runner's Pod spec is security-critical — assert the sandbox
lockdown that replaces the `docker run` flags is actually present. No cluster is
needed: `_build_pod` just constructs the client object graph."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from worker import k8s_runner


def _pod():
    return k8s_runner._build_pod(
        name="judge-abc", image="shikomi-judge:latest", cm_name="judge-abc",
        memory_mb=256, cpus="1", wall_timeout_s=30)


def test_pod_is_locked_down():
    spec = _pod().spec
    assert spec.restart_policy == "Never"
    assert spec.automount_service_account_token is False   # can't reach the API
    assert spec.active_deadline_seconds == 35              # wall clock + slack backstop

    sc = spec.containers[0].security_context
    assert sc.read_only_root_filesystem is True
    assert sc.run_as_non_root is True
    assert sc.allow_privilege_escalation is False
    assert sc.capabilities.drop == ["ALL"]


def test_pod_limits_and_payload_channel():
    c = _pod().spec.containers[0]
    assert c.resources.limits["memory"] == "256Mi"
    assert c.image_pull_policy == "Never"
    # Payload arrives as a mounted file, not stdin (there's no pipe to a Pod).
    assert any(e.name == "JUDGE_PAYLOAD_FILE" for e in c.env)
    # /tmp is the only writable path: a memory-backed, size-capped emptyDir.
    tmp = next(v for v in _pod().spec.volumes if v.name == "tmp")
    assert tmp.empty_dir.medium == "Memory"


def test_oom_maps_to_137():
    # ContainerResult.oom_killed keys off exit 137 (docker's OOM signal); the k8s
    # runner normalises reason=OOMKilled to the same code so aggregation is shared.
    from worker.docker_runner import ContainerResult
    assert ContainerResult("", "", 137, False, False).oom_killed is True


def _mock_named(name):
    """An orphan-aged object: the sweep only reaps names older than MAX_LIVE_SANDBOX_AGE_S."""
    obj = MagicMock()
    obj.metadata.name = name
    obj.metadata.creation_timestamp = datetime.now(timezone.utc) - timedelta(hours=1)
    return obj


def test_sweep_orphans_skips_active_pods():
    """`_sweep_sync` must not delete a pod/configmap whose name is in `_active`
    — those are submissions this process is still legitimately judging when
    the cron fires, not orphans (a bulk `delete_collection_namespaced_pod` by
    label would delete every `app=judge` pod, including ones still mid-judge —
    see `_active`'s docstring)."""
    fake_v1 = MagicMock()
    fake_v1.list_namespaced_pod.return_value.items = [
        _mock_named("judge-live"), _mock_named("judge-dead")]
    fake_v1.list_namespaced_config_map.return_value.items = [
        _mock_named("judge-live"), _mock_named("judge-dead")]

    k8s_runner._active.add("judge-live")
    try:
        with patch.object(k8s_runner, "_load_config"), \
             patch("kubernetes.client.CoreV1Api", return_value=fake_v1):
            k8s_runner._sweep_sync()
    finally:
        k8s_runner._active.discard("judge-live")

    deleted_pods = {c.args[0] for c in fake_v1.delete_namespaced_pod.call_args_list}
    deleted_cms = {c.args[0] for c in fake_v1.delete_namespaced_config_map.call_args_list}
    assert deleted_pods == {"judge-dead"}
    assert deleted_cms == {"judge-dead"}
