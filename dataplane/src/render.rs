//! A page of rows as the JSON body Python answers with - its driver's types (psycopg2's, mysql-connector's), then
//! masking.apply(), formats._cell() and json_default(), then Flask's encoder (`ensure_ascii`, compact, a newline) - or a
//! Decline, for a type or value this doesn't reproduce exactly; the call is then forwarded, and Python answers it.
//! Tested against Python's own bodies for the same values on real servers: tests/render_vectors.jsonl.

use hmac::{KeyInit, Mac};
use serde_json::Value as Json;
use sha2::Sha256;

use crate::db::{Dialect, Output};
use crate::pyfmt;

#[derive(Debug, PartialEq)]
pub struct Decline;

type Out<T> = Result<T, Decline>;

/// A mask action per result column (masking.ACTIONS, `keep` as None).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Action {
    Drop,
    Null,
    Mask,
    Hash,
}

impl Action {
    pub fn named(name: Option<&str>) -> Out<Option<Action>> {
        Ok(match name {
            None | Some("keep") => None,
            Some("drop") => Some(Action::Drop),
            Some("null") => Some(Action::Null),
            Some("mask") => Some(Action::Mask),
            Some("hash") => Some(Action::Hash),
            Some(_) => return Err(Decline),
        })
    }
}

/// A value as the driver hands it to Python: what masking and JSON then see.
#[derive(Debug)]
enum Py {
    None,
    Bool(bool),
    /// An int, as its digits (any size)
    Int(String),
    Float(f64),
    /// A Decimal, as the float formats._cell() turns it into - str() of it isn't reproduced, so it can't be hashed
    Decimal(f64),
    Str(String),
    Bytes(Vec<u8>),
    /// PostgreSQL bytea, which psycopg2 hands over as a memoryview: JSON decodes it as bytes, `mask` nulls it, and
    /// masking._hash() takes str() of the memoryview - its address, not the value - which can't be reproduced
    View(Vec<u8>),
    /// A date or time: its isoformat(), what both str() and JSON give
    Iso(String),
    /// A datetime: JSON's '%Y-%m-%d %H:%M:%S' (str() is longer, so it can't be hashed)
    DateTime(String),
    /// A timedelta: str() of it, what JSON gives too
    Delta(String),
    /// A JSON object or array from PostgreSQL: json.dumps() of it, a string in the body
    Dumped(String),
}

// --------------------------------------------------------------------------------------
// Python's JSON encoding
// --------------------------------------------------------------------------------------

/// json.dumps() of a string with ensure_ascii, as Flask writes it.
pub fn json_string(text: &str, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            c => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

/// A float as json.dumps() writes it: repr(), and NaN/Infinity bare (allow_nan, the default).
fn json_float(value: f64, out: &mut String) {
    if value.is_nan() {
        out.push_str("NaN");
    } else if value.is_infinite() {
        out.push_str(if value > 0.0 { "Infinity" } else { "-Infinity" });
    } else {
        out.push_str(&pyfmt::repr(value));
    }
}

/// A PostgreSQL JSON value, read as json.loads() reads it and written as json.dumps() writes it (', ' and ': ').
fn python_dumps(value: &Json, out: &mut String) -> Out<()> {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Number(n) => match python_number(&n.to_string())? {
            Py::Int(digits) => out.push_str(&digits),
            Py::Float(f) => json_float(f, out),
            _ => return Err(Decline),
        },
        Json::String(s) => json_string(s, out),
        Json::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                python_dumps(item, out)?;
            }
            out.push(']');
        }
        Json::Object(map) => {
            out.push('{');
            for (i, (key, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                json_string(key, out);
                out.push_str(": ");
                python_dumps(item, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// A JSON number as json.loads() reads it: an int when it has no fraction or exponent (exact, `-0` is 0), else a float.
fn python_number(text: &str) -> Out<Py> {
    if text.contains(['.', 'e', 'E']) {
        return text.parse::<f64>().map(Py::Float).map_err(|_| Decline);
    }
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(Decline);
    }
    let digits = digits.trim_start_matches('0');
    Ok(Py::Int(match (digits.is_empty(), negative) {
        (true, _) => "0".to_string(),
        (false, true) => format!("-{digits}"),
        (false, false) => digits.to_string(),
    }))
}

// --------------------------------------------------------------------------------------
// What the driver hands Python
// --------------------------------------------------------------------------------------

fn text(bytes: &[u8]) -> Out<&str> {
    std::str::from_utf8(bytes).map_err(|_| Decline)
}

fn integer(bytes: &[u8]) -> Out<Py> {
    let t = text(bytes)?;
    let digits = t.strip_prefix('-').unwrap_or(t);
    if digits.is_empty()
        || !digits.bytes().all(|b| b.is_ascii_digit())
        || (digits.len() > 1 && digits.starts_with('0'))
    {
        return Err(Decline);
    }
    Ok(Py::Int(t.to_string()))
}

/// A float or a decimal as the server writes it - PostgreSQL's NaN and Infinity spellings included.
fn float(bytes: &[u8]) -> Out<f64> {
    match text(bytes)? {
        "NaN" => Ok(f64::NAN),
        "Infinity" => Ok(f64::INFINITY),
        "-Infinity" => Ok(f64::NEG_INFINITY),
        t if t
            .bytes()
            .all(|b| b.is_ascii_digit() || b"+-.eE".contains(&b)) =>
        {
            t.parse().map_err(|_| Decline)
        }
        _ => Err(Decline),
    }
}

fn two(t: &str, at: usize) -> Out<u32> {
    let part = t.get(at..at + 2).ok_or(Decline)?;
    if !part.bytes().all(|b| b.is_ascii_digit()) {
        return Err(Decline);
    }
    part.parse().map_err(|_| Decline)
}

/// `YYYY-MM-DD`, a date Python's date can hold; PostgreSQL's infinities as psycopg2 maps them.
fn date(t: &str) -> Out<String> {
    match t {
        "infinity" => return Ok("9999-12-31".into()),
        "-infinity" => return Ok("0001-01-01".into()),
        _ => {}
    }
    let valid = t.len() == 10
        && t.as_bytes()[4] == b'-'
        && t.as_bytes()[7] == b'-'
        && chrono::NaiveDate::parse_from_str(t, "%Y-%m-%d").is_ok()
        && &t[..4] != "0000";
    if valid {
        Ok(t.to_string())
    } else {
        Err(Decline)
    }
}

/// `HH:MM:SS[.f]` -> (isoformat of the time, the rest after it).
fn clock(t: &str) -> Out<(String, &str)> {
    if t.len() < 8 || t.as_bytes()[2] != b':' || t.as_bytes()[5] != b':' {
        return Err(Decline);
    }
    let (h, m, s) = (two(t, 0)?, two(t, 3)?, two(t, 6)?);
    if h > 23 || m > 59 || s > 59 {
        return Err(Decline);
    }
    let mut rest = &t[8..];
    let mut micros = 0u32;
    if let Some(fraction) = rest.strip_prefix('.') {
        let len = fraction.bytes().take_while(u8::is_ascii_digit).count();
        if len == 0 || len > 6 {
            return Err(Decline);
        }
        micros = format!("{:0<6}", &fraction[..len])
            .parse()
            .map_err(|_| Decline)?;
        rest = &fraction[len..];
    }
    let iso = if micros == 0 {
        format!("{h:02}:{m:02}:{s:02}")
    } else {
        format!("{h:02}:{m:02}:{s:02}.{micros:06}")
    };
    Ok((iso, rest))
}

/// `+HH[:MM[:SS]]` as isoformat() writes a UTC offset.
fn offset(t: &str) -> Out<String> {
    let sign = match t.as_bytes().first() {
        Some(b'+') => '+',
        Some(b'-') => '-',
        _ => return Err(Decline),
    };
    let parts: Vec<&str> = t[1..].split(':').collect();
    if parts.is_empty()
        || parts.len() > 3
        || parts
            .iter()
            .any(|p| p.len() != 2 || !p.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(Decline);
    }
    let mut out = format!("{sign}{}:{}", parts[0], parts.get(1).unwrap_or(&"00"));
    if let Some(seconds) = parts.get(2).filter(|s| **s != "00") {
        out.push(':');
        out.push_str(seconds);
    }
    Ok(out)
}

/// A timestamp, with or without its zone: JSON's '%Y-%m-%d %H:%M:%S', in the session's zone as the server wrote it.
fn timestamp(t: &str, with_zone: bool) -> Out<String> {
    match t {
        "infinity" => return Ok("9999-12-31 23:59:59".into()),
        "-infinity" => return Ok("0001-01-01 00:00:00".into()),
        _ => {}
    }
    let (day, time) = t.split_once(' ').ok_or(Decline)?;
    let day = date(day)?;
    let (iso, rest) = clock(time)?;
    if with_zone {
        offset(rest)?;
    } else if !rest.is_empty() {
        return Err(Decline);
    }
    Ok(format!("{day} {}", &iso[..8]))
}

/// str() of a timedelta of `micros` microseconds: `[-]D day[s], H:MM:SS[.ffffff]`, normalised as Python does.
fn delta(micros: i128) -> String {
    let day = 86_400_000_000i128;
    let days = micros.div_euclid(day);
    let rest = micros.rem_euclid(day);
    let (seconds, us) = (rest / 1_000_000, rest % 1_000_000);
    let clock = format!(
        "{}:{:02}:{:02}",
        seconds / 3600,
        seconds / 60 % 60,
        seconds % 60
    );
    let clock = if us == 0 {
        clock
    } else {
        format!("{clock}.{us:06}")
    };
    match days {
        0 => clock,
        1 | -1 => format!("{days} day, {clock}"),
        _ => format!("{days} days, {clock}"),
    }
}

/// `[-]H+:MM:SS[.f]` as microseconds: MySQL's TIME, and the clock part of a PostgreSQL interval.
fn duration(t: &str) -> Out<i128> {
    let (negative, t) = match t.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    let (whole, fraction) = t.split_once('.').unwrap_or((t, ""));
    let parts: Vec<&str> = whole.split(':').collect();
    if parts.len() != 3
        || parts
            .iter()
            .any(|p| p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(Decline);
    }
    if fraction.len() > 6 || !fraction.bytes().all(|b| b.is_ascii_digit()) {
        return Err(Decline);
    }
    let number = |p: &str| p.parse::<i128>().map_err(|_| Decline);
    let (h, m, s) = (number(parts[0])?, number(parts[1])?, number(parts[2])?);
    if m > 59 || s > 59 {
        return Err(Decline);
    }
    let us = if fraction.is_empty() {
        0
    } else {
        number(&format!("{fraction:0<6}"))?
    };
    let total = ((h * 60 + m) * 60 + s) * 1_000_000 + us;
    Ok(if negative { -total } else { total })
}

/// A PostgreSQL interval of days and a clock (`1 day 02:03:04`, `-01:00:00`, `3 days`) - psycopg2's timedelta. One
/// with years or months is declined: psycopg2 counts them in days, which this doesn't reproduce.
fn interval(t: &str) -> Out<i128> {
    let mut words = t.split(' ').peekable();
    let mut days = 0i128;
    if let Some(first) = words.peek().copied()
        && !first.contains(':')
    {
        days = first.parse::<i128>().map_err(|_| Decline)?;
        words.next();
        match words.next() {
            Some("day" | "days") => {}
            _ => return Err(Decline),
        }
    }
    let clock = match words.next() {
        Some(clock) => duration(clock)?,
        None => 0,
    };
    if words.next().is_some() {
        return Err(Decline);
    }
    Ok(days * 86_400_000_000 + clock)
}

/// PostgreSQL bytea in its hex output.
fn bytea(t: &str) -> Out<Vec<u8>> {
    hex::decode(t.strip_prefix("\\x").ok_or(Decline)?).map_err(|_| Decline)
}

/// One value, as the driver would have handed it to Python.
fn value(dialect: Dialect, kind: &str, bytes: &[u8]) -> Out<Py> {
    use Dialect::*;
    Ok(match (dialect, kind) {
        (Postgres, "INT2" | "INT4" | "INT8" | "OID") => integer(bytes)?,
        (Mysql, k)
            if matches!(
                k.trim_end_matches(" UNSIGNED"),
                "TINYINT" | "SMALLINT" | "MEDIUMINT" | "INT" | "BIGINT" | "YEAR" | "BOOLEAN"
            ) =>
        {
            integer(bytes)?
        }
        (Postgres, "FLOAT4" | "FLOAT8") | (Mysql, "FLOAT" | "DOUBLE") => Py::Float(float(bytes)?),
        (Postgres, "NUMERIC") | (Mysql, "DECIMAL") => Py::Decimal(float(bytes)?),
        (Postgres, "BOOL") => match bytes {
            b"t" => Py::Bool(true),
            b"f" => Py::Bool(false),
            _ => return Err(Decline),
        },
        (
            Postgres,
            "TEXT" | "VARCHAR" | "CHAR" | "BPCHAR" | "NAME" | "\"CHAR\"" | "UUID" | "INET" | "CIDR"
            | "MONEY" | "MACADDR",
        )
        | (
            Mysql,
            "VARCHAR" | "CHAR" | "TEXT" | "TINYTEXT" | "MEDIUMTEXT" | "LONGTEXT" | "ENUM" | "SET"
            | "JSON",
        ) => Py::Str(text(bytes)?.to_string()),
        (Postgres, "BYTEA") => Py::View(bytea(text(bytes)?)?),
        (Mysql, "VARBINARY" | "BINARY" | "BLOB" | "TINYBLOB" | "MEDIUMBLOB" | "LONGBLOB") => {
            Py::Bytes(bytes.to_vec())
        }
        (_, "DATE") => Py::Iso(date(text(bytes)?)?),
        (Postgres, "TIME") => {
            let (iso, rest) = clock(text(bytes)?)?;
            if !rest.is_empty() {
                return Err(Decline);
            }
            Py::Iso(iso)
        }
        (Postgres, "TIMETZ") => {
            let (iso, rest) = clock(text(bytes)?)?;
            Py::Iso(format!("{iso}{}", offset(rest)?))
        }
        (Postgres, "TIMESTAMP") | (Mysql, "DATETIME" | "TIMESTAMP") => {
            Py::DateTime(timestamp(text(bytes)?, false)?)
        }
        (Postgres, "TIMESTAMPTZ") => Py::DateTime(timestamp(text(bytes)?, true)?),
        (Postgres, "INTERVAL") => Py::Delta(delta(interval(text(bytes)?)?)),
        (Mysql, "TIME") => Py::Delta(delta(duration(text(bytes)?)?)),
        (Postgres, "JSON" | "JSONB") => {
            let parsed: Json = serde_json::from_slice(bytes).map_err(|_| Decline)?;
            match &parsed {
                Json::Object(_) | Json::Array(_) => {
                    let mut out = String::new();
                    python_dumps(&parsed, &mut out)?;
                    Py::Dumped(out)
                }
                Json::Null => Py::None,
                Json::Bool(b) => Py::Bool(*b),
                Json::String(s) => Py::Str(s.clone()),
                Json::Number(n) => python_number(&n.to_string())?,
            }
        }
        _ => return Err(Decline),
    })
}

// --------------------------------------------------------------------------------------
// Masking, as masking.apply() does it on the driver's values
// --------------------------------------------------------------------------------------

/// str() of a value, for `hash` - or Decline for one whose str() this doesn't reproduce.
fn python_str(value: &Py) -> Out<String> {
    Ok(match value {
        Py::Str(s) | Py::Iso(s) | Py::Delta(s) => s.clone(),
        Py::Bytes(b) => String::from_utf8_lossy(b).into_owned(),
        Py::Int(digits) => digits.clone(),
        Py::Bool(true) => "True".into(),
        Py::Bool(false) => "False".into(),
        Py::Float(f) if f.is_nan() => "nan".into(),
        Py::Float(f) if f.is_infinite() => {
            if *f > 0.0 {
                "inf".into()
            } else {
                "-inf".into()
            }
        }
        Py::Float(f) => pyfmt::repr(*f),
        Py::None | Py::Decimal(_) | Py::DateTime(_) | Py::Dumped(_) | Py::View(_) => {
            return Err(Decline);
        }
    })
}

/// masking._mask()
fn mask(value: Py) -> Py {
    let Py::Str(text) = value else {
        return Py::None;
    };
    if text.is_empty() {
        return Py::None;
    }
    if let Some((local, domain)) = text.rsplit_once('@') {
        let first: String = local.chars().take(1).collect();
        return Py::Str(if local.is_empty() {
            format!("***@{domain}")
        } else {
            format!("{first}***@{domain}")
        });
    }
    let length = text.chars().count();
    let first: String = text.chars().take(1).collect();
    Py::Str(format!("{first}{}", "*".repeat(3.max(length - 1))))
}

/// masking._hash(): the first 16 hex digits of HMAC-SHA256 over str() of the value.
fn hash(value: Py, key: &[u8]) -> Out<Py> {
    if matches!(value, Py::None) {
        return Ok(Py::None);
    }
    let text = python_str(&value)?;
    let mut mac = <hmac::Hmac<Sha256> as KeyInit>::new_from_slice(key).map_err(|_| Decline)?;
    mac.update(text.as_bytes());
    Ok(Py::Str(
        hex::encode(mac.finalize().into_bytes())[..16].to_string(),
    ))
}

/// formats._cell() and json_default(), written as json.dumps() writes them.
fn write(value: &Py, out: &mut String) {
    match value {
        Py::None => out.push_str("null"),
        Py::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Py::Int(digits) => out.push_str(digits),
        Py::Float(f) | Py::Decimal(f) => json_float(*f, out),
        Py::Str(s) | Py::Iso(s) | Py::DateTime(s) | Py::Delta(s) | Py::Dumped(s) => {
            json_string(s, out)
        }
        Py::Bytes(b) | Py::View(b) => json_string(&String::from_utf8_lossy(b), out),
    }
}

/// formats._unique(): a repeated name gets `_2`, `_3`, ...
fn unique(names: &[&str]) -> Vec<String> {
    let mut seen: std::collections::HashMap<&str, usize> = std::collections::HashMap::new();
    names
        .iter()
        .map(|name| {
            let count = seen.entry(name).or_insert(0);
            *count += 1;
            if *count == 1 {
                name.to_string()
            } else {
                format!("{name}_{count}")
            }
        })
        .collect()
}

/// The body for a page of rows: `rows` (already cut to the page) masked by `actions`, one per column, when the caller
/// has rules - the column count checked against the trace's, as masking.check_columns() does.
pub fn body(
    dialect: Dialect,
    output: &Output,
    rows: usize,
    actions: Option<(&[Option<Action>], usize)>,
    key: Option<&[u8]>,
) -> Out<String> {
    let rows = &output.rows[..rows.min(output.rows.len())];
    if rows.is_empty() {
        return Ok("{\"message\":\"No results returned\"}\n".into());
    }
    if let Some((actions, traced)) = actions
        && (traced != output.columns.len() || actions.len() != traced)
    {
        return Err(Decline); // Python refuses this one, mask_unresolvable
    }
    let none: Vec<Option<Action>> = vec![None; output.columns.len()];
    let actions = actions.map(|(a, _)| a).unwrap_or(&none);
    let kept: Vec<usize> = (0..output.columns.len())
        .filter(|i| actions[*i] != Some(Action::Drop))
        .collect();
    let names: Vec<&str> = kept.iter().map(|i| output.columns[*i].0.as_str()).collect();
    let names = unique(&names);
    // dict(zip(columns, row)): a name made twice keeps its first place and its last value
    let mut order: Vec<usize> = Vec::new();
    let mut position: std::collections::HashMap<&str, usize> = std::collections::HashMap::new();
    for (i, name) in names.iter().enumerate() {
        match position.get(name.as_str()) {
            Some(&at) => order[at] = i,
            None => {
                position.insert(name, order.len());
                order.push(i);
            }
        }
    }
    let mut out = String::from("[");
    for (r, row) in rows.iter().enumerate() {
        let mut values = Vec::with_capacity(kept.len());
        for &i in &kept {
            let (_, kind) = &output.columns[i];
            let mut value = match &row[i] {
                None => Py::None,
                Some(bytes) => value(dialect, kind, bytes)?,
            };
            value = match actions[i] {
                None | Some(Action::Drop) => value,
                Some(Action::Null) => Py::None,
                Some(Action::Mask) => mask(value),
                Some(Action::Hash) => hash(value, key.ok_or(Decline)?)?,
            };
            values.push(value);
        }
        if r > 0 {
            out.push(',');
        }
        out.push('{');
        for (n, &i) in order.iter().enumerate() {
            if n > 0 {
                out.push(',');
            }
            json_string(&names[i], &mut out);
            out.push(':');
            write(&values[i], &mut out);
        }
        out.push('}');
    }
    out.push_str("]\n");
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every value Python rendered: this renders the same body, byte for byte, or declines. Declines are counted, so
    /// a type this should render can't silently stop being served.
    #[test]
    fn renders_as_python_does_or_declines() {
        let key = b"render-vectors-key";
        let (mut same, mut declined) = (0, Vec::new());
        for line in include_str!("../tests/render_vectors.jsonl").lines() {
            let case: Json = serde_json::from_str(line).unwrap();
            let dialect = if case["dialect"] == "postgres" {
                Dialect::Postgres
            } else {
                Dialect::Mysql
            };
            let columns: Vec<(String, String)> =
                serde_json::from_value(case["columns"].clone()).unwrap();
            let rows: Vec<Vec<Option<Vec<u8>>>> = case["hex"]
                .as_array()
                .unwrap()
                .iter()
                .map(|row| {
                    row.as_array()
                        .unwrap()
                        .iter()
                        .map(|c| c.as_str().map(|h| hex::decode(h).unwrap()))
                        .collect()
                })
                .collect();
            let output = Output { columns, rows };
            let actions: Option<Vec<Option<Action>>> = case["actions"].as_array().map(|a| {
                a.iter()
                    .map(|n| Action::named(n.as_str()).unwrap())
                    .collect()
            });
            let masked = actions.as_ref().map(|a| (a.as_slice(), a.len()));
            let ours = body(dialect, &output, output.rows.len(), masked, Some(key));
            match (ours, case["expected"].as_str()) {
                (Ok(ours), Some(python)) => {
                    assert_eq!(ours, python, "{}", case["sql"]);
                    same += 1;
                }
                (Ok(ours), None) => panic!("rendered {ours} where Python fails: {}", case["sql"]),
                (Err(Decline), _) => declined.push(case["sql"].as_str().unwrap().to_string()),
            }
        }
        eprintln!("same as Python: {same}; declined: {declined:?}");
        assert!(same >= 185, "only {same} rendered");
        assert!(declined.len() <= 3, "declined {declined:?}");
    }

    #[test]
    fn timedelta_as_python_writes_it() {
        assert_eq!(delta(0), "0:00:00");
        assert_eq!(delta(-3_600_000_000), "-1 day, 23:00:00");
        assert_eq!(delta(76 * 3_600_000_000), "3 days, 4:00:00");
        assert_eq!(delta(-500_000), "-1 day, 23:59:59.500000");
        assert_eq!(delta(86_400_000_000 + 7_384_000_000), "1 day, 2:03:04");
    }

    #[test]
    fn an_interval_with_months_is_declined() {
        assert_eq!(
            interval("1 day -01:00:00").map(delta),
            Ok("23:00:00".into())
        );
        assert!(interval("1 mon").is_err());
        assert!(interval("1 year 2 mons").is_err());
    }
}
