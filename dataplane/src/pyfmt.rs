//! Python's number formatting, where the data plane must write a number as Python does: `repr(float)` - how both
//! drivers write a float into SQL, and how JSON writes one - and `format(x, 'g')`, MariaDB's time limit.

/// (sign, shortest round-trip digits, decimal exponent of the first digit): `-1.5e-5` is ("-", "15", -5).
fn shortest(value: f64) -> (&'static str, String, i32) {
    let text = format!("{:e}", value.abs()); // Rust's `{:e}` writes the shortest digits that round-trip
    let (mantissa, exponent) = text.split_once('e').expect("{:e} has an exponent");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let sign = if value.is_sign_negative() { "-" } else { "" };
    (sign, digits, exponent.parse().expect("an integer exponent"))
}

fn scientific(sign: &str, digits: &str, exponent: i32) -> String {
    let (first, rest) = digits.split_at(1);
    let mantissa = if rest.is_empty() {
        first.to_string()
    } else {
        format!("{first}.{rest}")
    };
    let exp_sign = if exponent < 0 { '-' } else { '+' };
    format!("{sign}{mantissa}e{exp_sign}{:02}", exponent.abs())
}

fn fixed(sign: &str, digits: &str, exponent: i32, add_dot_zero: bool) -> String {
    let point = exponent + 1; // digits before the decimal point
    let body = if point <= 0 {
        format!("0.{}{digits}", "0".repeat((-point) as usize))
    } else if point as usize >= digits.len() {
        let whole = format!("{digits}{}", "0".repeat(point as usize - digits.len()));
        if add_dot_zero {
            format!("{whole}.0")
        } else {
            whole
        }
    } else {
        let (whole, fraction) = digits.split_at(point as usize);
        format!("{whole}.{fraction}")
    };
    format!("{sign}{body}")
}

/// `repr(value)` for a finite float.
pub fn repr(value: f64) -> String {
    debug_assert!(value.is_finite());
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    let (sign, digits, exponent) = shortest(value);
    if !(-4..16).contains(&exponent) {
        scientific(sign, &digits, exponent)
    } else {
        fixed(sign, &digits, exponent, true)
    }
}

/// `format(value, 'g')` for a finite, positive float: six significant digits, trailing zeros dropped.
pub fn general(value: f64) -> String {
    if value == 0.0 {
        return "0".into();
    }
    let rounded = format!("{:.5e}", value); // six significant digits, rounded half to even as Python does
    let (mantissa, exponent) = rounded.split_once('e').expect("an exponent");
    let exponent: i32 = exponent.parse().expect("an integer exponent");
    let mut digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    while digits.len() > 1 && digits.ends_with('0') {
        digits.pop();
    }
    let sign = if value < 0.0 { "-" } else { "" };
    if !(-4..6).contains(&exponent) {
        scientific(sign, &digits, exponent)
    } else {
        fixed(sign, &digits, exponent, false)
    }
}

/// A time limit in seconds as Python holds it: a float, as the server's and `?timeout=` are, or an int - a key's own
/// whole-number `query_timeout` (ADR 0011), which `min()` hands back as it is. Python writes the two differently,
/// `5` and `5.0`, in a 504's body and in the log.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Seconds {
    pub value: f64,
    pub whole: bool,
}

impl Seconds {
    pub fn float(value: f64) -> Self {
        Seconds {
            value,
            whole: false,
        }
    }

    /// A key's `query_timeout` as the plan carries it: an int stays one
    pub fn from_json(number: &serde_json::Number) -> Option<Self> {
        let value = number.as_f64().filter(|v| v.is_finite() && *v > 0.0)?;
        Some(Seconds {
            value,
            whole: number.is_i64() || number.is_u64(),
        })
    }

    /// `min(self, other)` as Python's: `self` unless `other` is smaller
    pub fn min(self, other: Self) -> Self {
        if other.value < self.value {
            other
        } else {
            self
        }
    }

    /// `repr()` of it
    pub fn repr(self) -> String {
        if self.whole {
            format!("{}", self.value as i64)
        } else {
            repr(self.value)
        }
    }

    /// `json.dumps()` of it
    pub fn json(self) -> serde_json::Value {
        if self.whole {
            serde_json::json!(self.value as i64)
        } else {
            serde_json::json!(self.value)
        }
    }
}

/// `json.dumps(value)` as Python writes it - `", "` and `": "`, ASCII only, floats as `repr()` - for a run's history
/// entry, which Python stores as this text (ADR 0028).
pub fn dumps(value: &serde_json::Value) -> String {
    let mut out = String::new();
    dump(value, &mut out);
    out
}

fn dump(value: &serde_json::Value, out: &mut String) {
    use serde_json::Value;
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) if n.is_i64() || n.is_u64() => out.push_str(&n.to_string()),
        Value::Number(n) => out.push_str(&repr(n.as_f64().unwrap_or(f64::NAN))),
        Value::String(s) => crate::render::json_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                dump(item, out);
            }
            out.push(']');
        }
        Value::Object(fields) => {
            out.push('{');
            for (i, (name, item)) in fields.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                crate::render::json_string(name, out);
                out.push_str(": ");
                dump(item, out);
            }
            out.push('}');
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dumps_as_python() {
        // json.dumps() of each, in Python 3.12
        let entry = serde_json::json!({
            "executed_at": "2026-10-27 12:00:00", "key_name": "caf\u{e9} \u{1F600}", "rows": 3, "duration_ms": 1,
            "serialization_ms": 0.1, "big": 1e16, "masked": [{"column": "c.email", "action": "hash"}], "none": null,
            "ok": true, "quote": "a\"b\\c\nd",
        });
        // the escapes as Python writes them: \u00e9, a surrogate pair, \", \\ and \n
        assert_eq!(
            dumps(&entry),
            "{\"executed_at\": \"2026-10-27 12:00:00\", \"key_name\": \"caf\\u00e9 \\ud83d\\ude00\", \"rows\": 3, \"duration_ms\": 1, \"serialization_ms\": 0.1, \"big\": 1e+16, \"masked\": [{\"column\": \"c.email\", \"action\": \"hash\"}], \"none\": null, \"ok\": true, \"quote\": \"a\\\"b\\\\c\\nd\"}"
        );
    }

    #[test]
    fn repr_as_python() {
        // repr() of each, in Python 3.12
        for (value, python) in [
            (5.0, "5.0"),
            (-0.0, "-0.0"),
            (0.1, "0.1"),
            (123.456, "123.456"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (1e-7, "1e-07"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.5e-300, "1.5e-300"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (-2.5, "-2.5"),
            (1234567.125, "1234567.125"),
            (9007199254740993.0, "9007199254740992.0"),
            (0.30000000000000004, "0.30000000000000004"),
        ] {
            assert_eq!(repr(value), python, "{value}");
        }
    }

    #[test]
    fn general_as_python() {
        for (value, python) in [
            (30.0, "30"),
            (0.5, "0.5"),
            (2.25, "2.25"),
            (1234567.0, "1.23457e+06"),
            (100000.0, "100000"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (12.3456789, "12.3457"),
        ] {
            assert_eq!(general(value), python, "{value}");
        }
    }

    #[test]
    fn a_key_s_whole_timeout_stays_whole_as_python_s_min_keeps_it() {
        let number = |text: &str| serde_json::from_str::<serde_json::Number>(text).unwrap();
        let five = Seconds::from_json(&number("5")).unwrap();
        let thirty = Seconds::float(30.0);
        // min(30.0, 5) is 5 and min(5.0, 5) is 5.0 in Python: the first unless the second is smaller
        assert_eq!(thirty.min(five).repr(), "5");
        assert_eq!(thirty.min(five).json().to_string(), "5");
        assert_eq!(Seconds::float(5.0).min(five).repr(), "5.0");
        assert_eq!(Seconds::float(0.3).min(five).repr(), "0.3");
        let quarter = Seconds::from_json(&number("0.25")).unwrap();
        assert_eq!(thirty.min(quarter).repr(), "0.25");
        assert_eq!(thirty.min(quarter).json().to_string(), "0.25");
        assert_eq!(Seconds::float(30.0).json().to_string(), "30.0");
        for refused in ["0", "-1", "0.0"] {
            assert_eq!(Seconds::from_json(&number(refused)), None, "{refused}");
        }
    }
}
