from pathlib import PurePosixPath as P

import pytest

from asp.patterns import PatternError, expand_braces, matches, render_output, split_root

ROOTS = {"in", "out"}


def test_split_root() -> None:
    assert split_root("in:**/*.txt", ROOTS) == ("in", "**/*.txt")
    assert split_root("**/*.txt", ROOTS) == (None, "**/*.txt")
    assert split_root("{dir}/thumbs/{name}", ROOTS) == (None, "{dir}/thumbs/{name}")
    assert split_root("**/a:b", ROOTS) == (None, "**/a:b")


def test_undeclared_root_is_an_error() -> None:
    with pytest.raises(PatternError, match="nowhere"):
        split_root("nowhere:**/*", ROOTS)


def test_expand_braces() -> None:
    assert expand_braces("**/*.{jpg,png,gif}") == ["**/*.jpg", "**/*.png", "**/*.gif"]
    assert expand_braces("{a,b}/{c,d}") == ["a/c", "a/d", "b/c", "b/d"]
    assert expand_braces("{dir}/x") == ["{dir}/x"]


@pytest.mark.parametrize("rel, expected", [
    ("a.txt", True), ("sub/b.txt", True), ("x/y/z.txt", True), ("c.md", False),
])
def test_doublestar_spans_zero_or_more_dirs(rel, expected) -> None:
    assert matches("**/*.txt", P(rel)) is expected


def test_matches_with_braces() -> None:
    assert matches("**/*.{jpg,png}", P("g/a.png"))
    assert not matches("**/*.{jpg,png}", P("g/a.gif"))


@pytest.mark.parametrize("template, rel, expected", [
    ("{dir}/{name}", "a.txt", "a.txt"),
    ("{dir}/{name}", "sub/b.txt", "sub/b.txt"),
    ("{dir}/thumbs/128/{name}", "g/a.jpg", "g/thumbs/128/a.jpg"),
    ("{dir}/thumbs/128/{name}", "a.jpg", "thumbs/128/a.jpg"),
])
def test_render_output(template, rel, expected) -> None:
    assert render_output(template, P(rel)) == P(expected)


@pytest.mark.parametrize("template", ["../{name}", "/abs/{name}", "{stem}.png", ""])
def test_render_output_rejects(template) -> None:
    with pytest.raises(PatternError):
        render_output(template, P("sub/b.txt"))
