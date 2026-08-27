from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


_TOKEN = re.compile(
    r"""
    \s*(?:
        (?P<number>-?\d+(?:\.\d+)?)
      | (?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
      | (?P<operator>==|!=|<=|>=|<|>|\(|\)|,|\.)
      | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
    )
    """,
    re.VERBOSE,
)
_TEMPLATE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def evaluate_expression(expression: str, context: dict[str, Any]) -> Any:
    tree = _Parser(_tokenize(expression)).parse()
    return _evaluate(tree, context)


def resolve_template(template: str, context: dict[str, Any]) -> Any:
    matches = list(_TEMPLATE.finditer(template))
    if not matches:
        return template
    if len(matches) == 1 and matches[0].span() == (0, len(template)):
        return evaluate_expression(matches[0].group(1), context)
    output: list[str] = []
    offset = 0
    for match in matches:
        output.append(template[offset : match.start()])
        value = evaluate_expression(match.group(1), context)
        if isinstance(value, (dict, list)):
            output.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
        elif value is None:
            output.append("null")
        elif isinstance(value, bool):
            output.append("true" if value else "false")
        else:
            output.append(str(value))
        offset = match.end()
    output.append(template[offset:])
    return "".join(output)


def resolve_input_mapping(mapping: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    paths = set(mapping)
    for path in paths:
        parts = path.split(".")
        if any(".".join(parts[:index]) in paths for index in range(1, len(parts))):
            raise ExpressionError(f"input mapping path conflicts at: {path}")
    resolved: dict[str, Any] = {}
    for path, template in mapping.items():
        parts = path.split(".")
        target = resolved
        for part in parts[:-1]:
            existing = target.get(part)
            if existing is None:
                target[part] = {}
            elif not isinstance(existing, dict):
                raise ExpressionError(f"input mapping path conflicts at: {part}")
            target = target[part]
        leaf = parts[-1]
        if leaf in target:
            raise ExpressionError(f"input mapping path conflicts at: {path}")
        target[leaf] = resolve_template(template, context)
    return resolved


def template_references(template: str) -> set[tuple[str, ...]]:
    references: set[tuple[str, ...]] = set()
    for match in _TEMPLATE.finditer(template):
        tree = _Parser(_tokenize(match.group(1))).parse()
        _collect_references(tree, references)
    unmatched = _TEMPLATE.sub("", template)
    if "${{" in unmatched or "}}" in unmatched:
        raise ExpressionError("unterminated template expression")
    return references


def _tokenize(expression: str) -> list[_Token]:
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("expression cannot be empty")
    expression = expression.strip()
    tokens: list[_Token] = []
    offset = 0
    while offset < len(expression):
        match = _TOKEN.match(expression, offset)
        if match is None:
            raise ExpressionError(f"unexpected token at position {offset}")
        kind = next(name for name, value in match.groupdict().items() if value is not None)
        tokens.append(_Token(kind, match.group(kind)))
        offset = match.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]):
        self.tokens = tokens
        self.index = 0

    def parse(self):
        value = self._or()
        if self.index != len(self.tokens):
            raise ExpressionError(f"unexpected token: {self.tokens[self.index].value}")
        return value

    def _or(self):
        value = self._and()
        while self._take_name("or"):
            value = ("binary", "or", value, self._and())
        return value

    def _and(self):
        value = self._not()
        while self._take_name("and"):
            value = ("binary", "and", value, self._not())
        return value

    def _not(self):
        if self._take_name("not"):
            return ("not", self._not())
        return self._comparison()

    def _comparison(self):
        value = self._primary()
        if self._peek_kind("operator") and self.tokens[self.index].value in {
            "==",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
        }:
            operator = self._take().value
            value = ("binary", operator, value, self._primary())
        return value

    def _primary(self):
        token = self._take()
        if token.kind == "operator" and token.value == "(":
            value = self._or()
            self._expect_operator(")")
            return value
        if token.kind == "string":
            try:
                return ("literal", ast.literal_eval(token.value))
            except (SyntaxError, ValueError) as exc:
                raise ExpressionError("invalid string literal") from exc
        if token.kind == "number":
            return ("literal", float(token.value) if "." in token.value else int(token.value))
        if token.kind != "name":
            raise ExpressionError(f"unexpected token: {token.value}")
        if token.value in {"true", "false", "null"}:
            return (
                "literal",
                {"true": True, "false": False, "null": None}[token.value],
            )
        if token.value in {"exists", "length"} and self._take_operator("("):
            argument = self._or()
            self._expect_operator(")")
            return ("call", token.value, argument)
        parts = [token.value]
        while self._take_operator("."):
            part = self._take()
            if part.kind != "name":
                raise ExpressionError("path segment must be an identifier")
            parts.append(part.value)
        if parts[0] not in {"inputs", "trigger", "nodes"}:
            raise ExpressionError(f"unknown expression root: {parts[0]}")
        if parts[0] == "nodes" and len(parts) < 2:
            raise ExpressionError("nodes path must include a node id")
        return ("path", tuple(parts))

    def _take(self) -> _Token:
        if self.index >= len(self.tokens):
            raise ExpressionError("unexpected end of expression")
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _peek_kind(self, kind: str) -> bool:
        return self.index < len(self.tokens) and self.tokens[self.index].kind == kind

    def _take_name(self, value: str) -> bool:
        if self._peek_kind("name") and self.tokens[self.index].value == value:
            self.index += 1
            return True
        return False

    def _take_operator(self, value: str) -> bool:
        if self._peek_kind("operator") and self.tokens[self.index].value == value:
            self.index += 1
            return True
        return False

    def _expect_operator(self, value: str) -> None:
        if not self._take_operator(value):
            raise ExpressionError(f"expected {value}")


def _evaluate(tree, context: dict[str, Any]) -> Any:
    kind = tree[0]
    if kind == "literal":
        return tree[1]
    if kind == "path":
        value: Any = context
        for part in tree[1]:
            if not isinstance(value, dict) or part not in value:
                raise ExpressionError(f"path does not exist: {'.'.join(tree[1])}")
            value = value[part]
        return value
    if kind == "call":
        if tree[1] == "exists":
            try:
                _evaluate(tree[2], context)
            except ExpressionError:
                return False
            return True
        value = _evaluate(tree[2], context)
        try:
            return len(value)
        except TypeError as exc:
            raise ExpressionError("length() requires a sized value") from exc
    if kind == "not":
        return not bool(_evaluate(tree[1], context))
    if kind == "binary":
        operator = tree[1]
        left = _evaluate(tree[2], context)
        if operator == "and":
            return bool(left) and bool(_evaluate(tree[3], context))
        if operator == "or":
            return bool(left) or bool(_evaluate(tree[3], context))
        right = _evaluate(tree[3], context)
        try:
            return {
                "==": lambda: left == right,
                "!=": lambda: left != right,
                "<": lambda: left < right,
                "<=": lambda: left <= right,
                ">": lambda: left > right,
                ">=": lambda: left >= right,
            }[operator]()
        except TypeError as exc:
            raise ExpressionError(f"values cannot be compared with {operator}") from exc
    raise ExpressionError("unsupported expression")


def _collect_references(tree, references: set[tuple[str, ...]]) -> None:
    if tree[0] == "path":
        references.add(tree[1])
        return
    if tree[0] in {"not", "call"}:
        _collect_references(tree[2] if tree[0] == "call" else tree[1], references)
    elif tree[0] == "binary":
        _collect_references(tree[2], references)
        _collect_references(tree[3], references)
