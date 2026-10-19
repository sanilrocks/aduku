"""Errors of the expression language. Messages name a rule or a limit and never repeat a value."""

from __future__ import annotations

__all__ = ["ExprError", "ExprFault"]


class ExprError(ValueError):
    """An expression cannot be compiled: bad syntax, an unknown name, or it is over a static limit."""


class ExprFault(RuntimeError):
    """An expression failed while running: a type error, a bad index, a limit reached. Never repeats a value."""
