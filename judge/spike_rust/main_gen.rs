// What harness_rs would *generate* from params [{nums: List[int]}, {target: int}]:
// decode args per case, call, time it, catch panics, emit the §5.3 result JSON.
use std::io::Read;
use std::time::Instant;
use prelude::*;
mod user { include!(concat!(env!("SPIKE_DIR"), "/user.rs")); }
fn main() {
    let mut s = String::new(); std::io::stdin().read_to_string(&mut s).unwrap();
    let payload = Parser::new(&s).parse().unwrap();
    std::panic::set_hook(Box::new(|_| {}));
    let mut results = Vec::new();
    if let Json::Arr(cases) = payload.get("test_cases") {
        for c in cases {
            let mut r = std::collections::BTreeMap::new();
            r.insert("test_case_id".to_string(), c.get("id").clone());
            let t0 = Instant::now();
            let out = std::panic::catch_unwind(|| {
                let a = match c.get("input") { Json::Arr(a) => a.clone(), _ => vec![] };
                let nums = <Vec<i32>>::from_json(&a[0])?; let target = <i32>::from_json(&a[1])?;
                Ok::<Json, String>(user::two_sum(nums, target).to_json())
            });
            r.insert("runtime_ms".into(), Json::Num(t0.elapsed().as_secs_f64() * 1000.0));
            match out {
                Ok(Ok(v)) => { let st = if &v == c.get("expected") { "accepted" } else { "wrong_answer" };
                               r.insert("status".into(), Json::Str(st.into())); let mut o = String::new(); v.write(&mut o); r.insert("output".into(), Json::Str(o)); }
                Ok(Err(e)) => { r.insert("status".into(), Json::Str("runtime_error".into())); r.insert("error".into(), Json::Str(e)); }
                Err(_) => { r.insert("status".into(), Json::Str("runtime_error".into())); r.insert("error".into(), Json::Str("panicked".into())); }
            }
            results.push(Json::Obj(r));
        }
    }
    let mut o = String::new(); let mut top = std::collections::BTreeMap::new(); top.insert("results".to_string(), Json::Arr(results));
    Json::Obj(top).write(&mut o); println!("{}", o);
}
