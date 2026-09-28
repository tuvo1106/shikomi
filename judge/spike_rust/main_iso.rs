// Spike: per-case process isolation. The binary re-execs itself once per case:
//   driver (no args): reads payload, spawns `self --case`, feeds ONLY the input
//     (never `expected`), enforces time_limit with a poll loop, classifies the
//     child's fate (exit/abort/SIGKILL-by-OOM/timeout), and compares here.
//   case (--case): decodes args, calls the user fn, writes the return value to
//     fd-independent file /tmp/ret; its stdout/stderr become the case's `stdout`.
// A panic, stack overflow, process::exit, OOM kill or infinite loop therefore
// costs one case, not the whole run — and user stdout can't corrupt the protocol.
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use std::os::unix::process::ExitStatusExt;
use prelude::*;
mod user { include!(concat!(env!("SPIKE_DIR"), "/user.rs")); }

fn case_mode() {
    let mut s = String::new(); std::io::stdin().read_to_string(&mut s).unwrap();
    let a = match Parser::new(&s).parse().unwrap() { Json::Arr(a) => a, _ => vec![] };
    let out = (|| -> Result<Json, String> {
        let nums = <Vec<i32>>::from_json(&a[0])?; let target = <i32>::from_json(&a[1])?;
        Ok(user::two_sum(nums, target).to_json())
    })();
    let mut o = String::new();
    match out { Ok(v) => v.write(&mut o), Err(e) => { o.push('!'); o.push_str(&e); } }
    std::fs::write("/tmp/ret", o).unwrap();
}

fn main() {
    if std::env::args().nth(1).as_deref() == Some("--case") { return case_mode(); }
    let mut s = String::new(); std::io::stdin().read_to_string(&mut s).unwrap();
    let payload = Parser::new(&s).parse().unwrap();
    let limit = Duration::from_millis(match payload.get("time_limit_ms") { Json::Int(n) => *n as u64, _ => 2000 });
    let me = std::env::current_exe().unwrap();
    let mut results = Vec::new();
    let cases = match payload.get("test_cases") { Json::Arr(c) => c.clone(), _ => vec![] };
    for c in &cases {
        let _ = std::fs::remove_file("/tmp/ret");
        let mut input = String::new(); c.get("input").write(&mut input);
        let t0 = Instant::now();
        let mut child = Command::new(&me).arg("--case").stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        child.stdin.take().unwrap().write_all(input.as_bytes()).unwrap();
        let status = loop {
            if let Some(st) = child.try_wait().unwrap() { break Some(st); }
            if t0.elapsed() > limit { let _ = child.kill(); let _ = child.wait(); break None; }
            std::thread::sleep(Duration::from_millis(1));
        };
        let ms = t0.elapsed().as_secs_f64() * 1000.0;
        let mut so = String::new(); if let Some(mut p) = child.stdout.take() { let _ = p.read_to_string(&mut so); }
        let mut se = String::new(); if let Some(mut p) = child.stderr.take() { let _ = p.read_to_string(&mut se); }
        let (status, output, error) = match status {
            None => ("time_limit_exceeded", None, None),
            Some(st) if st.signal() == Some(9) => ("memory_limit_exceeded", None, None),
            Some(st) if !st.success() => ("runtime_error", None, Some(se.lines().rev().take(3).collect::<Vec<_>>().join(" | "))),
            Some(_) => match std::fs::read_to_string("/tmp/ret") {
                Err(_) => ("runtime_error", None, Some("exited before returning".to_string())),
                Ok(r) if r.starts_with('!') => ("runtime_error", None, Some(r[1..].to_string())),
                Ok(r) => { let v = Parser::new(&r).parse().unwrap_or(Json::Null);
                           (if &v == c.get("expected") { "accepted" } else { "wrong_answer" }, Some(r), None) }
            },
        };
        let mut m = std::collections::BTreeMap::new();
        m.insert("test_case_id".to_string(), c.get("id").clone());
        m.insert("status".into(), Json::Str(status.into()));
        m.insert("runtime_ms".into(), Json::Num((ms * 100.0).round() / 100.0));
        m.insert("output".into(), output.map_or(Json::Null, Json::Str));
        m.insert("stdout".into(), if so.is_empty() { Json::Null } else { Json::Str(so) });
        m.insert("error".into(), error.map_or(Json::Null, Json::Str));
        results.push(Json::Obj(m));
    }
    let mut top = std::collections::BTreeMap::new(); top.insert("results".to_string(), Json::Arr(results));
    let mut o = String::new(); Json::Obj(top).write(&mut o); println!("{}", o);
}
