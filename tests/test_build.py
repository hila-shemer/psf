import ast
import os

import pytest

import build

SRC = open(build.TOPF, encoding="utf-8").read()


def test_marker_coverage():
    """Every expected marker appears exactly once; defaults cover them all."""
    template, defaults = build.build_template(SRC)
    for marker in build.CONST_TO_MARKER.values():
        assert template.count("@@TOPFIG:%s@@" % marker) == 1
    assert set(defaults) == set(build.CONST_TO_MARKER.values())


def test_round_trip():
    """Substituting each captured default back reproduces topf.py byte-for-byte."""
    template, defaults = build.build_template(SRC)
    restored = template
    for marker, text in defaults.items():
        restored = restored.replace('"""@@TOPFIG:%s@@"""' % marker, text)
    assert restored == SRC


def test_template_parses():
    """The markered template is still importable/ast-parseable Python."""
    template, _ = build.build_template(SRC)
    ast.parse(template)  # markers live inside string literals -> no SyntaxError


def test_missing_constant_raises():
    """A source lacking the knob constants fails loudly, not silently."""
    with pytest.raises(ValueError, match="missing knob constants"):
        build.build_template("X = 1\n")


def test_value_extraction():
    """Initial knob values parsed from source match current topf defaults."""
    _, defaults = build.build_template(SRC)
    values = build.extract_values(defaults)
    assert values["cmd_width"] == 50
    assert values["cache_ttl"] == 30
    names = {row["name"] for row in values["interesting_names"]}
    assert {"bazel", "sshd", "tmux", "claude"} <= names
    kinds = {row["kind"] for row in values["interesting_names"]}
    assert kinds == {"comm", "cmdline"}
    assert values["cpu_windows"] == [2.0, 10.0, 60.0]
    assert values["tint_sgr"] == ["2", "2;33", "33", "1;31"]
    # arithmetic literal (100 * 1024**2) evaluates to a number
    assert values["rss_tint_anchors"][0] == 100 * 1024 ** 2


def test_render_html_smoke():
    """Generated HTML is non-empty, carries the template + all knob markers,
    and has the injection point consumed."""
    html = build.render_html(SRC)
    assert html.strip()
    assert "@@TOPFIG:interesting_names@@" in html      # template baked in
    for marker in build.CONST_TO_MARKER.values():
        assert marker in html                           # four knob groups + ids
    assert "/*__TOPFIG_DATA__*/null" not in html        # injection happened


def test_render_html_data_is_valid_json():
    """The injected blob parses as JSON and exposes the contract keys."""
    import json
    html = build.render_html(SRC)
    blob = html.split("const TOPFIG = ", 1)[1].split(";\n", 1)[0]
    data = json.loads(blob)
    assert set(data) == {"template", "defaults", "values", "knobs"}
    assert data["values"]["cmd_width"] == 50
    assert len(data["knobs"]) == len(build.KNOBS)
