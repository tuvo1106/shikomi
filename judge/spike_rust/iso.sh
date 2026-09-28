#!/bin/sh
# Spike probe: every hostile case in ONE run through main_iso's per-case isolation.
cd /tmp; cp /opt/spike/main_iso.rs /tmp/; export SPIKE_DIR=/tmp
cat > /tmp/user.rs <<'U'
fn r(n:u64)->u64{ let b=[n;64]; if n==0 {0} else {std::hint::black_box(&b); r(n-1).wrapping_add(b[3])} }
pub fn two_sum(nums: Vec<i32>, target: i32) -> Vec<i32> {
    match target {
        1 => panic!("boom"),
        2 => vec![r(1<<40) as i32],
        3 => loop { std::hint::black_box(0); },
        4 => { let mut v: Vec<Vec<u8>> = vec![]; loop { v.push(vec![1u8; 1<<20]); } }
        5 => std::process::exit(0),
        6 => { println!("{{\"results\":[]}}"); vec![0, 1] }
        _ => { let _ = nums; vec![0, 1] }
    }
}
U
t0=$(date +%s%N | cut -c1-13)
rustc --edition 2021 -C opt-level=1 -C strip=symbols --extern prelude=/opt/judge/lib/O1/libprelude.rlib -o /tmp/app main_iso.rs || exit 1
t1=$(date +%s%N | cut -c1-13); echo "compile_ms=$((t1-t0))"
cases=""; for t in 9 1 2 3 4 5 6 9; do cases="$cases{\"id\":\"t$t\",\"input\":[[2,7],$t],\"expected\":[0,1]},"; done
# 200 trivial cases to price the per-case spawn overhead
many=""; i=0; while [ $i -lt 200 ]; do many="$many{\"id\":\"m$i\",\"input\":[[2,7],9],\"expected\":[0,1]},"; i=$((i+1)); done
echo "{\"time_limit_ms\":1000,\"test_cases\":[${cases%,}]}" | /tmp/app | tr '{' '\n'
t0=$(date +%s%N | cut -c1-13)
echo "{\"time_limit_ms\":1000,\"test_cases\":[${many%,}]}" | /tmp/app > /tmp/many.json
t1=$(date +%s%N | cut -c1-13); echo "200 isolated trivial cases: total_ms=$((t1-t0)) accepted=$(grep -o accepted /tmp/many.json | wc -l)"
