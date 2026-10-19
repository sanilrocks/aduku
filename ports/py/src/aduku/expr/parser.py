"""Tokenizer and parser for the expression language (contracts/expression-language.md). Pure.

Produces an immutable tree. The expression length and the nesting depth are checked here, so a hostile expression is
refused before anything is evaluated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from aduku.expr.errors import ExprError
from aduku.expr.limits import Limits

MAX_TREE_HEIGHT = 200  # evaluation recurses once per level of the tree, so the tree is kept well under the stack

__all__ = [
    "Binary",
    "Call",
    "Cond",
    "Index",
    "ListLit",
    "Lit",
    "MapLit",
    "Member",
    "Method",
    "Name",
    "Node",
    "Unary",
    "parse",
]

INT_MIN, INT_MAX = -(2**63), 2**63 - 1


@dataclass(frozen=True)
class Lit:
    value: Any


@dataclass(frozen=True)
class Name:
    name: str


@dataclass(frozen=True)
class Unary:
    op: str
    operand: Node


@dataclass(frozen=True)
class Binary:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Cond:
    test: Node
    yes: Node
    no: Node


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Method:
    target: Node
    name: str
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Member:
    target: Node
    name: str


@dataclass(frozen=True)
class Index:
    target: Node
    index: Node


@dataclass(frozen=True)
class ListLit:
    items: tuple[Node, ...]


@dataclass(frozen=True)
class MapLit:
    pairs: tuple[tuple[str, Node], ...]


Node = Lit | Name | Unary | Binary | Cond | Call | Method | Member | Index | ListLit | MapLit

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<decimal>\d+\.\d+d|\d+d)
    | (?P<float>\d+\.\d+|\d+[eE][+-]?\d+|\d+\.)
    | (?P<int>\d+)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<string>"(?:[^"\\\n]|\\.)*")
    | (?P<op>==|!=|<=|>=|&&|\|\||[-+*/%()\[\]{},.:?!<>])
    )""",
    re.VERBOSE,
)
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "'": "'", "r": "\r", "/": "/"}
_KEYWORDS = {"true": True, "false": False, "null": None}


def _unescape(body: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        code = body[index + 1] if index + 1 < len(body) else ""
        if code == "u":
            digits = body[index + 2 : index + 6]
            if len(digits) != 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                raise ExprError("a \\u escape needs four hex digits")
            out.append(chr(int(digits, 16)))
            index += 6
        elif code in _ESCAPES:
            out.append(_ESCAPES[code])
            index += 2
        else:
            raise ExprError("unknown escape in a string")
    return "".join(out)


def _tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while True:
        if text[position:].strip() == "":
            return tokens
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise ExprError(
                f"unexpected character at position {position + len(text[position:]) - len(text[position:].lstrip())}"
            )
        kind = match.lastgroup or ""
        if kind == "float":
            raise ExprError("floats are not supported; write a decimal with the suffix d, for example 1.5d")
        tokens.append((kind, match.group(kind)))
        position = match.end()


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], max_depth: int) -> None:
        self.tokens = tokens
        self.at = 0
        self.depth = 0
        self.max_depth = max_depth

    def peek(self) -> tuple[str, str]:
        return self.tokens[self.at] if self.at < len(self.tokens) else ("end", "")

    def take(self) -> tuple[str, str]:
        token = self.peek()
        self.at += 1
        return token

    def accept(self, *ops: str) -> str | None:
        kind, value = self.peek()
        if kind == "op" and value in ops:
            self.at += 1
            return value
        if kind == "ident" and value == "in" and "in" in ops:
            self.at += 1
            return "in"
        return None

    def expect(self, op: str) -> None:
        if self.accept(op) is None:
            raise ExprError(f"expected {op!r}")

    def enter(self) -> None:
        self.depth += 1
        if self.depth > self.max_depth:
            raise ExprError(f"nesting depth exceeds the limit of {self.max_depth}")

    def leave(self) -> None:
        self.depth -= 1

    # expr := ternary
    def expression(self) -> Node:
        self.enter()
        try:
            test = self.logical_or()
            if self.accept("?"):
                yes = self.expression()
                self.expect(":")
                no = self.expression()
                return Cond(test, yes, no)
            return test
        finally:
            self.leave()

    def logical_or(self) -> Node:
        node = self.logical_and()
        while self.accept("||"):
            node = Binary("||", node, self.logical_and())
        return node

    def logical_and(self) -> Node:
        node = self.relation()
        while self.accept("&&"):
            node = Binary("&&", node, self.relation())
        return node

    def relation(self) -> Node:
        node = self.additive()
        while (op := self.accept("==", "!=", "<=", ">=", "<", ">", "in")) is not None:
            node = Binary(op, node, self.additive())
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while (op := self.accept("+", "-")) is not None:
            node = Binary(op, node, self.multiplicative())
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while (op := self.accept("*", "/", "%")) is not None:
            node = Binary(op, node, self.unary())
        return node

    def unary(self) -> Node:
        op = self.accept("!", "-")
        if op is None:
            return self.postfix()
        self.enter()  # a chain of prefix operators nests like parentheses do
        try:
            operand = self.unary()
        finally:
            self.leave()
        if (
            op == "-"
            and isinstance(operand, Lit)
            and isinstance(operand.value, int)
            and not isinstance(operand.value, bool)
        ):
            if INT_MIN <= -operand.value <= INT_MAX:
                return Lit(-operand.value)
        return Unary(op, operand)  # out of range: left for the evaluator, which raises "integer overflow"

    def postfix(self) -> Node:
        node = self.primary()
        while True:
            if self.accept("."):
                kind, name = self.take()
                if kind != "ident":
                    raise ExprError("expected a name after '.'")
                if self.accept("("):
                    node = Method(node, name, self.arguments(")"))
                else:
                    node = Member(node, name)
            elif self.accept("["):
                index = self.expression()
                self.expect("]")
                node = Index(node, index)
            else:
                return node

    def arguments(self, closer: str) -> tuple[Node, ...]:
        items: list[Node] = []
        if self.accept(closer):
            return ()
        while True:
            items.append(self.expression())
            if self.accept(closer):
                return tuple(items)
            self.expect(",")

    def primary(self) -> Node:
        kind, value = self.take()
        if kind == "int":
            number = int(value)
            if number > INT_MAX + 1:
                raise ExprError("an integer literal is over 64 bits")
            return Lit(number)
        if kind == "decimal":
            try:
                return Lit(Decimal(value[:-1]))
            except InvalidOperation:
                raise ExprError("bad decimal literal") from None
        if kind == "string":
            return Lit(_unescape(value[1:-1]))
        if kind == "ident":
            if value in _KEYWORDS:
                return Lit(_KEYWORDS[value])
            if value == "in":
                raise ExprError("'in' is an operator, not a name")
            if self.accept("("):
                return Call(value, self.arguments(")"))
            return Name(value)
        if kind == "op" and value == "(":
            inner = self.expression()
            self.expect(")")
            return inner
        if kind == "op" and value == "[":
            return ListLit(self.arguments("]"))
        if kind == "op" and value == "{":
            return self.map_literal()
        raise ExprError("unexpected end of expression" if kind == "end" else "unexpected token")

    def map_literal(self) -> Node:
        pairs: list[tuple[str, Node]] = []
        if self.accept("}"):
            return MapLit(())
        while True:
            kind, key = self.take()
            if kind != "string":
                raise ExprError("a map key must be a string literal")
            self.expect(":")
            pairs.append((_unescape(key[1:-1]), self.expression()))
            if self.accept("}"):
                return MapLit(tuple(pairs))
            self.expect(",")


def parse(text: str, limits: Limits) -> Node:
    """Parse one expression. Raises ExprError for bad syntax or an expression over the length or depth limit."""
    if len(text) > limits.expression_chars:
        raise ExprError(f"expression length {len(text)} is over the limit of {limits.expression_chars}")
    tokens = _tokens(text)
    if not tokens:
        raise ExprError("the expression is empty")
    parser = _Parser(tokens, limits.depth)
    node = parser.expression()
    if parser.at != len(tokens):
        raise ExprError("unexpected token after the end of the expression")
    if _height(node) > MAX_TREE_HEIGHT:
        raise ExprError(f"the expression chains more than {MAX_TREE_HEIGHT} operations deep")
    return node


def _children(node: Node) -> tuple[Node, ...]:
    if isinstance(node, Unary):
        return (node.operand,)
    if isinstance(node, Binary):
        return (node.left, node.right)
    if isinstance(node, Cond):
        return (node.test, node.yes, node.no)
    if isinstance(node, Call):
        return node.args
    if isinstance(node, ListLit):
        return node.items
    if isinstance(node, Method):
        return (node.target, *node.args)
    if isinstance(node, Member):
        return (node.target,)
    if isinstance(node, Index):
        return (node.target, node.index)
    if isinstance(node, MapLit):
        return tuple(value for _, value in node.pairs)
    return ()


def _height(root: Node) -> int:
    """The height of the tree, found without recursion: a long `a + a + ...` or `a.b.c...` chain nests to its length."""
    tallest = 0
    stack = [(root, 1)]
    while stack:
        node, level = stack.pop()
        tallest = max(tallest, level)
        if tallest > MAX_TREE_HEIGHT:
            break
        stack.extend((child, level + 1) for child in _children(node))
    return tallest
