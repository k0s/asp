import pytest

from asp import rule
from asp.rules import rules_of


def test_rule_attaches_metadata_and_returns_the_function() -> None:
    def handler(event, out):
        return None

    decorated = rule(name="r", on={"created"}, match="**/*.txt")(handler)
    assert decorated is handler
    (r,) = rules_of(handler)
    assert (r.name, r.on, r.match, r.output, r.params) == (
        "r", frozenset({"created"}), "**/*.txt", None, {})
    assert r.handler is handler


def test_stacked_rules_accumulate_innermost_first() -> None:
    @rule(name="outer", on={"created"}, match="*", output="b/{name}", params={"n": 2})
    @rule(name="inner", on={"modified"}, match="*", output="a/{name}", params={"n": 1})
    def h(event, out, n):
        return None

    assert [r.name for r in rules_of(h)] == ["inner", "outer"]
    assert [r.params for r in rules_of(h)] == [{"n": 1}, {"n": 2}]


def test_rules_of_a_plain_object_is_empty() -> None:
    assert rules_of(len) == ()


@pytest.mark.parametrize("on", [set(), {"created", "renamed"}])
def test_bad_event_kinds_are_rejected(on) -> None:
    with pytest.raises(ValueError):
        rule(name="r", on=on, match="*")
