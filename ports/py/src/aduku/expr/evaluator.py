"""Compile and run expressions (contracts/expression-language.md). Pure.

An expression sees only its parameters. The function library is the preset macros, so there is nothing to call that
reaches the clock, randomness, the network or the filesystem. Every evaluation is bounded by deterministic budgets
(steps and value sizes): the same inputs fault, or do not, on every machine.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from typing import Any

from aduku.behaviors.compute import DECIMAL_PRECISION, PRESETS, PresetError
from aduku.expr.errors import ExprError, ExprFault
from aduku.expr.limits import DEFAULTS, Limits
from aduku.expr.parser import (
    INT_MAX,
    INT_MIN,
    Binary,
    Call,
    Cond,
    Index,
    ListLit,
    Lit,
    MapLit,
    Member,
    Method,
    Name,
    Node,
    Unary,
    parse,
)
from aduku.ion_codec import FrozenStruct, IonError, to_canonical

__all__ = ["Compiled", "ExprError", "ExprFault", "FUNCTIONS", "compile_expression"]

# The only names that can be called: the presets, and two helpers.
FUNCTIONS = frozenset({*PRESETS, "size", "has"})
_RESERVED = FUNCTIONS | {"true", "false", "null", "in"}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | Decimal) and not isinstance(value, bool)


def _size(value: Any) -> int:
    """A cheap, deterministic estimate of the size of a value in bytes."""
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return 8
    if isinstance(value, Decimal):
        return 16
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, tuple):
        return 2 + sum(_size(item) for item in value)
    if isinstance(value, FrozenStruct):
        return 2 + sum(len(key.encode("utf-8")) + _size(item) for key, item in value.items())
    return 16


def _equal(left: Any, right: Any) -> bool:
    if _is_number(left) and _is_number(right):
        return bool(left == right)
    if left is None or right is None:
        return left is None and right is None
    if _is_number(left) or _is_number(right) or isinstance(left, bool) != isinstance(right, bool):
        return False
    try:
        return to_canonical(left) == to_canonical(right)
    except (TypeError, IonError):
        raise ExprFault("values cannot be compared") from None


@dataclass(frozen=True)
class Compiled:
    """A parsed and checked expression with its parameters and limits."""

    text: str
    params: tuple[str, ...]
    tree: Node
    limits: Limits

    def run(self, args: Sequence[Any]) -> Any:
        """The value of the expression for these arguments (None for a missing one). Raises ExprFault."""
        if len(args) != len(self.params):
            raise ExprFault(f"expected {len(self.params)} argument(s), got {len(args)}")
        if sum(_size(arg) for arg in args) > self.limits.input_bytes:
            raise ExprFault("input size limit exceeded")
        return _Run(self.limits, dict(zip(self.params, args, strict=True))).value(self.tree)


def compile_expression(text: str, params: Sequence[str], limits: Limits = DEFAULTS) -> Compiled:
    """Parse `text` and check that it uses only its parameters and the function library."""
    names = tuple(params)
    for name in names:
        if (
            not name
            or not (name[0].isalpha() or name[0] == "_")
            or not name.replace("_", "a").isalnum()
            or not name.isascii()
        ):
            raise ExprError("a parameter must be a plain name")
        if name in _RESERVED:
            raise ExprError("a parameter may not be named like a function or a literal")
    if len(set(names)) != len(names):
        raise ExprError("parameters must be distinct")
    tree = parse(text, limits)
    _check(tree, set(names))
    return Compiled(text, names, tree, limits)


def _check(node: Node, params: set[str]) -> None:
    if isinstance(node, Lit):
        if isinstance(node.value, int) and not isinstance(node.value, bool) and not INT_MIN <= node.value <= INT_MAX:
            raise ExprError("an integer literal is over 64 bits")
    elif isinstance(node, Name):
        if node.name not in params:
            raise ExprError("the expression uses a name that is not one of its parameters")
    elif isinstance(node, Unary):
        _check(node.operand, params)
    elif isinstance(node, Binary):
        _check(node.left, params)
        _check(node.right, params)
    elif isinstance(node, Cond):
        for part in (node.test, node.yes, node.no):
            _check(part, params)
    elif isinstance(node, Call):
        _check_call(node.name, len(node.args), node.args)
        for arg in node.args:
            _check(arg, params)
    elif isinstance(node, Method):
        _check_call(node.name, len(node.args) + 1, node.args)
        _check(node.target, params)
        for arg in node.args:
            _check(arg, params)
    elif isinstance(node, Member):
        _check(node.target, params)
    elif isinstance(node, Index):
        _check(node.target, params)
        _check(node.index, params)
    elif isinstance(node, ListLit):
        for item in node.items:
            _check(item, params)
    elif isinstance(node, MapLit):
        for _, value in node.pairs:
            _check(value, params)


def _check_call(name: str, count: int, args: Sequence[Node]) -> None:
    if name not in FUNCTIONS:
        raise ExprError("the expression calls a function that is not in the library")
    if name == "has":
        if count != 1 or not isinstance(args[0] if args else None, Member | Index):
            raise ExprError("has() takes one field access, such as has(m.key)")
    elif name == "size":
        if count != 1:
            raise ExprError("size() takes one argument")
    else:
        preset = PRESETS[name]
        if count < preset.fewest_args or (preset.most_args is not None and count > preset.most_args):
            raise ExprError(f"{name}() was given the wrong number of arguments")


class _Run:
    def __init__(self, limits: Limits, variables: dict[str, Any]) -> None:
        self.limits = limits
        self.variables = variables
        self.steps = 0
        self.built = 0

    def tick(self, amount: int = 1) -> None:
        self.steps += amount
        if self.steps > self.limits.steps:
            raise ExprFault("step budget exceeded")

    def made(self, value: Any) -> Any:
        self.built += _size(value)
        if self.built > self.limits.value_bytes:
            raise ExprFault("value size limit exceeded")
        return value

    def value(self, node: Node) -> Any:
        self.tick()
        handler = self._HANDLERS[type(node)]
        return handler(self, node)

    # -- nodes -------------------------------------------------------------------------------------------------

    def _lit(self, node: Lit) -> Any:
        return node.value

    def _name(self, node: Name) -> Any:
        return self.variables[node.name]

    def _unary(self, node: Unary) -> Any:
        operand = self.value(node.operand)
        if node.op == "!":
            if not isinstance(operand, bool):
                raise ExprFault("type error: ! takes a boolean")
            return not operand
        if not _is_number(operand):
            raise ExprFault("type error: unary - takes a number")
        return self._checked(-operand)

    def _cond(self, node: Cond) -> Any:
        test = self.value(node.test)
        if not isinstance(test, bool):
            raise ExprFault("type error: the condition must be a boolean")
        return self.value(node.yes if test else node.no)

    def _call(self, node: Call) -> Any:
        if node.name == "has":
            return self._has(node.args[0])
        return self._apply(node.name, [self.value(arg) for arg in node.args])

    def _method(self, node: Method) -> Any:
        return self._apply(node.name, [self.value(node.target), *(self.value(arg) for arg in node.args)])

    def _apply(self, name: str, args: list[Any]) -> Any:
        self.tick(sum(len(a) for a in args if isinstance(a, str | tuple | FrozenStruct)))
        if name == "size":
            name = "len"
        try:
            return self.made(PRESETS[name].call(args))
        except PresetError as error:
            raise ExprFault(str(error)) from None

    def _has(self, access: Node) -> Any:
        if isinstance(access, Member):
            target = self.value(access.target)
            if not isinstance(target, FrozenStruct):
                raise ExprFault("type error: has() needs a map")
            return access.name in target
        assert isinstance(access, Index)
        target, key = self.value(access.target), self.value(access.index)
        if isinstance(target, FrozenStruct) and isinstance(key, str):
            return key in target
        if isinstance(target, tuple) and isinstance(key, int) and not isinstance(key, bool):
            return 0 <= key < len(target)
        raise ExprFault("type error: has() needs a map with a string key or a list with an index")

    def _member(self, node: Member) -> Any:
        target = self.value(node.target)
        if not isinstance(target, FrozenStruct):
            raise ExprFault("type error: only a map has fields")
        if node.name not in target:
            raise ExprFault("no such key")
        return target[node.name]

    def _index(self, node: Index) -> Any:
        target, key = self.value(node.target), self.value(node.index)
        if isinstance(target, tuple) and isinstance(key, int) and not isinstance(key, bool):
            if not 0 <= key < len(target):
                raise ExprFault("index out of range")
            return target[key]
        if isinstance(target, FrozenStruct) and isinstance(key, str):
            if key not in target:
                raise ExprFault("no such key")
            return target[key]
        raise ExprFault("type error: index a list with a whole number or a map with a string")

    def _list(self, node: ListLit) -> Any:
        return self.made(tuple(self.value(item) for item in node.items))

    def _map(self, node: MapLit) -> Any:
        return self.made(FrozenStruct({key: self.value(item) for key, item in node.pairs}))

    def _binary(self, node: Binary) -> Any:
        op = node.op
        if op in ("&&", "||"):
            left = self.value(node.left)
            if not isinstance(left, bool):
                raise ExprFault(f"type error: {op} takes booleans")
            if (op == "&&" and not left) or (op == "||" and left):
                return left
            right = self.value(node.right)
            if not isinstance(right, bool):
                raise ExprFault(f"type error: {op} takes booleans")
            return right
        left, right = self.value(node.left), self.value(node.right)
        if op == "==":
            return _equal(left, right)
        if op == "!=":
            return not _equal(left, right)
        if op == "in":
            return self._contains(left, right)
        if op in ("<", "<=", ">", ">="):
            return self._compare(op, left, right)
        return self._arithmetic(op, left, right)

    def _contains(self, item: Any, container: Any) -> bool:
        if isinstance(container, tuple):
            return any(_equal(item, other) for other in container)
        if isinstance(container, FrozenStruct) and isinstance(item, str):
            return item in container
        raise ExprFault("type error: 'in' needs a list, or a map with a string key")

    def _compare(self, op: str, left: Any, right: Any) -> bool:
        if _is_number(left) and _is_number(right):
            pair = (left, right)
        elif isinstance(left, str) and isinstance(right, str):
            pair = (left, right)
        else:
            raise ExprFault("type error: cannot order values of different types")
        a, b = pair
        return bool({"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op])

    def _checked(self, number: Any) -> Any:
        if isinstance(number, int) and not INT_MIN <= number <= INT_MAX:
            raise ExprFault("integer overflow")
        return number

    def _arithmetic(self, op: str, left: Any, right: Any) -> Any:
        if op == "+":
            if isinstance(left, str) and isinstance(right, str):
                return self.made(left + right)
            if isinstance(left, tuple) and isinstance(right, tuple):
                return self.made(left + right)
        if not (_is_number(left) and _is_number(right)):
            raise ExprFault(f"type error: {op} does not take these types")
        try:
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                if op == "+":
                    return self._checked(left + right)
                if op == "-":
                    return self._checked(left - right)
                if op == "*":
                    return self._checked(left * right)
                if op == "/":
                    return self._divide(left, right)
                return self._remainder(left, right)
        except DecimalException:  # division by zero, an invalid operation, or a result beyond the decimal range
            raise ExprFault("division by zero or an invalid decimal operation") from None

    def _divide(self, left: Any, right: Any) -> Any:
        if right == 0:
            raise ExprFault("division by zero")
        if isinstance(left, int) and isinstance(right, int):
            quotient = abs(left) // abs(right)
            return self._checked(quotient if (left < 0) == (right < 0) else -quotient)
        return left / right

    def _remainder(self, left: Any, right: Any) -> Any:
        if not (isinstance(left, int) and isinstance(right, int)):
            raise ExprFault("type error: % takes whole numbers")
        if right == 0:
            raise ExprFault("division by zero")
        remainder = abs(left) % abs(right)
        return -remainder if left < 0 else remainder

    _HANDLERS: dict[type, Callable[[_Run, Any], Any]] = {
        Lit: _lit,
        Name: _name,
        Unary: _unary,
        Binary: _binary,
        Cond: _cond,
        Call: _call,
        Method: _method,
        Member: _member,
        Index: _index,
        ListLit: _list,
        MapLit: _map,
    }
