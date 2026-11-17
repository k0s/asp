"""Reuses the rule name ``upper`` from ``watchers.upper``."""

from asp import Event, rule


@rule(name="upper", on={"created"}, match="in:**/*.md", output="out:{dir}/{name}")
def upper_again(event: Event, out):
    return None
