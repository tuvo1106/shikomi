//! `shikomi_prelude` — the JSON core shared by the Rust judge harness and every
//! program it compiles (DESIGN.md §13, ADR-0004).
//!
//! Built once, at image build time, into `libshikomi_prelude.rlib`
//! (judge/Dockerfile.rust). Two very different crates link against it:
//!
//! * **`harness.rs`** — the trusted driver. Parses the payload, compares each
//!   case's return value against `expected`, prints the result document.
//! * **the per-submission program** — the user's code plus a few lines of glue
//!   `harness.rs` generates. Its `main` is just `__run(|a| ...)`: decode the
//!   case's arguments, call the user's function, write the return value back.
//!
//! **No serde.** The sandbox has `--network=none`, so crates can't be fetched at
//! judge time. Vendoring serde into the image would work, but its proc-macros
//! cost compile time on every submission, and serde is a far larger trusted
//! surface than the handful of types a coding problem passes around. A small
//! hand-rolled value type does the job and keeps the per-submission compile at
//! ~150ms (ADR-0004's measurements).
//!
//! **Type-directed decoding without a type table.** The glue never names a
//! parameter's type. It writes `f(arg(a, 0)?, arg(a, 1)?)`, and rustc *infers*
//! each `arg::<T>` from the user's own signature. So `params[].type` in a
//! problem file is display-only for Rust, any `T: FromJson` works as a
//! parameter, and a type the prelude can't decode (say, `HashSet<i32>`) fails
//! at *compile* time. That surfaces the first time the problem's reference
//! solution is judged (`judge/tests/test_seed_solutions.py`), never as a
//! surprise at submit time. Parameters must be owned (`Vec<i32>`, `String`),
//! not borrowed (`&[i32]`, `&str`): a borrow would need a value that outlives
//! the call, and owned is the convention Rust problem sets already use.

use std::collections::{BTreeMap, HashMap};
use std::fmt::Write as _;

/// A parsed JSON value.
///
/// Integers and floats are kept apart (`Int` vs `Num`) so an `i64` survives the
/// round trip exactly. An `f64` would silently round integers above 2^53.
/// `PartialEq` is hand-written below, not derived, because JSON `1` and `1.0`
/// must compare equal (harness.py's `1 == 1.0` is `True`, and an author's
/// `expected` shouldn't care which spelling the submission's type produces).
#[derive(Debug, Clone)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(BTreeMap<String, Json>),
}

impl PartialEq for Json {
    fn eq(&self, other: &Json) -> bool {
        use Json::*;
        match (self, other) {
            (Null, Null) => true,
            (Bool(a), Bool(b)) => a == b,
            (Int(a), Int(b)) => a == b,
            (Num(a), Num(b)) => a == b || (a.is_nan() && b.is_nan()),
            // Cross-representation numbers compare by value; bool never equals a
            // number (unlike Python's True == 1). harness.js is strict too.
            (Int(a), Num(b)) | (Num(b), Int(a)) => int_equals_float(*a, *b),
            (Str(a), Str(b)) => a == b,
            (Arr(a), Arr(b)) => a == b,
            (Obj(a), Obj(b)) => a == b,
            _ => false,
        }
    }
}

/// Exact integer-vs-float equality. The obvious `a as f64 == b` rounds `a`
/// first, and above 2^53 neighbouring integers share an f64, so a wrong
/// 9007199254740993 would equal an expected 9007199254740992.0. Instead, a
/// float equals an integer only if it's a whole number inside i64's range that
/// converts to *exactly* that integer.
fn int_equals_float(a: i64, b: f64) -> bool {
    // 2^63 as f64 is exact; i64's range is [-2^63, 2^63).
    b.fract() == 0.0 && b >= -9_223_372_036_854_775_808.0 && b < 9_223_372_036_854_775_808.0 && b as i64 == a
}

impl Json {
    /// Parse a complete JSON document. Trailing non-whitespace is an error.
    pub fn parse(text: &str) -> Result<Json, String> {
        let mut p = Parser { b: text.as_bytes(), i: 0 };
        let v = p.value(0)?;
        p.ws();
        if p.i != p.b.len() {
            return Err(format!("trailing characters at byte {}", p.i));
        }
        Ok(v)
    }

    /// Compact JSON, with no whitespace. That matches harness.py's
    /// `separators=(",", ":")` and JSON.stringify, so the Output/Expected panes
    /// read the same whichever language judged.
    pub fn dump(&self) -> String {
        let mut out = String::new();
        self.write_to(&mut out);
        out
    }

    fn write_to(&self, o: &mut String) {
        match self {
            Json::Null => o.push_str("null"),
            Json::Bool(b) => o.push_str(if *b { "true" } else { "false" }),
            Json::Int(n) => {
                let _ = write!(o, "{}", n);
            }
            Json::Num(n) => write_float(*n, o),
            Json::Str(s) => write_str(s, o),
            Json::Arr(v) => {
                o.push('[');
                for (i, x) in v.iter().enumerate() {
                    if i > 0 {
                        o.push(',');
                    }
                    x.write_to(o);
                }
                o.push(']');
            }
            Json::Obj(m) => {
                o.push('{');
                for (i, (k, v)) in m.iter().enumerate() {
                    if i > 0 {
                        o.push(',');
                    }
                    write_str(k, o);
                    o.push(':');
                    v.write_to(o);
                }
                o.push('}');
            }
        }
    }

    /// Look up `key` in an object; `Null` for a missing key or a non-object,
    /// the same forgiving shape as `payload.get(...)` in harness.py.
    pub fn get(&self, key: &str) -> &Json {
        static NULL: Json = Json::Null;
        match self {
            Json::Obj(m) => m.get(key).unwrap_or(&NULL),
            _ => &NULL,
        }
    }

    pub fn as_arr(&self) -> &[Json] {
        match self {
            Json::Arr(v) => v,
            _ => &[],
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Int(n) => Some(*n as f64),
            Json::Num(n) => Some(*n),
            _ => None,
        }
    }
}

// Whole floats keep a ".0" so an f64 answer reads as a float (Python prints
// 2.0, not 2); NaN/inf aren't valid JSON, but this is a display string for
// `output`, and a readable token beats a parse failure.
fn write_float(n: f64, o: &mut String) {
    if n.is_nan() {
        o.push_str("NaN");
    } else if n.is_infinite() {
        o.push_str(if n > 0.0 { "Infinity" } else { "-Infinity" });
    } else if n == n.trunc() && n.abs() < 1e16 {
        let _ = write!(o, "{:.1}", n);
    } else {
        let _ = write!(o, "{}", n);
    }
}

fn write_str(s: &str, o: &mut String) {
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(o, "\\u{:04x}", c as u32);
            }
            c => o.push(c),
        }
    }
    o.push('"');
}

/// Nesting cap. Payloads come from our own worker, but return values are
/// built by user code, and the parser recurses; bounding depth turns a
/// pathological document into an error instead of a stack overflow.
const MAX_DEPTH: usize = 512;

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn expect_lit(&mut self, lit: &str, v: Json) -> Result<Json, String> {
        if self.b[self.i..].starts_with(lit.as_bytes()) {
            self.i += lit.len();
            Ok(v)
        } else {
            Err(format!("invalid literal at byte {}", self.i))
        }
    }

    fn value(&mut self, depth: usize) -> Result<Json, String> {
        if depth > MAX_DEPTH {
            return Err("JSON nested too deeply".into());
        }
        self.ws();
        match self.peek() {
            None => Err("unexpected end of JSON".into()),
            Some(b'n') => self.expect_lit("null", Json::Null),
            Some(b't') => self.expect_lit("true", Json::Bool(true)),
            Some(b'f') => self.expect_lit("false", Json::Bool(false)),
            Some(b'"') => self.string().map(Json::Str),
            Some(b'[') => {
                self.i += 1;
                let mut v = Vec::new();
                self.ws();
                if self.peek() == Some(b']') {
                    self.i += 1;
                    return Ok(Json::Arr(v));
                }
                loop {
                    v.push(self.value(depth + 1)?);
                    self.ws();
                    match self.peek() {
                        Some(b',') => self.i += 1,
                        Some(b']') => {
                            self.i += 1;
                            return Ok(Json::Arr(v));
                        }
                        _ => return Err(format!("expected ',' or ']' at byte {}", self.i)),
                    }
                }
            }
            Some(b'{') => {
                self.i += 1;
                let mut m = BTreeMap::new();
                self.ws();
                if self.peek() == Some(b'}') {
                    self.i += 1;
                    return Ok(Json::Obj(m));
                }
                loop {
                    self.ws();
                    if self.peek() != Some(b'"') {
                        return Err(format!("expected object key at byte {}", self.i));
                    }
                    let k = self.string()?;
                    self.ws();
                    if self.peek() != Some(b':') {
                        return Err(format!("expected ':' at byte {}", self.i));
                    }
                    self.i += 1;
                    let v = self.value(depth + 1)?;
                    m.insert(k, v);
                    self.ws();
                    match self.peek() {
                        Some(b',') => self.i += 1,
                        Some(b'}') => {
                            self.i += 1;
                            return Ok(Json::Obj(m));
                        }
                        _ => return Err(format!("expected ',' or '}}' at byte {}", self.i)),
                    }
                }
            }
            Some(_) => self.number(),
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.i;
        while self.i < self.b.len() && b"+-0123456789.eE".contains(&self.b[self.i]) {
            self.i += 1;
        }
        let t = std::str::from_utf8(&self.b[start..self.i]).map_err(|e| e.to_string())?;
        if t.is_empty() {
            return Err(format!("unexpected character at byte {}", start));
        }
        // Integer first so i64 values stay exact; an integer too big for i64
        // (or anything with '.'/'e') falls back to f64, like JSON.parse would.
        if let Ok(n) = t.parse::<i64>() {
            return Ok(Json::Int(n));
        }
        t.parse::<f64>().map(Json::Num).map_err(|_| format!("invalid number '{}'", t))
    }

    fn string(&mut self) -> Result<String, String> {
        self.i += 1; // opening quote
        let mut out = String::new();
        loop {
            let start = self.i;
            while self.i < self.b.len() && self.b[self.i] != b'"' && self.b[self.i] != b'\\' {
                self.i += 1;
            }
            out.push_str(std::str::from_utf8(&self.b[start..self.i]).map_err(|e| e.to_string())?);
            match self.peek() {
                None => return Err("unterminated string".into()),
                Some(b'"') => {
                    self.i += 1;
                    return Ok(out);
                }
                _ => {
                    // backslash escape
                    self.i += 1;
                    let e = self.peek().ok_or("unterminated escape")?;
                    self.i += 1;
                    match e {
                        b'n' => out.push('\n'),
                        b't' => out.push('\t'),
                        b'r' => out.push('\r'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'/' => out.push('/'),
                        b'\\' => out.push('\\'),
                        b'"' => out.push('"'),
                        b'u' => {
                            let hi = self.hex4()?;
                            // A surrogate pair (non-BMP char, e.g. an emoji) arrives as
                            // two \u escapes; decode both halves into one char.
                            let code = if (0xD800..0xDC00).contains(&hi)
                                && self.b[self.i..].starts_with(b"\\u")
                            {
                                self.i += 2;
                                let lo = self.hex4()?;
                                0x10000 + ((hi - 0xD800) << 10) + (lo.wrapping_sub(0xDC00) & 0x3FF)
                            } else {
                                hi
                            };
                            out.push(char::from_u32(code).unwrap_or('\u{FFFD}'));
                        }
                        other => return Err(format!("invalid escape '\\{}'", other as char)),
                    }
                }
            }
        }
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let h = self.b.get(self.i..self.i + 4).ok_or("truncated \\u escape")?;
        self.i += 4;
        u32::from_str_radix(std::str::from_utf8(h).map_err(|e| e.to_string())?, 16)
            .map_err(|_| "invalid \\u escape".to_string())
    }
}

// --- decoding arguments (JSON -> Rust) ------------------------------------

/// Decode a case argument into a parameter type. The error string names what
/// was expected, and it reaches the user as a `runtime_error`. In practice it
/// means the problem's test data doesn't match the signature, an authoring
/// bug the seed-solution tests catch.
pub trait FromJson: Sized {
    fn from_json(j: &Json) -> Result<Self, String>;
}

fn type_err<T>(want: &str, got: &Json) -> Result<T, String> {
    let mut shown = got.dump();
    if shown.len() > 60 {
        shown.truncate(60);
        shown.push('…');
    }
    Err(format!("expected {}, got {}", want, shown))
}

macro_rules! from_json_int {
    ($($t:ty),*) => {$(
        impl FromJson for $t {
            fn from_json(j: &Json) -> Result<Self, String> {
                match j {
                    // try_from, not `as`: an out-of-range value must fail loudly,
                    // not wrap into a different, wrong number.
                    Json::Int(n) => <$t>::try_from(*n)
                        .map_err(|_| format!("{} is out of range for {}", n, stringify!($t))),
                    other => type_err(stringify!($t), other),
                }
            }
        }
    )*};
}
from_json_int!(i8, i16, i32, i64, u8, u16, u32, u64, usize, isize);

impl FromJson for f64 {
    fn from_json(j: &Json) -> Result<Self, String> {
        j.as_f64().map_or_else(|| type_err("f64", j), Ok)
    }
}

impl FromJson for f32 {
    fn from_json(j: &Json) -> Result<Self, String> {
        j.as_f64().map(|x| x as f32).map_or_else(|| type_err("f32", j), Ok)
    }
}

impl FromJson for bool {
    fn from_json(j: &Json) -> Result<Self, String> {
        match j {
            Json::Bool(b) => Ok(*b),
            other => type_err("bool", other),
        }
    }
}

impl FromJson for String {
    fn from_json(j: &Json) -> Result<Self, String> {
        match j {
            Json::Str(s) => Ok(s.clone()),
            other => type_err("String", other),
        }
    }
}

/// A `char` travels as a one-character string (there's no JSON char type);
/// grid problems (`Vec<Vec<char>>`) are the usual reason to want one.
impl FromJson for char {
    fn from_json(j: &Json) -> Result<Self, String> {
        match j {
            Json::Str(s) if s.chars().count() == 1 => Ok(s.chars().next().unwrap()),
            other => type_err("a one-character string (char)", other),
        }
    }
}

impl<T: FromJson> FromJson for Vec<T> {
    fn from_json(j: &Json) -> Result<Self, String> {
        match j {
            Json::Arr(v) => v.iter().map(T::from_json).collect(),
            other => type_err("an array", other),
        }
    }
}

impl<T: FromJson> FromJson for Option<T> {
    fn from_json(j: &Json) -> Result<Self, String> {
        match j {
            Json::Null => Ok(None),
            other => T::from_json(other).map(Some),
        }
    }
}

impl<V: FromJson> FromJson for HashMap<String, V> {
    fn from_json(j: &Json) -> Result<Self, String> {
        match j {
            Json::Obj(m) => m.iter().map(|(k, v)| Ok((k.clone(), V::from_json(v)?))).collect(),
            other => type_err("an object", other),
        }
    }
}

impl<V: FromJson> FromJson for BTreeMap<String, V> {
    fn from_json(j: &Json) -> Result<Self, String> {
        match j {
            Json::Obj(m) => m.iter().map(|(k, v)| Ok((k.clone(), V::from_json(v)?))).collect(),
            other => type_err("an object", other),
        }
    }
}

macro_rules! tuple_json {
    ($n:expr; $($t:ident . $i:tt),+) => {
        /// A tuple travels as a fixed-length array (`[1, "a"]`), the only JSON shape
        /// for a heterogeneous group.
        impl<$($t: FromJson),+> FromJson for ($($t,)+) {
            fn from_json(j: &Json) -> Result<Self, String> {
                match j {
                    Json::Arr(v) if v.len() == $n => Ok(($($t::from_json(&v[$i])?,)+)),
                    other => type_err(concat!("an array of length ", $n), other),
                }
            }
        }
        impl<$($t: ToJson),+> ToJson for ($($t,)+) {
            fn to_json(&self) -> Json {
                Json::Arr(vec![$(self.$i.to_json()),+])
            }
        }
    };
}

// --- encoding return values (Rust -> JSON) --------------------------------

/// Encode a function's return value for comparison against `expected`.
pub trait ToJson {
    fn to_json(&self) -> Json;
}

macro_rules! to_json_int {
    ($($t:ty),*) => {$(
        impl ToJson for $t {
            fn to_json(&self) -> Json {
                // Unsigned values past i64::MAX can't be an exact Int; fall back to
                // a float rather than wrap to a negative number.
                i64::try_from(*self).map_or(Json::Num(*self as f64), Json::Int)
            }
        }
    )*};
}
to_json_int!(i8, i16, i32, i64, u8, u16, u32, u64, usize, isize);

tuple_json!(2; A.0, B.1);
tuple_json!(3; A.0, B.1, C.2);
tuple_json!(4; A.0, B.1, C.2, D.3);

impl ToJson for f64 {
    fn to_json(&self) -> Json {
        Json::Num(*self)
    }
}

impl ToJson for f32 {
    fn to_json(&self) -> Json {
        Json::Num(*self as f64)
    }
}

impl ToJson for bool {
    fn to_json(&self) -> Json {
        Json::Bool(*self)
    }
}

impl ToJson for String {
    fn to_json(&self) -> Json {
        Json::Str(self.clone())
    }
}

impl ToJson for str {
    fn to_json(&self) -> Json {
        Json::Str(self.to_string())
    }
}

impl ToJson for char {
    fn to_json(&self) -> Json {
        Json::Str(self.to_string())
    }
}

/// `()` (a function with no return value) encodes as `null`, like Python's `None`.
impl ToJson for () {
    fn to_json(&self) -> Json {
        Json::Null
    }
}

impl<T: ToJson + ?Sized> ToJson for &T {
    fn to_json(&self) -> Json {
        (**self).to_json()
    }
}

impl<T: ToJson> ToJson for Vec<T> {
    fn to_json(&self) -> Json {
        Json::Arr(self.iter().map(ToJson::to_json).collect())
    }
}

impl<T: ToJson> ToJson for [T] {
    fn to_json(&self) -> Json {
        Json::Arr(self.iter().map(ToJson::to_json).collect())
    }
}

impl<T: ToJson, const N: usize> ToJson for [T; N] {
    fn to_json(&self) -> Json {
        Json::Arr(self.iter().map(ToJson::to_json).collect())
    }
}

impl<T: ToJson> ToJson for Option<T> {
    fn to_json(&self) -> Json {
        self.as_ref().map_or(Json::Null, ToJson::to_json)
    }
}

impl<V: ToJson> ToJson for HashMap<String, V> {
    fn to_json(&self) -> Json {
        Json::Obj(self.iter().map(|(k, v)| (k.clone(), v.to_json())).collect())
    }
}

impl<V: ToJson> ToJson for BTreeMap<String, V> {
    fn to_json(&self) -> Json {
        Json::Obj(self.iter().map(|(k, v)| (k.clone(), v.to_json())).collect())
    }
}

// --- the per-case program's side of the protocol ---------------------------
//
// harness.rs runs the compiled submission once per test case (a fresh process
// each time, so a stack overflow, OOM or `process::exit` costs one case, not the
// run; ADR-0004). The case's argument array arrives on stdin. The outcome goes
// to the file named by SHIKOMI_RESULT, never stdout: the user's own prints go
// to stdout, and the harness reports those as the case's `stdout` field.
// Wire format of the result file, one JSON object:
//   {"ok": <return value>}  |  {"panic": "<message>"}  |  {"decode": "<message>"}

/// Environment variable naming the per-case result file.
pub const RESULT_ENV: &str = "SHIKOMI_RESULT";

fn write_result(key: &str, value: Json) {
    let mut m = BTreeMap::new();
    m.insert(key.to_string(), value);
    if let Ok(path) = std::env::var(RESULT_ENV) {
        // Nothing useful to do if this fails (e.g. tmpfs full); the harness
        // sees no result file and reports the case as a runtime_error.
        let _ = std::fs::write(path, Json::Obj(m).dump());
    }
}

/// Decode argument `i` of the case. Called by generated glue as
/// `arg(&a, i)?`, where `T` is inferred from the user's parameter type.
pub fn arg<T: FromJson>(args: &[Json], i: usize) -> Result<T, String> {
    let j = args.get(i).ok_or_else(|| format!("argument {} is missing from the test case", i + 1))?;
    T::from_json(j).map_err(|e| format!("argument {}: {}", i + 1, e))
}

/// Encode a return value. It's a function (not a bare `.to_json()` in the glue)
/// so the call site stays readable in a rustc error that points at the glue.
pub fn ret<T: ToJson>(v: T) -> Json {
    v.to_json()
}

/// Entry point of every generated program: read the argument array from
/// stdin, call `call` with it, write the outcome to the result file.
///
/// A panic is recorded by the panic hook (its message and the user's source
/// location), and the process then exits through the normal panic path. We
/// deliberately don't `catch_unwind`: the process is thrown away after one
/// case anyway, and the hook sees the panic *before* any user `Drop` impl gets
/// a chance to run code during unwinding.
pub fn __run<F: FnOnce(&[Json]) -> Result<Json, String>>(call: F) {
    use std::io::Read;
    std::panic::set_hook(Box::new(|info| {
        let msg = if let Some(s) = info.payload().downcast_ref::<&str>() {
            s.to_string()
        } else if let Some(s) = info.payload().downcast_ref::<String>() {
            s.clone()
        } else {
            "Box<dyn Any>".to_string()
        };
        let at = info.location().map_or(String::new(), |l| {
            format!(" (at {}:{}:{})", l.file().rsplit('/').next().unwrap_or(""), l.line(), l.column())
        });
        write_result("panic", Json::Str(format!("panicked: {}{}", msg, at)));
    }));

    let mut input = String::new();
    if let Err(e) = std::io::stdin().read_to_string(&mut input) {
        write_result("decode", Json::Str(format!("could not read test case input: {}", e)));
        return;
    }
    let args = match Json::parse(&input) {
        Ok(Json::Arr(a)) => a,
        Ok(_) => {
            write_result("decode", Json::Str("test case input is not an array".into()));
            return;
        }
        Err(e) => {
            write_result("decode", Json::Str(format!("test case input is not valid JSON: {}", e)));
            return;
        }
    };
    match call(&args) {
        Ok(v) => write_result("ok", v),
        Err(e) => write_result("decode", Json::Str(e)),
    }
}
