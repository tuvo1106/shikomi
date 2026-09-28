//! Spike prelude: a dependency-free JSON value + FromJson/ToJson, standing in
//! for what a real harness_rs prelude would ship (no crates: --network=none).
use std::collections::BTreeMap;
use std::fmt::Write;

#[derive(Debug, Clone, PartialEq)]
pub enum Json { Null, Bool(bool), Num(f64), Int(i64), Str(String), Arr(Vec<Json>), Obj(BTreeMap<String, Json>) }

pub struct Parser<'a> { b: &'a [u8], i: usize }
impl<'a> Parser<'a> {
    pub fn new(s: &'a str) -> Self { Parser { b: s.as_bytes(), i: 0 } }
    fn ws(&mut self) { while self.i < self.b.len() && (self.b[self.i] as char).is_ascii_whitespace() { self.i += 1; } }
    pub fn parse(&mut self) -> Result<Json, String> {
        self.ws();
        match self.b.get(self.i).copied() {
            None => Err("eof".into()),
            Some(b'n') => { self.i += 4; Ok(Json::Null) }
            Some(b't') => { self.i += 4; Ok(Json::Bool(true)) }
            Some(b'f') => { self.i += 5; Ok(Json::Bool(false)) }
            Some(b'"') => self.string().map(Json::Str),
            Some(b'[') => {
                self.i += 1; let mut v = Vec::new();
                loop { self.ws(); if self.b[self.i] == b']' { self.i += 1; break; }
                       v.push(self.parse()?); self.ws(); if self.b[self.i] == b',' { self.i += 1; } }
                Ok(Json::Arr(v))
            }
            Some(b'{') => {
                self.i += 1; let mut m = BTreeMap::new();
                loop { self.ws(); if self.b[self.i] == b'}' { self.i += 1; break; }
                       let k = self.string()?; self.ws(); self.i += 1; let v = self.parse()?; m.insert(k, v);
                       self.ws(); if self.b[self.i] == b',' { self.i += 1; } }
                Ok(Json::Obj(m))
            }
            Some(_) => {
                let s = self.i;
                while self.i < self.b.len() && b"+-0123456789.eE".contains(&self.b[self.i]) { self.i += 1; }
                let t = std::str::from_utf8(&self.b[s..self.i]).unwrap();
                t.parse::<i64>().map(Json::Int).or_else(|_| t.parse::<f64>().map(Json::Num).map_err(|e| e.to_string()))
            }
        }
    }
    fn string(&mut self) -> Result<String, String> {
        self.i += 1; let mut out = String::new();
        loop {
            let c = *self.b.get(self.i).ok_or("unterminated string")?; self.i += 1;
            match c {
                b'"' => return Ok(out),
                b'\\' => { let e = self.b[self.i]; self.i += 1; match e {
                    b'n' => out.push('\n'), b't' => out.push('\t'), b'r' => out.push('\r'),
                    b'u' => { let h = std::str::from_utf8(&self.b[self.i..self.i+4]).unwrap(); self.i += 4;
                              out.push(char::from_u32(u32::from_str_radix(h, 16).unwrap()).unwrap_or('?')); }
                    o => out.push(o as char) } }
                _ => { let s = self.i - 1; let mut e = self.i; while e < self.b.len() && self.b[e] != b'"' && self.b[e] != b'\\' { e += 1; }
                       out.push_str(std::str::from_utf8(&self.b[s..e]).unwrap()); self.i = e; }
            }
        }
    }
}

impl Json {
    pub fn write(&self, o: &mut String) {
        match self {
            Json::Null => o.push_str("null"), Json::Bool(b) => o.push_str(if *b { "true" } else { "false" }),
            Json::Int(n) => { let _ = write!(o, "{}", n); } Json::Num(n) => { let _ = write!(o, "{}", n); }
            Json::Str(s) => { o.push('"'); for c in s.chars() { match c { '"' => o.push_str("\\\""), '\\' => o.push_str("\\\\"),
                '\n' => o.push_str("\\n"), c if (c as u32) < 0x20 => { let _ = write!(o, "\\u{:04x}", c as u32); } c => o.push(c) } } o.push('"'); }
            Json::Arr(v) => { o.push('['); for (i, x) in v.iter().enumerate() { if i > 0 { o.push(','); } x.write(o); } o.push(']'); }
            Json::Obj(m) => { o.push('{'); for (i, (k, v)) in m.iter().enumerate() { if i > 0 { o.push(','); } Json::Str(k.clone()).write(o); o.push(':'); v.write(o); } o.push('}'); }
        }
    }
    pub fn get(&self, k: &str) -> &Json { match self { Json::Obj(m) => m.get(k).unwrap_or(&Json::Null), _ => &Json::Null } }
}

pub trait FromJson: Sized { fn from_json(j: &Json) -> Result<Self, String>; }
pub trait ToJson { fn to_json(&self) -> Json; }
impl FromJson for i32 { fn from_json(j: &Json) -> Result<Self, String> { match j { Json::Int(n) => i32::try_from(*n).map_err(|e| e.to_string()), _ => Err("expected int".into()) } } }
impl FromJson for i64 { fn from_json(j: &Json) -> Result<Self, String> { match j { Json::Int(n) => Ok(*n), _ => Err("expected int".into()) } } }
impl FromJson for f64 { fn from_json(j: &Json) -> Result<Self, String> { match j { Json::Int(n) => Ok(*n as f64), Json::Num(n) => Ok(*n), _ => Err("expected number".into()) } } }
impl FromJson for bool { fn from_json(j: &Json) -> Result<Self, String> { match j { Json::Bool(b) => Ok(*b), _ => Err("expected bool".into()) } } }
impl FromJson for String { fn from_json(j: &Json) -> Result<Self, String> { match j { Json::Str(s) => Ok(s.clone()), _ => Err("expected string".into()) } } }
impl<T: FromJson> FromJson for Vec<T> { fn from_json(j: &Json) -> Result<Self, String> { match j { Json::Arr(v) => v.iter().map(T::from_json).collect(), _ => Err("expected array".into()) } } }
impl<T: FromJson> FromJson for Option<T> { fn from_json(j: &Json) -> Result<Self, String> { match j { Json::Null => Ok(None), o => T::from_json(o).map(Some) } } }
impl ToJson for i32 { fn to_json(&self) -> Json { Json::Int(*self as i64) } }
impl ToJson for i64 { fn to_json(&self) -> Json { Json::Int(*self) } }
impl ToJson for f64 { fn to_json(&self) -> Json { Json::Num(*self) } }
impl ToJson for bool { fn to_json(&self) -> Json { Json::Bool(*self) } }
impl ToJson for String { fn to_json(&self) -> Json { Json::Str(self.clone()) } }
impl ToJson for () { fn to_json(&self) -> Json { Json::Null } }
impl<T: ToJson> ToJson for Vec<T> { fn to_json(&self) -> Json { Json::Arr(self.iter().map(|x| x.to_json()).collect()) } }
impl<T: ToJson> ToJson for Option<T> { fn to_json(&self) -> Json { self.as_ref().map_or(Json::Null, |x| x.to_json()) } }
