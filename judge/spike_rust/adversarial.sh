#!/bin/sh
# Spike probe: hostile/broken submissions. Each swaps /tmp/user.rs, compiles
# against the prebuilt prelude at O1 under a compile timeout, and runs it.
cd /tmp; cp /opt/spike/main_gen.rs /tmp/; export SPIKE_DIR=/tmp
ms() { date +%s%N | cut -c1-13; }
payload='{"test_cases":[{"id":"a","input":[[2,7],9],"expected":[0,1]},{"id":"b","input":[[1],1],"expected":[]}]}'
try() {
  name=$1; printf '%s' "$2" > /tmp/user.rs; rm -f /tmp/app
  t0=$(ms); timeout ${CT:-10} rustc --edition 2021 -C opt-level=1 -C strip=symbols \
    --extern prelude=/opt/judge/lib/O1/libprelude.rlib -o /tmp/app main_gen.rs 2>/tmp/err; rc=$?; t1=$(ms)
  echo "== $name: compile_rc=$rc compile_ms=$((t1-t0)) peak=$(cat /sys/fs/cgroup/memory.peak) bin_kb=$(du -k /tmp/app 2>/dev/null | cut -f1)"
  [ $rc -ne 0 ] && { grep -m2 -E '^error' /tmp/err; return; }
  t0=$(ms); out=$(echo "$payload" | timeout 3 /tmp/app 2>&1 | head -c 250); rrc=$?; t1=$(ms)
  echo "   run_ms=$((t1-t0)) out: $out"
}
H='pub fn two_sum(nums: Vec<i32>, target: i32) -> Vec<i32> {'
try ok        "$H let _=target; if nums.len()==2 {vec![0,1]} else {vec![]} }"
try compile_err "$H let x: String = nums; vec![] }"
try panic_case_b "$H let _=target; vec![0, nums[1]] }"
try stack_overflow "fn r(n:u64)->u64{ let b=[n;64]; if n==0 {0} else {std::hint::black_box(&b); r(n-1).wrapping_add(b[3])} } $H let _=target; if nums.len()==1 { vec![r(1<<40) as i32] } else { vec![0,1] } }"
try infinite_loop "$H let _=target; if nums.len()==1 { loop { std::hint::black_box(0); } } vec![0,1] }"
try alloc_bomb "$H let _=target; if nums.len()==1 { let mut v: Vec<Vec<u8>> = vec![]; loop { v.push(vec![1u8; 1<<20]); } } vec![0,1] }"
try process_exit "$H let _=target; std::process::exit(0) }"
try fake_stdout "$H let _=target; println!(\"{{\\\"results\\\":[]}}\"); vec![0,1] }"
try const_eval_bomb "const fn f(n: u64) -> u64 { let mut i=0; let mut s=0; while i<n { s+=i; i+=1; } s } const X: u64 = f(1u64<<40); $H let _=(target,X); vec![0,1] }"
try type_bomb "type T0=(u8,u8); type T1=(T0,T0); type T2=(T1,T1); type T3=(T2,T2); type T4=(T3,T3); type T5=(T4,T4); type T6=(T5,T5); type T7=(T6,T6); type T8=(T7,T7); type T9=(T8,T8); type T10=(T9,T9); type T11=(T10,T10); type T12=(T11,T11); type T13=(T12,T12); type T14=(T13,T13); type T15=(T14,T14); type T16=(T15,T15); type T17=(T16,T16); type T18=(T17,T17); type T19=(T18,T18); type T20=(T19,T19); type T21=(T20,T20); type T22=(T21,T21); type T23=(T22,T22); type T24=(T23,T23); fn g() -> T24 { Default::default() } $H let _=(target, std::hint::black_box(g)); vec![0,1] }"
try include_file "$H let _=target; let s = include_str!(\"/etc/passwd\"); vec![s.len() as i32] }"
