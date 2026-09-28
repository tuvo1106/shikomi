"""Docker-free coverage of the judge_local CLI + docker_runner argv (DESIGN.md §5.2)."""
import json

from worker import judge_local
from worker.docker_runner import ContainerResult, build_run_args

PAIR_SUM_OK = "def pair_sum(nums, target):\n    return [0, 1]\n"


def _payload(user_code):
    return {
        "function_name": "pair_sum",
        "user_code": user_code,
        "test_cases": [{"id": 0, "input": [[1, 6], 7], "expected": [0, 1]}],
        "comparison": {"mode": "exact"},
        "time_limit_ms": 2000,
    }


def _write(tmp_path, payload):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload))
    return str(path)


def test_cli_subprocess_accepts(tmp_path, capsys):
    rc = judge_local.main(["--subprocess", _write(tmp_path, _payload(PAIR_SUM_OK))])
    assert rc == 0
    assert "accepted" in capsys.readouterr().out


def test_cli_subprocess_reports_wrong_answer(tmp_path, capsys):
    payload = _payload("def pair_sum(nums, target):\n    return [9, 9]\n")
    rc = judge_local.main(["--subprocess", _write(tmp_path, payload)])
    assert rc == 0
    assert "wrong_answer" in capsys.readouterr().out


def test_build_run_args_has_isolation_flags():
    args = build_run_args(image="img", container_name="judge-x",
                          memory_mb=256, cpus="1", pids_limit=64)
    assert "--network=none" in args
    assert "--read-only" in args
    assert args[-1] == "img"


def test_tmpfs_is_noexec_unless_a_language_opts_in():
    """Only the Rust image runs a binary it wrote into /tmp (ADR-0004); every
    other image keeps Docker's noexec tmpfs."""
    default = build_run_args(image="img", container_name="judge-x",
                             memory_mb=256, cpus="1", pids_limit=64)
    assert default[default.index("--tmpfs") + 1] == "/tmp:size=16m"
    rust = build_run_args(image="img", container_name="judge-x", memory_mb=256, cpus="1",
                          pids_limit=64, tmpfs_size_mb=32, tmpfs_exec=True)
    assert rust[rust.index("--tmpfs") + 1] == "/tmp:size=32m,exec"


def test_container_result_oom_detection():
    result = ContainerResult(stdout="", stderr="", exit_code=137,
                             timed_out=False, stdout_truncated=False)
    assert result.oom_killed is True


def test_judge_local_runs_each_language_with_its_sandbox_profile(tmp_path, monkeypatch):
    """--language picks the profile's image, tmpfs size and `exec` flag. Without
    them the Rust harness can't run the binary it compiles (the bug this guards)."""
    captured = {}

    async def fake_run(payload_json, **kwargs):
        captured.update(kwargs)
        return ContainerResult(stdout='{"results": []}', stderr="", exit_code=0,
                               timed_out=False, stdout_truncated=False)

    monkeypatch.setattr(judge_local.docker_runner, "run_in_container", fake_run)
    judge_local.main(["--language", "rust", _write(tmp_path, _payload(PAIR_SUM_OK))])
    assert captured["image"] == "shikomi-judge-rust:latest"
    assert (captured["tmpfs_size_mb"], captured["tmpfs_exec"]) == (32, True)

    judge_local.main(["--image", "custom:tag", _write(tmp_path, _payload(PAIR_SUM_OK))])
    assert captured["image"] == "custom:tag"
    assert (captured["tmpfs_size_mb"], captured["tmpfs_exec"]) == (16, False)
