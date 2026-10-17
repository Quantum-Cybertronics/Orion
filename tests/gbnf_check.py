r"""A tiny GBNF interpreter, just big enough to test ORION's generated grammars.

Supports what ORION emits: rule references, "string" literals, [char classes]
(negation, ranges, \xHH and simple escapes), grouping, alternation (including
an empty alternative), and the postfix operators ? * + {m} {m,} {m,n}.

``Grammar(text).matches(string)`` is True when the WHOLE string matches ``root``.
It tracks sets of reachable positions, so it cannot blow up on repetition.
"""

import re

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "]": "]", "[": "[", "^": "^", "-": "-"}


class GrammarError(Exception):
    pass


def _read_escape(text, i):
    """text[i] is the character after a backslash. Returns (char, next index)."""
    c = text[i]

    if c == "x":
        return chr(int(text[i + 1 : i + 3], 16)), i + 3

    if c in _ESCAPES:
        return _ESCAPES[c], i + 1

    raise GrammarError(f"unknown escape \\{c}")


class _Parser:
    def __init__(self, text):
        self.s = text
        self.i = 0

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_spaces(self):
        while self.peek() in (" ", "\t"):
            self.i += 1

    def alternation(self):
        branches = [self.sequence()]

        while True:
            self.skip_spaces()

            if self.peek() == "|":
                self.i += 1
                branches.append(self.sequence())
            else:
                return ("alt", branches)

    def sequence(self):
        items = []

        while True:
            self.skip_spaces()
            c = self.peek()

            if c in ("", "|", ")"):
                return ("seq", items)

            items.append(self.postfix(self.atom()))

    def atom(self):
        c = self.peek()

        if c == '"':
            self.i += 1
            chars = []

            while self.peek() != '"':
                if self.peek() == "":
                    raise GrammarError("unterminated string literal")

                if self.peek() == "\\":
                    ch, self.i = _read_escape(self.s, self.i + 1)
                    chars.append(ch)
                else:
                    chars.append(self.peek())
                    self.i += 1

            self.i += 1
            return ("lit", "".join(chars))

        if c == "[":
            return self.char_class()

        if c == "(":
            self.i += 1
            inner = self.alternation()
            self.skip_spaces()

            if self.peek() != ")":
                raise GrammarError("missing )")

            self.i += 1
            return inner

        m = re.compile(r"[a-z][a-z0-9-]*").match(self.s, self.i)

        if not m:
            raise GrammarError(f"unexpected {c!r} at {self.i}")

        self.i = m.end()
        return ("ref", m.group(0))

    def char_class(self):
        self.i += 1
        negate = self.peek() == "^"

        if negate:
            self.i += 1

        ranges = []

        while self.peek() != "]":
            if self.peek() == "":
                raise GrammarError("unterminated [")

            lo = self.class_char()

            if self.peek() == "-" and self.s[self.i + 1] != "]":
                self.i += 1
                hi = self.class_char()
            else:
                hi = lo

            ranges.append((lo, hi))

        self.i += 1
        return ("class", negate, ranges)

    def class_char(self):
        if self.peek() == "\\":
            ch, self.i = _read_escape(self.s, self.i + 1)
            return ch

        ch = self.peek()
        self.i += 1
        return ch

    def postfix(self, node):
        c = self.peek()

        if c == "?":
            self.i += 1
            return ("rep", node, 0, 1)

        if c == "*":
            self.i += 1
            return ("rep", node, 0, None)

        if c == "+":
            self.i += 1
            return ("rep", node, 1, None)

        if c == "{":
            m = re.compile(r"\{(\d*)(,?)(\d*)\}").match(self.s, self.i)

            if not m:
                raise GrammarError("bad {m,n}")

            self.i = m.end()
            low = int(m.group(1) or 0)
            high = (int(m.group(3)) if m.group(3) else None) if m.group(2) else low

            return ("rep", node, low, high)

        return node


class Grammar:
    def __init__(self, text):
        self.rules = {}

        for line in text.strip().splitlines():
            if not line.strip():
                continue

            name, sep, rhs = line.partition("::=")

            if not sep:
                raise GrammarError(f"not a rule: {line!r}")

            name = name.strip()

            if name in self.rules:
                raise GrammarError(f"rule defined twice: {name}")

            parser = _Parser(rhs)
            self.rules[name] = parser.alternation()
            parser.skip_spaces()

            if parser.peek() != "":
                raise GrammarError(f"cannot parse rule {name!r} near {rhs[parser.i:]!r}")

        if "root" not in self.rules:
            raise GrammarError("no root rule")

        self._check_references()

    def _check_references(self):
        def walk(node):
            kind = node[0]

            if kind == "ref" and node[1] not in self.rules:
                raise GrammarError(f"undefined rule: {node[1]}")

            if kind == "alt":
                for branch in node[1]:
                    walk(branch)
            elif kind == "seq":
                for item in node[1]:
                    walk(item)
            elif kind == "rep":
                walk(node[1])

        for node in self.rules.values():
            walk(node)

    def _ends(self, node, text, starts):
        """Positions reachable after matching ``node`` from any start position."""
        kind = node[0]

        if kind == "lit":
            literal = node[1]
            return {p + len(literal) for p in starts if text.startswith(literal, p)}

        if kind == "class":
            _, negate, ranges = node
            out = set()

            for p in starts:
                if p < len(text):
                    inside = any(lo <= text[p] <= hi for lo, hi in ranges)

                    if inside != negate:
                        out.add(p + 1)

            return out

        if kind == "ref":
            return self._ends(self.rules[node[1]], text, starts)

        if kind == "alt":
            out = set()

            for branch in node[1]:
                out |= self._ends(branch, text, starts)

            return out

        if kind == "seq":
            current = set(starts)

            for item in node[1]:
                current = self._ends(item, text, current)

                if not current:
                    return set()

            return current

        _, inner, low, high = node
        current = set(starts)
        reached = set(current) if low == 0 else set()
        count = 0

        while current and (high is None or count < high):
            current = self._ends(inner, text, current)
            count += 1

            if count >= low:
                new = current - reached
                reached |= current

                if not new and high is None:
                    break

                current = new if high is None else current

        return reached

    def matches(self, text):
        return len(text) in self._ends(self.rules["root"], text, {0})
