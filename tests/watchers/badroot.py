"""Names a root that no config declares."""

from asp import Event, rule


@rule(name="badroot", on={"created"}, match="nowhere:**/*", output=None)
def badroot(event: Event, out):
    return None
