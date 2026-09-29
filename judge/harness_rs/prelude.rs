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
//!   An operations problem's glue replays a sequence of method calls instead
//!   (the `ops` module).
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
//!
//! The same inference covers linked lists and trees: a parameter typed
//! `Option<Box<ListNode>>` decodes from the same wire array harness.py's
//! `"ListNode"` codec reads (the `nodes` module). There, `params[].type` does
//! matter for Rust: declaring `"ListNode"` is what makes the glue define the
//! struct in the submission's crate (harness.rs `node_structs`), and the
//! workspace draws the sample from it too.

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

    /// A key that's equal for two values exactly when they're `==`, so an
    /// `unordered` comparison can sort keys instead of matching pairwise.
    /// It's `dump()` with one normalization: a whole float inside i64's range is
    /// written as that integer, matching `PartialEq`'s `1 == 1.0`. Everything
    /// else already dumps uniquely: object keys are sorted (`BTreeMap`), and
    /// Rust prints each distinct f64 as a distinct shortest string.
    pub fn canonical(&self) -> String {
        match self {
            Json::Num(n) if n.fract() == 0.0 && *n >= -9_223_372_036_854_775_808.0 && *n < 9_223_372_036_854_775_808.0 => {
                Json::Int(*n as i64).dump()
            }
            Json::Arr(v) => format!("[{}]", v.iter().map(Json::canonical).collect::<Vec<_>>().join(",")),
            Json::Obj(m) => {
                let body: Vec<String> =
                    m.iter().map(|(k, v)| format!("{}:{}", Json::Str(k.clone()).dump(), v.canonical())).collect();
                format!("{{{}}}", body.join(","))
            }
            other => other.dump(),
        }
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

    // The accessors below exist for custom validators (the `validator` module),
    // which read results and test inputs as `Json`: an operations result list mixes
    // `null`, bools and numbers, so it has no single Rust type to decode into.
    // Each returns `None` for any other variant rather than panicking, so a
    // validator can treat a submission's wrong-typed answer as a wrong answer.

    /// An integer, or a float that's exactly one (JSON doesn't tell `2` from `2.0`).
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(n) => Some(*n),
            Json::Num(n) if int_equals_float(*n as i64, *n) => Some(*n as i64),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Json::Null)
    }

    /// Decode into any `FromJson` type: `j.decode::<Vec<i32>>()`. For a validator
    /// that wants a typed view of a test input it knows the shape of.
    pub fn decode<T: FromJson>(&self) -> Result<T, String> {
        T::from_json(self)
    }
}

/// `j[i]`: element `i` of an array, or `Null` when `j` isn't one or `i` is out of
/// range, the same forgiving shape as `get`, so `args[1][0]` reads a nested test
/// input without a chain of `match`es and never panics on a malformed answer.
impl std::ops::Index<usize> for Json {
    type Output = Json;
    fn index(&self, i: usize) -> &Json {
        static NULL: Json = Json::Null;
        self.as_arr().get(i).unwrap_or(&NULL)
    }
}

/// `j["key"]`: `get` as an operator.
impl std::ops::Index<&str> for Json {
    type Output = Json;
    fn index(&self, key: &str) -> &Json {
        self.get(key)
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
            // Not JSON, but Python's json module reads and writes these (so an
            // author's `expected` may contain them), and `write_float` emits them.
            // Accepting them keeps a NaN/infinite answer a value, not a parse failure.
            Some(b'N') => self.expect_lit("NaN", Json::Num(f64::NAN)),
            Some(b'I') => self.expect_lit("Infinity", Json::Num(f64::INFINITY)),
            Some(b'-') if self.b[self.i..].starts_with(b"-Infinity") => {
                self.expect_lit("-Infinity", Json::Num(f64::NEG_INFINITY))
            }
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

    /// Decode an `Option<Self>`. The default is the obvious one (`null` is
    /// `None`), and the blanket `Option<T>` impl below delegates here, so a type
    /// can say what *its* absence looks like on the wire. The node types need
    /// that: an empty list or tree is `[]`, as in harness.py (see `nodes`). Rust
    /// has no specialization, so this hook is how `Option<Box<ListNode>>` gets
    /// different decoding from every other `Option<T>`.
    fn from_json_opt(j: &Json) -> Result<Option<Self>, String> {
        match j {
            Json::Null => Ok(None),
            other => Self::from_json(other).map(Some),
        }
    }
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
        T::from_json_opt(j)
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
            fn to_json(&self) -> Result<Json, String> {
                Ok(Json::Arr(vec![$(self.$i.to_json()?),+]))
            }
        }
    };
}

/// A decode-only iterator handed to an `operations` constructor (the Rust
/// counterpart of the Python harness's `Iterator`, DESIGN.md §5.4/§13). The
/// harness pre-builds it from a flat JSON `[i32, …]` — the wire shape of an
/// `"Iterator"`-typed constructor arg — and the submission's `new` consumes it.
/// It is a real `std::iter::Iterator<Item = i32>`, so a solution can call
/// `.next()`, `for x in it`, `.collect()`, `.peekable()`, etc. Decode-only:
/// there is no `ToJson`, because `"Iterator"` is never a return type. Int-only
/// (`i32`) like a node's `val`; a non-integer element fails the case's decode.
/// Deliberately not an `ExactSizeIterator` (and no `size_hint`): the Python
/// `Iterator` can't be asked its length, so a one-pass-stream problem means the
/// same thing in both languages.
pub struct IntIter(::std::vec::IntoIter<i32>);

impl FromJson for IntIter {
    fn from_json(j: &Json) -> Result<Self, String> {
        // Same wire as Vec<i32>: a JSON array of integers.
        let v = <::std::vec::Vec<i32> as FromJson>::from_json(j)?;
        Ok(IntIter(v.into_iter()))
    }
}

impl ::core::iter::Iterator for IntIter {
    type Item = i32;
    fn next(&mut self) -> ::core::option::Option<i32> {
        self.0.next()
    }
}

// --- encoding return values (Rust -> JSON) --------------------------------

/// Encode a function's return value for comparison against `expected`.
///
/// Fallible, though almost every impl can't fail: a node type can hold a value
/// its wire format can't express (a tree with a cycle), and that must fail the
/// case, not encode as a truncation that might happen to match. An `Err` is the
/// reason, reported to the user as the case's runtime error (`ret`). Carrying it
/// in the type means no caller can drop it by accident.
pub trait ToJson {
    fn to_json(&self) -> Result<Json, String>;

    /// Encode an `Option<Self>`: the encoding side of `FromJson::from_json_opt`,
    /// so a node type's `None` comes back as `[]` rather than `null`.
    fn to_json_opt(v: Option<&Self>) -> Result<Json, String>
    where
        Self: Sized,
    {
        v.map_or(Ok(Json::Null), ToJson::to_json)
    }
}

macro_rules! to_json_int {
    ($($t:ty),*) => {$(
        impl ToJson for $t {
            fn to_json(&self) -> Result<Json, String> {
                // Unsigned values past i64::MAX can't be an exact Int; fall back to
                // a float rather than wrap to a negative number.
                Ok(i64::try_from(*self).map_or(Json::Num(*self as f64), Json::Int))
            }
        }
    )*};
}
to_json_int!(i8, i16, i32, i64, u8, u16, u32, u64, usize, isize);

tuple_json!(2; A.0, B.1);
tuple_json!(3; A.0, B.1, C.2);
tuple_json!(4; A.0, B.1, C.2, D.3);

impl ToJson for f64 {
    fn to_json(&self) -> Result<Json, String> {
        Ok(Json::Num(*self))
    }
}

impl ToJson for f32 {
    fn to_json(&self) -> Result<Json, String> {
        Ok(Json::Num(*self as f64))
    }
}

impl ToJson for bool {
    fn to_json(&self) -> Result<Json, String> {
        Ok(Json::Bool(*self))
    }
}

impl ToJson for String {
    fn to_json(&self) -> Result<Json, String> {
        Ok(Json::Str(self.clone()))
    }
}

impl ToJson for str {
    fn to_json(&self) -> Result<Json, String> {
        Ok(Json::Str(self.to_string()))
    }
}

impl ToJson for char {
    fn to_json(&self) -> Result<Json, String> {
        Ok(Json::Str(self.to_string()))
    }
}

/// `()` (a function with no return value) encodes as `null`, like Python's `None`.
impl ToJson for () {
    fn to_json(&self) -> Result<Json, String> {
        Ok(Json::Null)
    }
}

impl<T: ToJson + ?Sized> ToJson for &T {
    fn to_json(&self) -> Result<Json, String> {
        (**self).to_json()
    }
}

impl<T: ToJson> ToJson for Vec<T> {
    fn to_json(&self) -> Result<Json, String> {
        self.iter().map(ToJson::to_json).collect::<Result<_, _>>().map(Json::Arr)
    }
}

impl<T: ToJson> ToJson for [T] {
    fn to_json(&self) -> Result<Json, String> {
        self.iter().map(ToJson::to_json).collect::<Result<_, _>>().map(Json::Arr)
    }
}

impl<T: ToJson, const N: usize> ToJson for [T; N] {
    fn to_json(&self) -> Result<Json, String> {
        self.iter().map(ToJson::to_json).collect::<Result<_, _>>().map(Json::Arr)
    }
}

impl<T: ToJson> ToJson for Option<T> {
    fn to_json(&self) -> Result<Json, String> {
        T::to_json_opt(self.as_ref())
    }
}

impl<V: ToJson> ToJson for HashMap<String, V> {
    fn to_json(&self) -> Result<Json, String> {
        self.iter().map(|(k, v)| Ok((k.clone(), v.to_json()?))).collect::<Result<_, String>>().map(Json::Obj)
    }
}

impl<V: ToJson> ToJson for BTreeMap<String, V> {
    fn to_json(&self) -> Result<Json, String> {
        self.iter().map(|(k, v)| Ok((k.clone(), v.to_json()?))).collect::<Result<_, String>>().map(Json::Obj)
    }
}

// --- linked-list and tree nodes ---------------------------------------------

/// The codecs for the node types a list or tree problem passes around, in the
/// conventional Rust shapes (the ones LeetCode's Rust problems use, so a
/// learner's muscle memory transfers): a list is `Option<Box<ListNode>>`, a
/// tree is `Option<Rc<RefCell<TreeNode>>>`.
///
/// **The structs themselves aren't here.** harness.rs's glue defines each one a
/// problem declares *in the submission's own crate*, from `harness.rs`
/// `LIST_NODE`/`TREE_NODE`, and the starter code only describes it in a comment.
/// That's what lets a solution treat the struct as its own: Rust's orphan rule
/// forbids `impl Ord for ListNode` (the usual way to put nodes in a
/// `BinaryHeap`) or a helper `impl ListNode { … }` for a type from another
/// crate, and a struct defined in this prelude would be exactly that. The
/// prelude can't name a type defined later in the user's crate, so the codecs
/// are written against small shape traits (`ListShape`, `TreeShape`), and the
/// glue implements them for the generated structs with one-line accessors.
///
/// Wire format, identical to harness.py's `ListNode`/`TreeNode` codecs
/// (DESIGN.md §5.3):
/// * a list is a flat array of values, head first: `[1, 2, 3]`;
/// * a tree is a null-padded level-order array, trailing nulls trimmed:
///   `[1, null, 2, 3]`. A `null` holds no slots for its own (absent) children;
/// * an empty list or tree is `[]` both ways; `null` is also read as empty.
///
/// The `List[ListNode]`/`List[TreeNode]` forms need no code of their own:
/// `Vec<Option<Box<ListNode>>>` goes through `Vec<T>`'s impl, one element at a time.
///
/// Three more types carry the shapes a `Box` can't: `CyclicListNode`,
/// `RandomListNode` and `GraphNode`, each shared through `Rc<RefCell<…>>` (their
/// section below says why, and why every link is strong).
///
/// Every walk here is iterative. A 10^5-node list or a degenerate
/// (linked-list-shaped) tree is a normal stress case, and recursion that deep
/// would spend the stack the user's own solution needs.
pub mod nodes {
    use super::{FromJson, Json, ToJson};
    use std::cell::RefCell;
    use std::collections::{HashMap, VecDeque};
    use std::rc::Rc;

    /// The most entries an encoded tree may have. A tree may share subtrees
    /// (below), and sharing can make the encoding exponentially larger than
    /// the node count, so a runaway answer fails with a message instead of
    /// exhausting the case's memory. Real answers are far smaller.
    pub const MAX_ENCODED: usize = 2_000_000;

    /// What the codecs need from a singly linked list node. harness.rs's glue
    /// implements it for the generated `ListNode`.
    pub trait ListShape: Sized {
        fn make(val: i32) -> Self;
        fn value(&self) -> i32;
        fn next_node(&self) -> Option<&Self>;
        fn set_next_node(&mut self, next: Option<Box<Self>>);
    }

    fn values(j: &Json, what: &str) -> Result<Vec<Option<i32>>, String> {
        match j {
            Json::Null => Ok(Vec::new()),
            Json::Arr(v) => v.iter().map(Option::<i32>::from_json).collect(),
            other => super::type_err(what, other),
        }
    }

    impl<T: ListShape> FromJson for Box<T> {
        /// Only reached for a bare `Box<ListNode>` parameter, which can't be
        /// empty; the usual `Option<Box<ListNode>>` goes through `from_json_opt`.
        fn from_json(j: &Json) -> Result<Self, String> {
            Self::from_json_opt(j)?.ok_or_else(|| "expected a non-empty list".to_string())
        }

        fn from_json_opt(j: &Json) -> Result<Option<Self>, String> {
            let vals = values(j, "a list as an array of values")?;
            // Built back to front, so each node is boxed once with its tail in hand.
            let mut head: Option<Box<T>> = None;
            for v in vals.into_iter().rev() {
                let mut node = Box::new(T::make(v.ok_or("a list's values can't be null")?));
                node.set_next_node(head);
                head = Some(node);
            }
            Ok(head)
        }
    }

    impl<T: ListShape> ToJson for Box<T> {
        fn to_json(&self) -> Result<Json, String> {
            // A `Box` list can't be cyclic in safe code, so no visited set is needed.
            let mut out = Vec::new();
            let mut cur: Option<&T> = Some(self);
            while let Some(node) = cur {
                out.push(Json::Int(node.value() as i64));
                cur = node.next_node();
            }
            Ok(Json::Arr(out))
        }

        fn to_json_opt(v: Option<&Self>) -> Result<Json, String> {
            v.map_or(Ok(Json::Arr(Vec::new())), ToJson::to_json)
        }
    }

    /// What the codecs need from a binary tree node. harness.rs's glue
    /// implements it for the generated `TreeNode`.
    pub trait TreeShape: Sized {
        fn make(val: i32) -> Self;
        fn value(&self) -> i32;
        fn left_node(&self) -> Option<Rc<RefCell<Self>>>;
        fn right_node(&self) -> Option<Rc<RefCell<Self>>>;
        fn set_left_node(&mut self, child: Option<Rc<RefCell<Self>>>);
        fn set_right_node(&mut self, child: Option<Rc<RefCell<Self>>>);
    }

    thread_local! {
        /// Every `Rc` node a decoder built, kept alive until the process exits.
        ///
        /// Dropping a long `Rc` chain recurses once per node (`Rc` -> `RefCell` ->
        /// the next `Rc`), so a 10^6-node input freed at the end of the user's
        /// function could overflow the stack after their code already finished.
        /// Holding one extra reference to each node means no drop ever cascades:
        /// the user's handles drop one refcount at a time, and the process (one per
        /// case) exits before the registry itself would be freed. The return value
        /// is leaked for the same reason (`super::ret`).
        static KEEP: RefCell<Vec<Rc<dyn std::any::Any>>> = const { RefCell::new(Vec::new()) };
    }

    fn keep_alive<T: 'static>(nodes: &[Rc<RefCell<T>>]) {
        KEEP.with(|k| k.borrow_mut().extend(nodes.iter().map(|n| Rc::clone(n) as Rc<dyn std::any::Any>)));
    }

    /// A node type shared through `Rc<RefCell<…>>`. Rust allows only one blanket
    /// `FromJson`/`ToJson` impl for `Rc<RefCell<T>>`, so every such shape routes
    /// through this trait, and the glue's impl for each generated struct names
    /// its codec (`decode_tree`/`encode_tree` for `TreeNode`).
    pub trait RcNode: Sized {
        fn decode(j: &Json) -> Result<Option<Rc<RefCell<Self>>>, String>;
        fn encode(node: &Rc<RefCell<Self>>) -> Result<Json, String>;
        /// How this type writes `None`.
        fn encode_none() -> Json;
    }

    impl<T: RcNode> FromJson for Rc<RefCell<T>> {
        fn from_json(j: &Json) -> Result<Self, String> {
            T::decode(j)?.ok_or_else(|| "expected a non-empty value".to_string())
        }

        fn from_json_opt(j: &Json) -> Result<Option<Self>, String> {
            T::decode(j)
        }
    }

    impl<T: RcNode> ToJson for Rc<RefCell<T>> {
        fn to_json(&self) -> Result<Json, String> {
            T::encode(self)
        }

        fn to_json_opt(v: Option<&Self>) -> Result<Json, String> {
            v.map_or_else(|| Ok(T::encode_none()), T::encode)
        }
    }

    /// Null-padded level order -> tree: the same fill as harness.py's
    /// `_build_tree`. Each value after the root takes the next open child slot
    /// in BFS order, and a `null` leaves its slot empty.
    pub fn decode_tree<T: TreeShape + 'static>(j: &Json) -> Result<Option<Rc<RefCell<T>>>, String> {
        let vals = values(j, "a tree as a level-order array")?;
        let mut it = vals.into_iter();
        let root = match it.next() {
            Some(Some(v)) => Rc::new(RefCell::new(T::make(v))),
            _ => return Ok(None),
        };
        let mut built = vec![Rc::clone(&root)];
        let mut queue = VecDeque::from([Rc::clone(&root)]);
        'fill: while let Some(node) = queue.pop_front() {
            for left in [true, false] {
                let Some(slot) = it.next() else { break 'fill };
                if let Some(v) = slot {
                    let child = Rc::new(RefCell::new(T::make(v)));
                    queue.push_back(Rc::clone(&child));
                    built.push(Rc::clone(&child));
                    let mut n = node.borrow_mut();
                    if left { n.set_left_node(Some(child)) } else { n.set_right_node(Some(child)) }
                }
            }
        }
        keep_alive(&built);
        Ok(Some(root))
    }

    /// Tree -> null-padded level order, trailing nulls trimmed (harness.py's
    /// `_flatten_tree`).
    ///
    /// An `Rc` lets a buggy solution point a child back at an ancestor, and a
    /// cycle has no finite encoding, so it's refused rather than encoded up to
    /// the repeated node (a truncation can match the expected answer exactly).
    /// A subtree *shared* by two parents is fine, though: it's a finite tree
    /// that encodes cleanly, and memoized solutions build them on purpose
    /// ("all full binary trees of size n" reuses equal-sized subtrees). So the
    /// check is for a node that is its own ancestor: a three-colour depth-first
    /// search, where reaching a node still on the current path is a cycle. Then
    /// the breadth-first encoding needs no visited set, just `MAX_ENCODED`.
    pub fn encode_tree<T: TreeShape>(root: &Rc<RefCell<T>>) -> Result<Json, String> {
        // false = on the current path (grey), true = finished (black).
        let mut state: HashMap<*const RefCell<T>, bool> = HashMap::new();
        let mut stack = vec![(Rc::clone(root), false)];
        while let Some((node, leaving)) = stack.pop() {
            let key = Rc::as_ptr(&node);
            if leaving {
                state.insert(key, true);
                continue;
            }
            match state.get(&key) {
                Some(true) => continue,
                Some(false) => {
                    return Err("the returned tree has a cycle: a child points back to one of its \
                                ancestors"
                        .into())
                }
                None => {}
            }
            state.insert(key, false);
            let (l, r) = { let n = node.borrow(); (n.left_node(), n.right_node()) };
            stack.push((node, true));
            stack.extend(r.into_iter().chain(l).map(|c| (c, false)));
        }
        let mut out = Vec::new();
        let mut queue = VecDeque::from([Some(Rc::clone(root))]);
        while let Some(slot) = queue.pop_front() {
            if out.len() >= MAX_ENCODED {
                return Err(format!("the returned tree is too large to encode (over {} entries)", MAX_ENCODED));
            }
            let Some(node) = slot else {
                out.push(Json::Null);
                continue;
            };
            let n = node.borrow();
            out.push(Json::Int(n.value() as i64));
            queue.push_back(n.left_node());
            queue.push_back(n.right_node());
        }
        while out.last() == Some(&Json::Null) {
            out.pop();
        }
        Ok(Json::Arr(out))
    }

    // --- the shared-node types: cyclic list, random-pointer list, graph ------
    //
    // These three can't be `Box`ed: a node is reachable along more than one path
    // (a cycle, a random pointer, an undirected edge), so every link is an
    // `Rc<RefCell<…>>`. Every link is also *strong*, cycles included, which leaks
    // any cycle a case builds. That's deliberate: each case runs in its own
    // process, which exits right after, so nothing accumulates. `Weak` back
    // edges would put an `upgrade()` into every traversal a learner writes, for
    // memory the OS reclaims anyway. Like the list and tree, the structs are
    // generated into the submission's crate (harness.rs), so these codecs are
    // written against shape traits.

    /// What the codec needs from a list node whose `next` may point back at an
    /// earlier node (the input to a cycle-detection problem).
    pub trait CyclicShape: Sized + 'static {
        fn make(val: i32) -> Self;
        fn set_next_node(&mut self, next: Option<Rc<RefCell<Self>>>);
    }

    thread_local! {
        /// Every cyclic-list node the decoder built, with its index *within its
        /// own list*, so a returned node can be answered by identity, as
        /// harness.py's per-list `_idx` stamp does: values may repeat, so "the node
        /// with value 1" is ambiguous. Holding the `Rc`s also keeps each node
        /// alive, so no node the user allocates later can reuse a built node's
        /// address and pass for it. `dyn Any`, because a thread-local can't be
        /// generic over the node type.
        static CYCLIC_BUILT: RefCell<Vec<(Rc<dyn std::any::Any>, usize)>> = const { RefCell::new(Vec::new()) };
    }

    /// `[values, pos]` -> a list whose last node's `next` points back to index
    /// `pos` (`-1` for no cycle). `[]` or `null` is an empty list.
    pub fn decode_cyclic<T: CyclicShape>(j: &Json) -> Result<Option<Rc<RefCell<T>>>, String> {
        let (vals, pos) = match j {
            Json::Null => return Ok(None),
            Json::Arr(a) if a.is_empty() => return Ok(None),
            Json::Arr(a) if a.len() == 2 => (Vec::<i32>::from_json(&a[0])?, i64::from_json(&a[1])?),
            other => return super::type_err("a cyclic list as [values, pos]", other),
        };
        let nodes: Vec<Rc<RefCell<T>>> = vals.into_iter().map(|v| Rc::new(RefCell::new(T::make(v)))).collect();
        for w in nodes.windows(2) {
            w[0].borrow_mut().set_next_node(Some(Rc::clone(&w[1])));
        }
        if pos != -1 {
            let target = usize::try_from(pos).ok().and_then(|p| nodes.get(p));
            let (Some(target), Some(last)) = (target, nodes.last()) else {
                return Err(format!("cycle position {} is outside the list", pos));
            };
            last.borrow_mut().set_next_node(Some(Rc::clone(target)));
        }
        let head = nodes.first().cloned();
        CYCLIC_BUILT.with(|b| {
            b.borrow_mut().extend(nodes.into_iter().enumerate().map(|(i, n)| (n as Rc<dyn std::any::Any>, i)))
        });
        Ok(head)
    }

    /// A returned node -> its index in the input, or `null` for a node the judge
    /// didn't build (a fresh node with the right value is a wrong answer, as in
    /// harness.py's `_encode_cyclic_node`).
    pub fn encode_cyclic<T: CyclicShape>(node: &Rc<RefCell<T>>) -> Result<Json, String> {
        let want = Rc::as_ptr(node) as *const ();
        Ok(CYCLIC_BUILT.with(|b| {
            b.borrow()
                .iter()
                .find(|(n, _)| Rc::as_ptr(n) as *const () == want)
                .map_or(Json::Null, |&(_, i)| Json::Int(i as i64))
        }))
    }

    /// What the codec needs from a list node with a second pointer, `random`, to
    /// any node of the same list (or none): the input to a deep-copy problem.
    pub trait RandomShape: Sized {
        fn make(val: i32) -> Self;
        fn value(&self) -> i32;
        fn next_node(&self) -> Option<Rc<RefCell<Self>>>;
        fn random_node(&self) -> Option<Rc<RefCell<Self>>>;
        fn set_next_node(&mut self, next: Option<Rc<RefCell<Self>>>);
        fn set_random_node(&mut self, random: Option<Rc<RefCell<Self>>>);
    }

    /// `[[val, random_index], ...]`, `random_index` indexing this same array (or
    /// `null`). Two passes, as in harness.py: chain `next`, then resolve
    /// `random`, whose target may come later in the list.
    pub fn decode_random<T: RandomShape + 'static>(j: &Json) -> Result<Option<Rc<RefCell<T>>>, String> {
        let pairs = Option::<Vec<(i32, Option<usize>)>>::from_json(j)?.unwrap_or_default();
        let nodes: Vec<Rc<RefCell<T>>> = pairs.iter().map(|&(v, _)| Rc::new(RefCell::new(T::make(v)))).collect();
        for w in nodes.windows(2) {
            w[0].borrow_mut().set_next_node(Some(Rc::clone(&w[1])));
        }
        for (node, &(_, r)) in nodes.iter().zip(&pairs) {
            if let Some(r) = r {
                let target = nodes.get(r).ok_or_else(|| format!("random index {} is outside the list", r))?;
                node.borrow_mut().set_random_node(Some(Rc::clone(target)));
            }
        }
        keep_alive(&nodes);
        Ok(nodes.into_iter().next())
    }

    /// Back to `[[val, random_index], ...]`, with each index re-derived from the
    /// returned list's own `next` order (a deep copy shares no node with the
    /// input, so nothing stamped at decode time survives). A `random` that lands
    /// off the returned list encodes as `null`. A `next` chain that loops is
    /// refused, like a looping `ListNode` in harness.py.
    pub fn encode_random<T: RandomShape>(head: &Rc<RefCell<T>>) -> Result<Json, String> {
        let mut order: Vec<Rc<RefCell<T>>> = Vec::new();
        let mut index = HashMap::new();
        let mut cur = Some(Rc::clone(head));
        while let Some(node) = cur {
            if index.insert(Rc::as_ptr(&node), order.len()).is_some() {
                return Err(format!(
                    "the returned list has a cycle: after {} node(s), a `next` points back to an \
                     earlier node (did you forget to end the list with None?)",
                    order.len()
                ));
            }
            cur = node.borrow().next_node();
            order.push(node);
        }
        Ok(Json::Arr(
            order
                .iter()
                .map(|n| {
                    let n = n.borrow();
                    let r = n.random_node().and_then(|r| index.get(&Rc::as_ptr(&r)).copied());
                    Json::Arr(vec![Json::Int(n.value() as i64), r.map_or(Json::Null, |i| Json::Int(i as i64))])
                })
                .collect(),
        ))
    }

    /// What the codec needs from a graph node: a value (1..=n, as the wire
    /// format numbers nodes) and its neighbours.
    pub trait GraphShape: Sized {
        fn make(val: i32) -> Self;
        fn value(&self) -> i32;
        fn neighbor_nodes(&self) -> &[Rc<RefCell<Self>>];
        fn push_neighbor(&mut self, node: Rc<RefCell<Self>>);
    }

    /// An adjacency list keyed by value: row `i` lists the neighbour values of
    /// the node valued `i + 1`. `[]` is the empty graph, `[[]]` a single node.
    /// The argument is the node valued 1, as in harness.py.
    pub fn decode_graph<T: GraphShape + 'static>(j: &Json) -> Result<Option<Rc<RefCell<T>>>, String> {
        let adj = Option::<Vec<Vec<usize>>>::from_json(j)?.unwrap_or_default();
        let nodes: Vec<Rc<RefCell<T>>> =
            (1..=adj.len()).map(|v| Rc::new(RefCell::new(T::make(v as i32)))).collect();
        for (node, row) in nodes.iter().zip(&adj) {
            for &v in row {
                let nb = v.checked_sub(1).and_then(|i| nodes.get(i));
                let nb = nb.ok_or_else(|| format!("neighbour {} is not a node of the graph", v))?;
                node.borrow_mut().push_neighbor(Rc::clone(nb));
            }
        }
        keep_alive(&nodes);
        Ok(nodes.into_iter().next())
    }

    /// Breadth-first from the returned node, by identity, then one row per value
    /// up to the largest seen (harness.py's `_encode_graph`). A graph is cyclic
    /// when it's *right* (every undirected edge is a 2-cycle), so the visited set
    /// is what makes this terminate at all.
    pub fn encode_graph<T: GraphShape>(start: &Rc<RefCell<T>>) -> Result<Json, String> {
        let mut seen = std::collections::HashSet::from([Rc::as_ptr(start)]);
        let mut visited = Vec::new();
        let mut queue = VecDeque::from([Rc::clone(start)]);
        while let Some(node) = queue.pop_front() {
            for nb in node.borrow().neighbor_nodes() {
                if seen.insert(Rc::as_ptr(nb)) {
                    queue.push_back(Rc::clone(nb));
                }
            }
            visited.push(node);
        }
        // Rows are keyed by value, so the largest value sizes the output. A buggy
        // clone can set any value, and `i32::MAX` rows would exhaust memory before
        // any comparison; the cap fails it readably instead (harness.py matches).
        let max = visited.iter().map(|n| n.borrow().value()).max().unwrap_or(0).max(0) as usize;
        if max > MAX_ENCODED {
            return Err(format!("the returned graph has a node valued {}, too large to encode", max));
        }
        let mut rows = vec![Json::Arr(Vec::new()); max];
        for n in &visited {
            let n = n.borrow();
            if n.value() >= 1 {
                rows[n.value() as usize - 1] =
                    Json::Arr(n.neighbor_nodes().iter().map(|nb| Json::Int(nb.borrow().value() as i64)).collect());
            }
        }
        Ok(Json::Arr(rows))
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
//   |  {"malformed": "<message>"}

/// Why a case produced no value to compare. `Decode` is the test data not
/// fitting the signature (an authoring bug); `Malformed` is the submission
/// returning something its type allows but the problem doesn't, like a tree
/// with a cycle (a `ToJson` error). `From<String>` lets the glue's `arg(..)?`
/// produce a `Decode`.
pub enum Fail {
    Decode(String),
    Malformed(String),
}

impl From<String> for Fail {
    fn from(e: String) -> Fail {
        Fail::Decode(e)
    }
}

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
/// An encoding error means the answer can't be expressed on the wire (a tree
/// with a cycle), which fails the case as malformed.
pub fn ret<T: ToJson>(v: T) -> Result<Json, Fail> {
    let j = v.to_json().map_err(Fail::Malformed);
    // Never dropped: freeing a long `Rc` or `Box` chain recurses once per node, and
    // the process exits right after this case anyway (see `nodes::KEEP`).
    std::mem::forget(v);
    j
}

/// Entry point of every generated program: read the argument array from
/// stdin, call `call` with it, write the outcome to the result file.
///
/// A panic is recorded by the panic hook (its message and the user's source
/// location), and the process then exits through the normal panic path. We
/// deliberately don't `catch_unwind`: the process is thrown away after one
/// case anyway, and the hook sees the panic *before* any user `Drop` impl gets
/// a chance to run code during unwinding.
pub fn __run<F: FnOnce(&[Json]) -> Result<Json, Fail>>(call: F) {
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
        Err(Fail::Decode(e)) => write_result("decode", Json::Str(e)),
        Err(Fail::Malformed(e)) => write_result("malformed", Json::Str(e)),
    }
}

/// What the generated C `main` calls (harness.rs `glue`): `__run` plus the two
/// jobs std's runtime would normally do around a Rust `fn main`, which the
/// judge's `#![no_main]` entry point skips. A panic is caught here, because it
/// can't unwind out of an `extern "C"` function (that aborts); the panic hook
/// has already recorded it by then. And stdout is flushed, since std only
/// flushes its line buffer at a normal Rust exit, and a final `print!` with no
/// newline would otherwise be lost.
pub fn __entry<F: FnOnce(&[Json]) -> Result<Json, Fail>>(call: F) -> i32 {
    use std::io::Write;
    let code = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| __run(call))) {
        Ok(()) => 0,
        Err(_) => 101, // the same code a panicking Rust `main` exits with
    };
    let _ = std::io::stdout().flush();
    code
}

// --- operations mode -------------------------------------------------------------

/// Operations mode (DESIGN.md §5.3): replay a sequence of calls on one object.
///
/// A case's input is `[ops, args]`. `ops[0]` names the class and `args[0]` holds
/// the constructor's arguments. Each later `ops[i]` is a method called with
/// `args[i]`. The result is the per-call list, with `null` in the constructor's
/// slot, the same wire shape harness.py's `_run_operations` produces.
///
/// **No method table.** Function mode never names a parameter type, because
/// rustc infers each `arg::<T>` from the user's signature. This module applies
/// the same trick to a whole method. The glue writes
/// `call(obj, super::Editor::append, "append", args)`, and the `Method` impl that
/// matches `append`'s signature fixes the arity, every argument's type and the
/// receiver. So a problem file doesn't have to declare method signatures, and a
/// method with a parameter the prelude can't decode fails at *compile* time,
/// like a bad function-mode signature.
///
/// The one-trait-many-impls pattern needs a marker type parameter
/// (`Method<S, (ByMut, A, B)>`): without it, the impls for `Fn(&mut S, A)` and
/// `Fn(&S, A)` would overlap as far as coherence can tell, although no function
/// implements both.
pub mod ops {
    use super::{arg, ret, Fail, FromJson, Json, ToJson};

    /// Marks a `&mut self` method in a `Method` impl's marker tuple.
    pub struct ByMut;
    /// Marks a `&self` method in a `Method` impl's marker tuple.
    pub struct ByRef;

    /// A constructor (`fn new(...) -> Self`) of up to six parameters, each decoded
    /// from the case's first argument list.
    pub trait Constructor<S, Args> {
        fn construct(self, args: &[Json]) -> Result<S, Fail>;
    }

    /// A method of up to six parameters, taking `&mut self` or `&self`, whose
    /// return value is encoded as that call's result. A method returning a
    /// reference doesn't match (its return type would borrow from `self`), so
    /// methods return owned values, as in the rest of the harness.
    pub trait Method<S, Args> {
        fn invoke(self, obj: &mut S, args: &[Json]) -> Result<Json, Fail>;
    }

    /// Refuses an argument list of the wrong length. The glue can't know an arity
    /// up front (that's the user's signature), so a mismatch is found here.
    /// Python raises a `TypeError` for the same mistake.
    fn expect_len(args: &[Json], n: usize) -> Result<(), Fail> {
        if args.len() == n {
            Ok(())
        } else {
            let s = if n == 1 { "" } else { "s" };
            Err(Fail::Decode(format!("takes {} argument{} but the test case passes {}", n, s, args.len())))
        }
    }

    macro_rules! arity {
        ($n:literal $(, $a:ident $i:literal)*) => {
            impl<S, F, $($a: FromJson),*> Constructor<S, ($($a,)*)> for F
            where
                F: FnOnce($($a),*) -> S,
            {
                fn construct(self, _args: &[Json]) -> Result<S, Fail> {
                    expect_len(_args, $n)?;
                    Ok(self($(arg::<$a>(_args, $i)?),*))
                }
            }
            impl<S, R: ToJson, F, $($a: FromJson),*> Method<S, (ByMut, $($a,)*)> for F
            where
                F: FnOnce(&mut S $(, $a)*) -> R,
            {
                fn invoke(self, obj: &mut S, _args: &[Json]) -> Result<Json, Fail> {
                    expect_len(_args, $n)?;
                    ret(self(obj $(, arg::<$a>(_args, $i)?)*))
                }
            }
            impl<S, R: ToJson, F, $($a: FromJson),*> Method<S, (ByRef, $($a,)*)> for F
            where
                F: FnOnce(&S $(, $a)*) -> R,
            {
                fn invoke(self, obj: &mut S, _args: &[Json]) -> Result<Json, Fail> {
                    expect_len(_args, $n)?;
                    ret(self(&*obj $(, arg::<$a>(_args, $i)?)*))
                }
            }
        };
    }
    arity!(0);
    arity!(1, A 0);
    arity!(2, A 0, B 1);
    arity!(3, A 0, B 1, C 2);
    arity!(4, A 0, B 1, C 2, D 3);
    arity!(5, A 0, B 1, C 2, D 3, E 4);
    arity!(6, A 0, B 1, C 2, D 3, E 4, G 5);

    /// Prefixes a failure with the call it came from, so "argument 1: expected an
    /// integer" says *which* call's argument.
    fn at(what: &str, e: Fail) -> Fail {
        match e {
            Fail::Decode(m) => Fail::Decode(format!("{}: {}", what, m)),
            Fail::Malformed(m) => Fail::Malformed(format!("{}: {}", what, m)),
        }
    }

    /// One method call: what the glue's dispatch `match` runs for each op. `name`
    /// is the Rust method's name, used in error messages.
    pub fn call<S, K, M: Method<S, K>>(obj: &mut S, method: M, name: &str, args: &[Json]) -> Result<Json, Fail> {
        method.invoke(obj, args).map_err(|e| at(&format!("`{}`", name), e))
    }

    fn arg_list<'a>(args: &'a [Json], i: usize) -> Result<&'a [Json], Fail> {
        match args.get(i) {
            Some(Json::Arr(a)) => Ok(a),
            _ => Err(Fail::Decode(format!("operation {}'s arguments are not an array", i + 1))),
        }
    }

    /// Runs a whole case. `new` is the class's constructor; `dispatch` maps a
    /// case's op name to a `call`, or returns `None` for a name it doesn't know.
    /// The glue generates `dispatch` from the op names the payload's cases use.
    ///
    /// The object is never dropped, on any path (it's held in a `ManuallyDrop`),
    /// for the same reason `ret` never drops a return value: freeing a long linked
    /// structure recurses once per node, which could overflow the stack after a
    /// clear error was already found, and the process exits after this case anyway.
    pub fn replay<S, K, C, D>(case: &[Json], new: C, mut dispatch: D) -> Result<Json, Fail>
    where
        C: Constructor<S, K>,
        D: FnMut(&mut S, &str, &[Json]) -> Option<Result<Json, Fail>>,
    {
        let (ops, args) = match case {
            [Json::Arr(ops), Json::Arr(args)] => (ops, args),
            _ => return Err(Fail::Decode("an operations case's input must be [ops, args]".into())),
        };
        if ops.is_empty() || ops.len() != args.len() {
            return Err(Fail::Decode(format!(
                "an operations case needs one argument list per op (got {} ops, {} argument lists)",
                ops.len(),
                args.len()
            )));
        }
        let mut obj = std::mem::ManuallyDrop::new(
            new.construct(arg_list(args, 0)?).map_err(|e| at("the constructor `new`", e))?,
        );
        let mut out = Vec::with_capacity(ops.len());
        out.push(Json::Null);
        for i in 1..ops.len() {
            let op = match &ops[i] {
                Json::Str(op) => op.as_str(),
                other => return Err(Fail::Decode(format!("operation {} is not a name: {}", i + 1, other.dump()))),
            };
            match dispatch(&mut *obj, op, arg_list(args, i)?) {
                Some(result) => out.push(result?),
                None => return Err(Fail::Decode(format!("unknown operation {:?}", op))),
            }
        }
        Ok(Json::Arr(out))
    }
}

// --- custom validators -------------------------------------------------------------

/// A problem's Rust custom validator (DESIGN.md §5.4; docs/adr/0007): the author's
/// `validate` plus a generated `main` that calls `validator::__entry(validate)`.
///
/// It's its own program, compiled from `validator.rs` and never linked with the
/// submission, because only the trusted harness may decide a verdict. harness.rs
/// runs it once per case, *after* the case's own process has ended, and talks to
/// it over pipes:
///
/// * stdin: one JSON object, `{"actual", "expected", "args", "probe_results"}`
///   (the same four values Python's `validate(actual, expected, args,
///   probe_results)` receives).
/// * stdout: the verdict, `true` or `false`, on the last line. It's the last
///   line rather than all of stdout so a `println!` the author left in while
///   debugging doesn't corrupt it. No verdict, or a panic (its message on
///   stderr), is the author's bug: harness.rs reports a judge_error, never the
///   submission's fault.
///
/// The author writes an ordinary function with this exact signature, bringing
/// their own `use shikomi_prelude::Json;`:
///
/// ```ignore
/// fn validate(actual: &Json, expected: &Json, args: &Json, probe_results: &[Json]) -> bool
/// ```
///
/// A different signature is a compile error, which harness.rs also reports as a
/// judge_error (the seed-solution tests catch it first).
pub mod validator {
    use super::Json;
    use std::io::{Read, Write};

    unsafe extern "C" {
        fn prctl(option: i32, arg2: u64, arg3: u64, arg4: u64, arg5: u64) -> i32;
    }
    const PR_SET_DUMPABLE: i32 = 4;

    /// The validator program's whole `main`. Returns its exit code: 0 with a
    /// verdict on stdout, 2 for a request it couldn't read, 101 for a panic.
    pub fn __entry(validate: fn(&Json, &Json, &Json, &[Json]) -> bool) -> i32 {
        // Non-dumpable, like harness.rs itself: this process holds `expected`, and
        // a process the submission left running (same uid) mustn't read it back.
        // harness.rs already runs this program from an execute-only file, which
        // makes it non-dumpable from exec (there's no window before this line);
        // this makes it hold however the program is started.
        unsafe { prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) };
        let mut input = String::new();
        if std::io::stdin().read_to_string(&mut input).is_err() {
            return 2;
        }
        let request = match Json::parse(&input) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("validator request is not valid JSON: {}", e);
                return 2;
            }
        };
        let probe_results = request.get("probe_results").as_arr();
        // A panic's message goes to stderr through the default hook; it can't
        // unwind out of the `extern "C"` main, so it's caught here.
        let verdict = std::panic::catch_unwind(|| {
            validate(request.get("actual"), request.get("expected"), request.get("args"), probe_results)
        });
        match verdict {
            Ok(v) => {
                let mut out = std::io::stdout();
                let _ = out.write_all(if v { b"\ntrue\n" } else { b"\nfalse\n" });
                let _ = out.flush();
                0
            }
            Err(_) => 101,
        }
    }
}
