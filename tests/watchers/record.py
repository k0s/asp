"""A side-effect rule with no output: records what it saw in SEEN."""

from asp import Event, rule

SEEN: list[tuple[str, str, object]] = []


@rule(name="record", on={"created"}, match="**/*.txt", output=None)
def record(event: Event, out):
    SEEN.append((event.root, event.rel.as_posix(), out))
    return None
