"""Root-prefixed path patterns: matching inputs and rendering outputs."""

import re
from collections.abc import Collection
from pathlib import PurePosixPath

ROOT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_PREFIX = re.compile(rf"({ROOT_NAME.pattern}):(.*)", re.DOTALL)
_BRACES = re.compile(r"\{([^{}]*,[^{}]*)\}")


class PatternError(ValueError):
    """A pattern names an undeclared root, or renders outside its root."""


def split_root(pattern: str, roots: Collection[str]) -> tuple[str | None, str]:
    """('in', '**/*.txt') for 'in:**/*.txt'; (None, pattern) with no prefix."""
    m = _PREFIX.fullmatch(pattern)
    if m is None:
        return None, pattern
    name, rest = m.groups()
    if name not in roots:
        raise PatternError(f"{pattern!r} names undeclared root {name!r}")
    return name, rest


def expand_braces(glob: str) -> list[str]:
    """'*.{jpg,png}' -> ['*.jpg', '*.png']. Braces without a comma are literal."""
    m = _BRACES.search(glob)
    if m is None:
        return [glob]
    head, tail = glob[:m.start()], glob[m.end():]
    return [g for alt in m.group(1).split(",") for g in expand_braces(head + alt + tail)]


def matches(glob: str, rel: PurePosixPath) -> bool:
    """Whether a root-relative path matches a glob; '**' spans zero or more dirs."""
    return any(rel.full_match(g) for g in expand_braces(glob))


def render_output(template: str, rel: PurePosixPath) -> PurePosixPath:
    """Fill {dir} and {name} from an input's root-relative path."""
    try:
        rendered = template.format(dir=rel.parent.as_posix(), name=rel.name)
    except (KeyError, IndexError, ValueError) as exc:
        raise PatternError(f"{template!r}: {exc!r}") from exc
    parts = PurePosixPath(rendered).parts
    if not parts or parts[0] == "/" or ".." in parts:
        raise PatternError(f"{template!r} renders outside its root: {rendered!r}")
    return PurePosixPath(*parts)
