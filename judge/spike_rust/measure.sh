#!/bin/sh
# Spike probe, run INSIDE the locked-down judge container. Compiles the
# generated main + user code under each strategy and reports wall time, the
# container cgroup's peak memory, and whether the binary could run.
set -u
cd /tmp
cp /opt/spike/user.rs /opt/spike/main_gen.rs /tmp/
export SPIKE_DIR=/tmp
ms() { date +%s%N | cut -c1-13; }
peak() { cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo "?"; }
reset_peak() { echo 0 > /sys/fs/cgroup/memory.peak 2>/dev/null || true; }
payload='{"test_cases":[{"id":"a","input":[[2,7,11,15],9],"expected":[0,1]},{"id":"b","input":[[3,2,4],6],"expected":[1,2]},{"id":"c","input":[[3,3],6],"expected":[0,1]}]}'
for mode in ${MODES:-cold-O0 pre-O0 pre-O1 pre-O2}; do
  rm -f /tmp/app /tmp/libprelude.rlib
  opt=${mode#*-O}; t0=$(ms)
  case $mode in
    cold-*) rustc --edition 2021 --crate-type lib --crate-name prelude -C opt-level=$opt \
              -o /tmp/libprelude.rlib /opt/spike/prelude.rs 2>/tmp/err && \
            rustc --edition 2021 -C opt-level=$opt --extern prelude=/tmp/libprelude.rlib \
              ${EXTRA:-} -o /tmp/app main_gen.rs 2>>/tmp/err ;;
    pre-*)  rustc --edition 2021 -C opt-level=$opt --extern prelude=/opt/judge/lib/O$opt/libprelude.rlib \
              ${EXTRA:-} -o /tmp/app main_gen.rs 2>/tmp/err ;;
  esac
  rc=$?; t1=$(ms)
  size=$(du -k /tmp/app 2>/dev/null | cut -f1)
  out=$(echo "$payload" | /tmp/app 2>&1 | head -c 300); rrc=$?
  echo "$mode compile_rc=$rc compile_ms=$((t1-t0)) peak_bytes=$(peak) bin_kb=${size:-none}"
  [ $rc -ne 0 ] && head -c 600 /tmp/err
  echo "   run: $out"
done
