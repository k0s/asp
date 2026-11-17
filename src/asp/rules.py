"""Rules, the events they receive, and the decorator that declares them."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal, get_args

EventKind = Literal["created", "modified", "deleted", "moved"]
EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))

Handler = Callable[..., BinaryIO | None]

RULES_ATTR = "__asp_rules__"


@dataclass(frozen=True)
class Event:
    id: int                  # journal id; a per-pass counter until the journal exists
    ts: datetime
    kind: EventKind
    root: str                # the root it is under, by name
    rel: PurePosixPath       # its path relative to that root: the identity
    path: Path               # its absolute path on this host, for reading
    source: str              # "inotify" | "scan" | "handler:<name>"
    depth: int = 0           # chain depth; 0 unless a handler caused it
    size: int | None = None  # catalog facts, present when known
    mtime: float | None = None
    sha256: str | None = None
    mimetype: str | None = None


@dataclass(frozen=True, eq=False)
class Rule:
    name: str
    on: frozenset[str]
    match: str
    output: str | None
    params: dict[str, Any]
    handler: Handler


def rule(*, name: str, on: set[EventKind], match: str,
         output: str | None = None,
         params: dict[str, Any] | None = None) -> Callable[[Handler], Handler]:
    """Declare a rule on a handler. Stacked rules accumulate, innermost first."""
    if not on:
        raise ValueError(f"rule {name!r}: 'on' names no event kinds")
    unknown = set(on) - EVENT_KINDS
    if unknown:
        raise ValueError(f"rule {name!r}: unknown event kinds {sorted(unknown)}")

    def decorate(handler: Handler) -> Handler:
        declared = Rule(name=name, on=frozenset(on), match=match, output=output,
                        params=dict(params or {}), handler=handler)
        setattr(handler, RULES_ATTR, (*rules_of(handler), declared))
        return handler

    return decorate


def rules_of(obj: object) -> tuple[Rule, ...]:
    """The rules declared on obj, or () if it isn't a handler."""
    return getattr(obj, RULES_ATTR, ())
