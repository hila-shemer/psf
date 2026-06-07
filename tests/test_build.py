import ast
import os

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
