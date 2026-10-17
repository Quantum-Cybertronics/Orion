r"""Grammars that force a model's reply to be a particular JSON object.

llama.cpp can restrict generation to text matching a GBNF grammar, so the
reply is always valid JSON of the right shape, whatever the model.

ORION builds the grammar itself and sends it as the plain ``grammar`` field,
rather than asking the server to convert a ``response_format`` schema. Some
llama.cpp builds wrap a converted schema in the chat template's own
generation prompt (``root ::= "<start_of_turn>model\n" ...``) and then fail to
start sampling with Gemma models ("Unexpected empty grammar stack"). A
verbatim ``grammar`` avoids that code path.

Only the small shape ORION needs is supported: a flat JSON object whose
properties are strings (optionally with ``maxLength``), string enums or
integers. Required properties come first (in schema order); optional ones may
follow, each at most once and in schema order. Anything else raises
``ValueError`` so the caller can fall back to unconstrained generation.

The rule forms below are the ones llama.cpp's own schema converter produces.
"""

import json
import re

_BASE_RULES = r'''space ::= | " " | "\n"{1,2} [ \t]{0,20}
char ::= [^"\\\x7F\x00-\x1F] | [\\] (["\\bfnrt] | "u" [0-9a-fA-F]{4})
integral-part ::= [0] | [1-9] [0-9]{0,15}
integer ::= ("-"? integral-part) space'''


def _literal(text: str) -> str:
    """A GBNF string literal that matches ``text`` exactly."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )

    return f'"{escaped}"'


def _rule_name(prefix: str, name: str) -> str:
    return prefix + "-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _value_rule(name: str, spec: dict) -> str:
    """The right-hand side of the rule matching one property's value."""
    kind = spec.get("type")

    if "enum" in spec:
        values = spec["enum"]

        if not values or not all(isinstance(v, str) for v in values):
            raise ValueError(f"'{name}': only non-empty string enums are supported")

        return "(" + " | ".join(_literal(json.dumps(v)) for v in values) + ")"

    if kind == "string":
        limit = spec.get("maxLength")

        if limit is None:
            return '"\\"" char* "\\"" space'

        if not isinstance(limit, int) or limit < 0:
            raise ValueError(f"'{name}': maxLength must be a non-negative integer")

        return f'"\\"" char{{0,{limit}}} "\\""'

    if kind == "integer":
        return "integer"

    raise ValueError(f"'{name}': unsupported type {kind!r}")


def schema_to_gbnf(schema: dict) -> str:
    """Compile a flat object schema into a GBNF grammar (root rule: ``root``)."""
    if schema.get("type") != "object":
        raise ValueError("only object schemas are supported")

    properties = schema.get("properties")

    if not isinstance(properties, dict) or not properties:
        raise ValueError("the schema needs properties")

    required_names = set(schema.get("required", []))

    unknown = required_names - set(properties)

    if unknown:
        raise ValueError(f"required properties are not defined: {sorted(unknown)}")

    required = [name for name in properties if name in required_names]
    optional = [name for name in properties if name not in required_names]

    if not required:
        raise ValueError("at least one property must be required")

    rules = [_BASE_RULES]

    for name, spec in properties.items():
        value = _rule_name("v", name)
        rules.append(f"{value} ::= {_value_rule(name, spec)}")
        rules.append(
            f'{_rule_name("kv", name)} ::= {_literal(json.dumps(name))} space ":" space {value}'
        )

    # Optional properties: any subset, in schema order. rest-X means
    # "what may follow property X": each later optional one may appear or not.
    for index, name in enumerate(optional[:-1]):
        following = optional[index + 1]
        tail = _rule_name("rest", following) if index + 1 < len(optional) - 1 else None
        parts = f'( "," space {_rule_name("kv", following)} )?'

        if tail:
            parts += f" {tail}"

        rules.append(f"{_rule_name('rest', name)} ::= {parts}")

    body = ' "," space '.join(_rule_name("kv", name) for name in required)

    if optional:
        starts = []

        for index, name in enumerate(optional):
            start = f'"," space {_rule_name("kv", name)}'

            if index < len(optional) - 1:
                start += f" {_rule_name('rest', name)}"

            starts.append(start)

        body += " (" + " | ".join(starts) + ")?"

    rules.append(f'root ::= "{{" space {body} "}}" space')

    return "\n".join(rules) + "\n"


# -- reading a reply that was not grammar-constrained --------------------------

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False

    return True


def extract_json_object(text: str) -> str:
    """The first JSON object inside free-form model text.

    Handles replies wrapped in prose or in a ```json fence. If no valid object
    is found the original text is returned unchanged, so the caller's own
    validation can report what was wrong.
    """
    cleaned = text.strip()
    fenced = _FENCE.match(cleaned)

    if fenced:
        cleaned = fenced.group(1)

    if cleaned.startswith("{") and _is_json(cleaned):
        return cleaned

    position = cleaned.find("{")

    while position != -1:
        depth = 0
        in_string = False
        escaped = False

        for index in range(position, len(cleaned)):
            char = cleaned[index]

            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1

                if depth == 0:
                    candidate = cleaned[position : index + 1]

                    if _is_json(candidate):
                        return candidate

                    break

        position = cleaned.find("{", position + 1)

    return text
