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

#[cfg(test)]
mod tests {
    use super::*;

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
}
