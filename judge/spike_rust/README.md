# Rust judge spike (throwaway)

Feasibility probes behind [ADR-0004](../../docs/adr/0004-rust-judge-compile-in-sandbox.md).
Not wired into the worker; delete this directory once `harness_rs` lands.

| File | What it is |
|---|---|
| `prelude.rs` | Dependency-free JSON value + `FromJson`/`ToJson`, pre-built to an rlib in the image |
| `user.rs` | A sample submission (two-sum) |
| `main_gen.rs` | What the harness would generate: **in-process** judging (the rejected design) |
| `main_iso.rs` | The proposed design: **one child process per case**, the driver compares |
| `measure.sh` | Compile time / peak memory / binary size per strategy |
| `adversarial.sh` | Hostile submissions against the in-process design |
| `iso.sh` | The same hostile set against per-case isolation, plus spawn-overhead timing |

Re-run (the same flags as `docker_runner.build_run_args`, but with tmpfs `exec`):

```sh
docker build -t shikomi-judge-rust-spike -f judge/spike_rust/Dockerfile.rust judge/spike_rust
R() { docker run --rm -i --network=none --memory=256m --memory-swap=256m --cpus=1 \
  --pids-limit=64 --read-only --tmpfs /tmp:size=16m,exec --security-opt=no-new-privileges \
  --cap-drop=ALL --user 1000:1000 "$@" shikomi-judge-rust-spike; }
R                                          # measure.sh
R --entrypoint /opt/spike/adversarial.sh
R --entrypoint /opt/spike/iso.sh
```

Drop `,exec` from the tmpfs to reproduce the `noexec` failure.
