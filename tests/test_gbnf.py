import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from gbnf_check import Grammar  # noqa: E402  (test helper next to this file)

from app.backend.agents.reader import build_schema  # noqa: E402
from app.backend.ai.gbnf import extract_json_object, schema_to_gbnf  # noqa: E402


def action_grammar(chars=2400):
    return Grammar(schema_to_gbnf(build_schema(answer_only=False, answer_chars=chars)))


def final_grammar(chars=2400):
    return Grammar(schema_to_gbnf(build_schema(answer_only=True, answer_chars=chars)))


def small(**properties):
    return {"type": "object", "properties": properties, "required": list(properties)[:1]}


# --------------------------------------------------------- what it accepts

@pytest.mark.parametrize(
    "reply",
    [
        {"why": "see the layout", "action": "list_files"},
        {"why": "see the layout", "action": "list_files", "path": "."},
        {"why": "open it", "action": "read_file", "path": "src/app.py"},
        {"why": "next page", "action": "read_file", "path": "src/app.py", "start_line": 121},
        {"why": "find it", "action": "search_files", "query": "hello"},
        {"why": "narrow", "action": "search_files", "query": "hello", "path": "src"},
        {"why": "done", "action": "answer", "answer": "It prints **hello** (`app.py:4`)."},
        {"why": "", "action": "answer", "answer": ""},
        {"why": "all", "action": "read_file", "query": "q", "path": "a", "start_line": 1, "answer": "x"},
    ],
)
def test_every_valid_action_is_accepted(reply):
    assert action_grammar().matches(json.dumps(reply))


def test_compact_and_spaced_json_are_both_fine():
    grammar = action_grammar()

    assert grammar.matches('{"why":"x","action":"list_files"}')
    assert grammar.matches('{"why": "x", "action": "list_files"}')
    assert grammar.matches('{\n  "why": "x",\n  "action": "list_files"}')


def test_every_subset_of_the_optional_fields_works_in_order():
    grammar = action_grammar()
    optional = [("query", '"q"'), ("path", '"p"'), ("start_line", "7"), ("answer", '"a"')]

    for mask in range(1 << len(optional)):
        chosen = [item for bit, item in enumerate(optional) if mask >> bit & 1]
        text = '{"why": "w", "action": "read_file"' + "".join(f', "{k}": {v}' for k, v in chosen) + "}"

        assert grammar.matches(text), text


def test_strings_can_hold_unicode_and_escapes():
    grammar = action_grammar()

    for answer in ("\u0928\u092e\u0938\u094d\u0924\u0947 ORION", "caf\u00e9 \u2713", "a\\nb", 'say \\"hi\\"', "tab\\there", "\\u0041", "back\\\\slash"):
        assert grammar.matches('{"why": "w", "action": "answer", "answer": "' + answer + '"}'), answer


def test_integers():
    grammar = action_grammar()

    for number in ("0", "7", "120", "-1"):
        assert grammar.matches('{"why": "w", "action": "read_file", "start_line": ' + number + "}"), number


# --------------------------------------------------------- what it refuses

@pytest.mark.parametrize(
    "bad",
    [
        "",
        "{}",
        '{"why": "x"}',                                                    # action missing
        '{"action": "list_files"}',                                        # why missing
        '{"action": "list_files", "why": "x"}',                            # wrong order
        '{"why": "x", "action": "delete_everything"}',                     # not an allowed action
        '{"why": "x", "action": "list_files", "extra": "no"}',             # unknown key
        '{"why": "x", "action": "search_files", "path": "p", "query": "q"}',  # optional fields out of order
        '{"why": "x", "action": "list_files", "path": "a", "path": "b"}',  # repeated
        '{"why": "x", "action": "list_files"} trailing',
        'Sure! {"why": "x", "action": "list_files"}',                      # prose around it
        '```json\n{"why": "x", "action": "list_files"}\n```',              # a code fence
        '{"why": "x", "action": "list_files"',                             # unfinished
        '{"why": "unterminated, "action": "list_files"}',
        '{"why": "x", "action": "read_file", "start_line": "3"}',          # a string, not an integer
        '{"why": "x", "action": "read_file", "start_line": 3.5}',
        '{"why": "x", "action": "read_file", "start_line": 007}',
        '{"why": "line\nbreak", "action": "list_files"}',                  # raw newline inside a string
        '{"why": "say "hi"", "action": "list_files"}',                     # unescaped quote
        "{'why': 'x', 'action': 'list_files'}",                            # single quotes
    ],
)
def test_invalid_replies_are_refused(bad):
    assert not action_grammar().matches(bad)


def test_the_final_answer_grammar_demands_an_answer_and_no_tools():
    grammar = final_grammar()

    assert grammar.matches('{"why": "w", "action": "answer", "answer": "done"}')
    assert not grammar.matches('{"why": "w", "action": "answer"}')
    assert not grammar.matches('{"why": "w", "action": "list_files", "path": "."}')
    assert not grammar.matches('{"why": "w", "action": "answer", "answer": "x", "path": "."}')


def test_string_length_limits_are_enforced_exactly():
    grammar = Grammar(schema_to_gbnf({
        "type": "object",
        "properties": {"s": {"type": "string", "maxLength": 5}},
        "required": ["s"],
    }))

    assert grammar.matches('{"s": "abcde"}')
    assert not grammar.matches('{"s": "abcdef"}')
    assert grammar.matches('{"s": ""}')


def test_the_real_answer_limit_is_enforced_exactly():
    grammar = final_grammar(chars=2400)

    def reply(n):
        return '{"why": "w", "action": "answer", "answer": "' + "x" * n + '"}'

    assert grammar.matches(reply(2400))
    assert not grammar.matches(reply(2401))


def test_unlimited_strings_when_no_maximum_is_given():
    grammar = Grammar(schema_to_gbnf({"type": "object", "properties": {"s": {"type": "string"}}, "required": ["s"]}))

    assert grammar.matches('{"s": "' + "y" * 5000 + '"}')


# ------------------------------------------------ the grammar text itself

def test_the_rule_forms_match_what_llamacpp_itself_generates():
    """These lines are copied from the grammar your llama-server printed in its log."""
    text = schema_to_gbnf(build_schema(answer_only=False, answer_chars=2400)).splitlines()

    assert r'space ::= | " " | "\n"{1,2} [ \t]{0,20}' in text
    assert r'v-action ::= ("\"list_files\"" | "\"read_file\"" | "\"search_files\"" | "\"answer\"")' in text
    assert r'kv-action ::= "\"action\"" space ":" space v-action' in text
    assert r'v-why ::= "\"" char{0,200} "\""' in text
    assert r'v-answer ::= "\"" char{0,2400} "\""' in text
    assert r'v-path ::= "\"" char{0,300} "\""' in text
    assert r'v-query ::= "\"" char{0,200} "\""' in text
    assert r'kv-start-line ::= "\"start_line\"" space ":" space v-start-line' in text
    assert "v-start-line ::= integer" in text
    assert 'rest-query ::= ( "," space kv-path )? rest-path' in text
    assert 'rest-path ::= ( "," space kv-start-line )? rest-start-line' in text
    assert 'rest-start-line ::= ( "," space kv-answer )?' in text


def test_the_grammar_is_a_set_of_one_line_rules_with_a_root():
    text = schema_to_gbnf(build_schema(answer_only=False, answer_chars=2400))
    names = [line.split("::=")[0].strip() for line in text.splitlines()]

    assert "root" in names
    assert len(names) == len(set(names))                       # nothing defined twice
    assert all("::=" in line for line in text.splitlines())    # no stray lines
    assert text.endswith("\n")
    assert all(ord(c) < 128 for c in text)                     # plain ASCII for the server


def test_a_schema_with_one_property_and_no_optionals():
    grammar = Grammar(schema_to_gbnf({"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}))

    assert grammar.matches('{"n": 5}') and not grammar.matches("{}")


# ----------------------------------------------- shapes that are not supported

@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "object"},
        {"type": "object", "properties": {}},
        {"type": "object", "properties": {"a": {"type": "array"}}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"type": "object"}}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"enum": [1, 2]}}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"enum": []}}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["missing"]},
        {"type": "object", "properties": {"a": {"type": "string"}}},                           # nothing required
        {"type": "object", "properties": {"a": {"type": "string", "maxLength": -1}}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"type": "string", "maxLength": "5"}}, "required": ["a"]},
    ],
)
def test_unsupported_schemas_raise_value_error(schema):
    with pytest.raises(ValueError):
        schema_to_gbnf(schema)


# ------------------------------------------------- reading unconstrained replies

PAYLOAD = '{"why": "x", "action": "answer", "answer": "ok"}'


@pytest.mark.parametrize(
    "text",
    [
        PAYLOAD,
        "  \n" + PAYLOAD + "\n  ",
        "```json\n" + PAYLOAD + "\n```",
        "```\n" + PAYLOAD + "\n```",
        "```JSON " + PAYLOAD + " ```",
        "Sure! Here is my reply:\n" + PAYLOAD + "\nHope that helps.",
        "Sure!\n```json\n" + PAYLOAD + "\n```\nAnything else?",
        "Use {curly} braces like so: " + PAYLOAD,
        PAYLOAD + ' {"second": "object"}',
    ],
)
def test_the_json_object_is_found_inside_chatty_text(text):
    assert json.loads(extract_json_object(text)) == json.loads(PAYLOAD)


def test_braces_inside_strings_do_not_confuse_the_search():
    tricky = '{"why": "a } brace", "action": "answer", "answer": "{\\"nested\\": 1} \\\\ done"}'

    assert json.loads(extract_json_object("Here: " + tricky + " bye")) == json.loads(tricky)


def test_nested_objects_are_returned_whole():
    nested = '{"a": {"b": {"c": 1}}, "d": [1, 2]}'

    assert extract_json_object("x " + nested + " y") == nested


@pytest.mark.parametrize("text", ["", "no json here", "{broken", '{"a": }', "[1, 2, 3]", "} {"])
def test_without_a_valid_object_the_text_is_returned_unchanged(text):
    assert extract_json_object(text) == text
