//! A saved query's parameter rules (params.py), applied to the values a request supplied - or declined, so the call
//! is forwarded and Python answers it. Wherever this binds a value it is the one Python would bind, of the same type;
//! wherever Python would refuse, or accept something this can't reproduce exactly (`int(" 5 ")`, `1_000`, Unicode
//! digits, a `re` pattern), it declines. Tested against Python's own outcomes: tests/params_vectors.jsonl.

use std::cmp::Ordering;
use std::collections::{BTreeSet, HashMap};

use serde_json::{Map, Value as Json};

/// A value as Python holds it once resolved - what gets bound.
#[derive(Clone, Debug, PartialEq)]
pub enum Param {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
}

/// A supplied value this can't represent as Python does (a list, an integer past 64 bits, `1e400`): fine where
/// nothing reads it, a reason to forward where something does.
#[derive(Clone, Debug, PartialEq)]
pub enum Supplied {
    Value(Param),
    Unsupported,
}

/// Why this declines - always answered by forwarding the call.
#[derive(Debug, PartialEq)]
pub struct Decline;

type Out<T> = Result<T, Decline>;

/// A query-string value: always text.
pub fn from_url(text: &str) -> Supplied {
    Supplied::Value(Param::Text(text.to_string()))
}

/// A JSON body's value, as Python's json.loads() reads it: an integer stays an integer at any size (so one past 64 bits
/// can't be represented here), a number with a fraction or exponent is a float.
pub fn from_json(value: &Json) -> Supplied {
    match value {
        Json::Null => Supplied::Value(Param::Null),
        Json::Bool(b) => Supplied::Value(Param::Bool(*b)),
        Json::String(s) => Supplied::Value(Param::Text(s.clone())),
        Json::Number(n) => {
            let text = n.to_string();
            if text.contains(['.', 'e', 'E']) {
                match text.parse::<f64>() {
                    Ok(f) if f.is_finite() => Supplied::Value(Param::Float(f)),
                    _ => Supplied::Unsupported,
                }
            } else {
                text.parse::<i64>()
                    .map(|n| Supplied::Value(Param::Int(n)))
                    .unwrap_or(Supplied::Unsupported)
            }
        }
        _ => Supplied::Unsupported,
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Type {
    Integer,
    Number,
    Text,
    Boolean,
    Any,
}

#[derive(Debug)]
struct Spec {
    kind: Type,
    required: bool,
    default: Option<Param>,
    choices: Option<Vec<Param>>,
    min: Option<Param>,
    max: Option<Param>,
    min_length: Option<u64>,
    max_length: Option<u64>,
}

const RULES: [&str; 13] = [
    "type",
    "required",
    "default",
    "enum",
    "min",
    "max",
    "min_length",
    "max_length",
    "pattern",
    "description",
    "example",
    "embed",
    "from_claim",
];

fn type_named(name: &str) -> Out<Type> {
    Ok(match name {
        "int" | "integer" => Type::Integer,
        "float" | "number" => Type::Number,
        "str" | "string" => Type::Text,
        "bool" | "boolean" => Type::Boolean,
        _ => return Err(Decline),
    })
}

/// `params._parse_strict()`, declining whatever it rejects - Python then degrades the definition, which this doesn't
/// reproduce - and the rules it doesn't apply itself (`pattern`, `embed`, `from_claim`).
fn parse(spec: &Json) -> Out<Spec> {
    let spec: Map<String, Json> = match spec {
        Json::String(name) => Map::from_iter([("type".to_string(), Json::String(name.clone()))]),
        Json::Object(map) => map.clone(),
        _ => return Err(Decline),
    };
    if spec.keys().any(|key| !RULES.contains(&key.as_str())) {
        return Err(Decline);
    }
    if spec.contains_key("pattern") || spec.contains_key("from_claim") {
        return Err(Decline);
    }
    let kind = match spec.get("type") {
        None | Some(Json::Null) => Type::Any,
        Some(Json::String(name)) if name.is_ascii() => type_named(&name.to_ascii_lowercase())?,
        Some(_) => return Err(Decline),
    };
    let mut parsed = Spec {
        kind,
        required: true,
        default: None,
        choices: None,
        min: None,
        max: None,
        min_length: None,
        max_length: None,
    };
    match spec.get("required") {
        Some(Json::Bool(required)) => parsed.required = *required,
        Some(_) => return Err(Decline),
        None if spec.contains_key("default") => parsed.required = false,
        None => {}
    }
    if spec.get("description").is_some_and(|d| !d.is_string()) {
        return Err(Decline);
    }
    match spec.get("embed") {
        None | Some(Json::Bool(false)) => {}
        Some(_) => return Err(Decline),
    }
    let numeric = matches!(kind, Type::Any | Type::Integer | Type::Number);
    let textual = matches!(kind, Type::Any | Type::Text);
    for (key, slot) in [("min", &mut parsed.min), ("max", &mut parsed.max)] {
        if let Some(value) = spec.get(key) {
            match (numeric, from_json(value)) {
                (true, Supplied::Value(number @ (Param::Int(_) | Param::Float(_)))) => {
                    *slot = Some(number)
                }
                _ => return Err(Decline),
            }
        }
    }
    if let (Some(min), Some(max)) = (&parsed.min, &parsed.max)
        && compare(min, max)? == Ordering::Greater
    {
        return Err(Decline);
    }
    for (key, slot) in [
        ("min_length", &mut parsed.min_length),
        ("max_length", &mut parsed.max_length),
    ] {
        if let Some(value) = spec.get(key) {
            match (textual, from_json(value)) {
                (true, Supplied::Value(Param::Int(n))) if n >= 0 => *slot = Some(n as u64),
                _ => return Err(Decline),
            }
        }
    }
    if let (Some(min), Some(max)) = (parsed.min_length, parsed.max_length)
        && min > max
    {
        return Err(Decline);
    }
    if let Some(choices) = spec.get("enum") {
        let Json::Array(choices) = choices else {
            return Err(Decline);
        };
        if choices.is_empty() {
            return Err(Decline);
        }
        let mut coerced = Vec::with_capacity(choices.len());
        for choice in choices {
            match from_json(choice) {
                Supplied::Value(value) => coerced.push(coerce(kind, value)?),
                Supplied::Unsupported => return Err(Decline),
            }
        }
        parsed.choices = Some(coerced);
    }
    if let Some(default) = spec.get("default") {
        if spec.get("required") == Some(&Json::Bool(true)) {
            return Err(Decline);
        }
        let Supplied::Value(default) = from_json(default) else {
            return Err(Decline);
        };
        parsed.default = Some(check(&parsed, default)?);
    }
    if let Some(example) = spec.get("example") {
        let Supplied::Value(example) = from_json(example) else {
            return Err(Decline);
        };
        check(&parsed, example)?;
    }
    Ok(parsed)
}

/// Decimal digits with an optional sign, nothing else - `int()` also takes spaces, `_` and other scripts' digits,
/// which this declines.
fn plain_integer(text: &str) -> Option<i64> {
    let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// A plain decimal (`5`, `-0.5`, `.5`, `5.`, `1e3`) - what both `float()` and Rust read the same, correctly rounded.
fn plain_float(text: &str) -> Option<f64> {
    let body = text.strip_prefix(['+', '-']).unwrap_or(text);
    let (mantissa, exponent) = match body.find(['e', 'E']) {
        Some(i) => (&body[..i], Some(&body[i + 1..])),
        None => (body, None),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let digits = |s: &str| s.bytes().all(|b| b.is_ascii_digit());
    if (whole.is_empty() && fraction.is_empty()) || !digits(whole) || !digits(fraction) {
        return None;
    }
    if let Some(exponent) = exponent {
        let exponent = exponent.strip_prefix(['+', '-']).unwrap_or(exponent);
        if exponent.is_empty() || !digits(exponent) {
            return None;
        }
    }
    text.parse::<f64>().ok().filter(|f| f.is_finite())
}

/// `params._coerce()`.
fn coerce(kind: Type, value: Param) -> Out<Param> {
    match (kind, value) {
        (Type::Integer, Param::Text(text)) => plain_integer(&text).map(Param::Int).ok_or(Decline),
        (Type::Integer, value @ Param::Int(_)) => Ok(value),
        (Type::Number, Param::Text(text)) => plain_float(&text).map(Param::Float).ok_or(Decline),
        (Type::Number, value @ (Param::Int(_) | Param::Float(_))) => Ok(value),
        (Type::Boolean, Param::Text(text)) => {
            if !text.is_ascii() {
                return Err(Decline); // str.strip() and .lower() reach further than ASCII
            }
            match text
                .trim_matches(|c: char| c.is_ascii_whitespace() || c == '\x0b')
                .to_ascii_lowercase()
                .as_str()
            {
                "true" | "1" | "yes" => Ok(Param::Bool(true)),
                "false" | "0" | "no" => Ok(Param::Bool(false)),
                _ => Err(Decline),
            }
        }
        (Type::Boolean, value @ Param::Bool(_)) => Ok(value),
        (Type::Text, value @ Param::Text(_)) => Ok(value),
        (
            Type::Any,
            value @ (Param::Text(_) | Param::Bool(_) | Param::Int(_) | Param::Float(_)),
        ) => Ok(value),
        _ => Err(Decline),
    }
}

/// Python's `==` between the values a parameter can hold: numbers and booleans compare by value (`True == 1 == 1.0`),
/// text only with text.
fn same(a: &Param, b: &Param) -> bool {
    match (a, b) {
        (Param::Text(x), Param::Text(y)) => x == y,
        (Param::Null, Param::Null) => true,
        (Param::Text(_), _) | (_, Param::Text(_)) | (Param::Null, _) | (_, Param::Null) => false,
        _ => compare(a, b) == Ok(Ordering::Equal),
    }
}

fn as_number(value: &Param) -> Option<(i64, f64, bool)> {
    match value {
        Param::Bool(b) => Some((*b as i64, 0.0, true)),
        Param::Int(n) => Some((*n, 0.0, true)),
        Param::Float(f) => Some((0, *f, false)),
        _ => None,
    }
}

/// Exact ordering of two numbers, an integer against a float included - as Python compares them.
fn compare(a: &Param, b: &Param) -> Out<Ordering> {
    let (Some((ai, af, a_int)), Some((bi, bf, b_int))) = (as_number(a), as_number(b)) else {
        return Err(Decline);
    };
    Ok(match (a_int, b_int) {
        (true, true) => ai.cmp(&bi),
        (false, false) => af.partial_cmp(&bf).ok_or(Decline)?,
        (true, false) => int_vs_float(ai, bf),
        (false, true) => int_vs_float(bi, af).reverse(),
    })
}

fn int_vs_float(i: i64, f: f64) -> Ordering {
    const LIMIT: f64 = 9_223_372_036_854_775_808.0; // 2**63
    if f >= LIMIT {
        return Ordering::Less;
    }
    if f < -LIMIT {
        return Ordering::Greater;
    }
    let whole = f.trunc();
    match i.cmp(&(whole as i64)) {
        Ordering::Equal => 0.0f64.partial_cmp(&(f - whole)).unwrap_or(Ordering::Equal),
        other => other,
    }
}

/// `params._check()`.
fn check(spec: &Spec, value: Param) -> Out<Param> {
    let value = coerce(spec.kind, value)?;
    if let Some(choices) = &spec.choices
        && !choices.iter().any(|choice| same(&value, choice))
    {
        return Err(Decline);
    }
    if matches!(value, Param::Int(_) | Param::Float(_)) {
        if let Some(min) = &spec.min
            && compare(&value, min)? == Ordering::Less
        {
            return Err(Decline);
        }
        if let Some(max) = &spec.max
            && compare(&value, max)? == Ordering::Greater
        {
            return Err(Decline);
        }
    }
    if let Param::Text(text) = &value {
        let length = text.chars().count() as u64;
        if spec.min_length.is_some_and(|min| length < min)
            || spec.max_length.is_some_and(|max| length > max)
        {
            return Err(Decline);
        }
    }
    Ok(value)
}

/// `params.resolve()` for the names in `used`: the value of each, or Decline where Python would refuse - or might
/// decide differently. Undeclared values pass through untouched, as there.
pub fn resolve(
    declared: Option<&Json>,
    supplied: &HashMap<String, Supplied>,
    used: &BTreeSet<String>,
) -> Out<HashMap<String, Supplied>> {
    let mut values = supplied.clone();
    let Some(Json::Object(declared)) = declared else {
        return Ok(values);
    };
    for (name, spec) in declared {
        if !used.contains(name) {
            continue;
        }
        let spec = parse(spec)?;
        match values.get(name).cloned() {
            None => {
                let value = match (&spec.default, spec.required) {
                    (Some(default), _) => default.clone(),
                    (None, true) => return Err(Decline),
                    (None, false) => Param::Null,
                };
                values.insert(name.clone(), Supplied::Value(value));
            }
            Some(Supplied::Value(Param::Null)) => {
                if spec.required {
                    return Err(Decline);
                }
            }
            Some(Supplied::Value(value)) => {
                values.insert(name.clone(), Supplied::Value(check(&spec, value)?));
            }
            Some(Supplied::Unsupported) => return Err(Decline),
        }
    }
    Ok(values)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A tagged Python value as this holds it: what it can't represent - a list passed through, an integer past 64 bits,
    /// an infinity - is Unsupported, declined only where something binds it.
    fn tagged(tag: &Json) -> Supplied {
        let tag = tag.as_array().unwrap();
        let text = || tag[1].as_str().unwrap();
        let value = match tag[0].as_str().unwrap() {
            "null" => Param::Null,
            "bool" => Param::Bool(tag[1].as_bool().unwrap()),
            "int" => match text().parse() {
                Ok(n) => Param::Int(n),
                Err(_) => return Supplied::Unsupported,
            },
            "float" => match text().parse::<f64>() {
                Ok(f) if f.is_finite() => Param::Float(f),
                _ => return Supplied::Unsupported,
            },
            "str" => Param::Text(text().to_string()),
            _ => return Supplied::Unsupported,
        };
        Supplied::Value(value)
    }

    /// Every case Python decided: where this binds, Python bound the same values, of the same types; where Python
    /// refused, this declines. Where this declines a case Python accepted, the call is merely forwarded - counted, so
    /// it stays a small share.
    #[test]
    fn agrees_with_python_on_every_vector() {
        let (mut bound, mut declined_accepted) = (0, 0);
        for (line, text) in include_str!("../tests/params_vectors.jsonl")
            .lines()
            .enumerate()
        {
            let case: Json = serde_json::from_str(text).unwrap();
            let mut supplied = HashMap::new();
            for s in case["supplied"].as_array().unwrap() {
                let raw = s["raw"].as_str().unwrap();
                let value = if s["from"] == "url" {
                    from_url(raw)
                } else {
                    from_json(&serde_json::from_str(raw).unwrap())
                };
                supplied.insert(s["name"].as_str().unwrap().to_string(), value);
            }
            let used: BTreeSet<String> = case["used"]
                .as_array()
                .unwrap()
                .iter()
                .map(|u| u.as_str().unwrap().to_string())
                .collect();
            let expected = &case["expected"];
            match resolve(Some(&case["declared"]), &supplied, &used) {
                Ok(values) => {
                    let python = expected.get("values").unwrap_or_else(|| {
                        panic!(
                            "line {}: bound {values:?} where Python refused: {text}",
                            line + 1
                        )
                    });
                    for (name, tag) in python.as_object().unwrap() {
                        let ours = values.get(name);
                        assert_eq!(ours, Some(&tagged(tag)), "line {}: {text}", line + 1);
                    }
                    bound += 1;
                }
                Err(Decline) => {
                    // Definitions the API can't save (Python degrades them at run time) are always declined
                    if expected.get("values").is_some() && case["valid"] == true {
                        declined_accepted += 1;
                        if std::env::var("SHOW_DECLINED").is_ok() {
                            eprintln!("DECLINED {} {}", case["declared"], case["supplied"]);
                        }
                    }
                }
            }
        }
        assert!(bound > 800, "bound only {bound}");
        eprintln!("bound {bound}; declined {declined_accepted} valid ones Python accepted");
        // All of them deliberate: from_claim, embed and pattern parameters (Python's), and values int() and float()
        // read that this doesn't reproduce (' 5', '1_000', other scripts' digits, integers past 64 bits)
        assert!(
            declined_accepted <= 179,
            "declined {declined_accepted} valid cases Python accepted - more than before"
        );
    }

    #[test]
    fn a_float_keeps_python_s_value_and_an_int_stays_an_int() {
        assert_eq!(
            from_json(&serde_json::from_str("5").unwrap()),
            Supplied::Value(Param::Int(5))
        );
        assert_eq!(
            from_json(&serde_json::from_str("5.0").unwrap()),
            Supplied::Value(Param::Float(5.0))
        );
        assert_eq!(
            from_json(&serde_json::from_str("123456789012345678901234567890").unwrap()),
            Supplied::Unsupported
        );
    }

    #[test]
    fn compares_an_integer_with_a_float_exactly() {
        assert_eq!(
            compare(
                &Param::Int(9_007_199_254_740_993),
                &Param::Float(9_007_199_254_740_992.0)
            ),
            Ok(Ordering::Greater)
        );
        assert_eq!(
            compare(&Param::Int(1), &Param::Float(1.5)),
            Ok(Ordering::Less)
        );
        assert_eq!(
            compare(&Param::Int(-1), &Param::Float(-1.5)),
            Ok(Ordering::Greater)
        );
        assert!(same(&Param::Bool(true), &Param::Int(1)));
        assert!(!same(&Param::Text("1".into()), &Param::Int(1)));
    }
}
