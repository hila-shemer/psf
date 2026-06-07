#!/usr/bin/env python3
"""topfig build step: turn topf.py into a self-contained index.html config editor.

Reads the real topf.py, replaces each knob-controlled module constant's value
with a triple-quoted ``@@TOPFIG:<marker>@@`` sentinel (capturing the original
literal text as the default), parses initial knob values, and injects
template+defaults+values as JSON into topfig_template.html, writing index.html.
Re-run whenever topf.py's knob literals change.
"""
import ast
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TOPF = os.path.join(HERE, "topf.py")
TEMPLATE_HTML = os.path.join(HERE, "topfig_template.html")
OUT_HTML = os.path.join(HERE, "index.html")

# marker -> (constant, group). One marker per knob-controlled module constant.
KNOBS = [
    ("interesting_names",      "DEFAULT_MATCHERS",       "matchers"),
    ("cmd_width",              "CMD_WIDTH",              "sizes"),
    ("collapse_threshold",     "COLLAPSE_THRESHOLD",     "sizes"),
    ("group_pids",             "GROUP_PIDS",             "sizes"),
    ("lifecycle_max",          "LIFECYCLE_MAX",          "sizes"),
    ("dedup_min",              "DEDUP_MIN",              "sizes"),
    ("repr_comms",             "REPR_COMMS",             "sizes"),
    ("cpu_windows",            "DEFAULT_WINDOWS",        "timing"),
    ("refresh_interval",       "REFRESH_INTERVAL",       "timing"),
    ("sample_interval",        "SAMPLE_INTERVAL",        "timing"),
    ("cache_ttl",              "CACHE_TTL",              "timing"),
    ("rss_tint_anchors",       "RSS_TINT_ANCHORS",       "colors"),
    ("cpu_tint_anchors",       "CPU_TINT_ANCHORS",       "colors"),
    ("vmstat_outlier_anchors", "VMSTAT_OUTLIER_ANCHORS", "colors"),
    ("tint_sgr",               "TINT_SGR",               "colors"),
]
CONST_TO_MARKER = {const: marker for marker, const, _ in KNOBS}


def _line_starts(src):
    """Absolute char offset of the first char of each 1-based source line."""
    starts, idx = [0], 0
    for line in src.splitlines(keepends=True):
        idx += len(line)
        starts.append(idx)
    return starts


def _span(node, starts):
    """(start, end) absolute char offsets of an ast node's source text."""
    start = starts[node.lineno - 1] + node.col_offset
    end = starts[node.end_lineno - 1] + node.end_col_offset
    return start, end


def build_template(src):
    """Return (template, defaults).

    template: topf.py with each knob constant's *value* replaced by a
    ``\"\"\"@@TOPFIG:<marker>@@\"\"\"`` sentinel (valid Python: the marker lives
    inside a string literal). defaults: {marker: original_literal_text}.
    """
    tree = ast.parse(src)
    starts = _line_starts(src)
    spans = []          # (start, end, marker); sorted right-to-left at substitution time
    defaults = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        marker = CONST_TO_MARKER.get(target.id)
        if marker is None:
            continue
        start, end = _span(node.value, starts)
        defaults[marker] = src[start:end]
        spans.append((start, end, marker))
    missing = set(CONST_TO_MARKER.values()) - set(defaults)
    if missing:
        raise ValueError("topf.py is missing knob constants: %s"
                         % ", ".join(sorted(missing)))
    out = src
    for start, end, marker in sorted(spans, reverse=True):
        out = out[:start] + '"""@@TOPFIG:%s@@"""' % marker + out[end:]
    return out, defaults


def _eval_literal(text):
    """Evaluate a numeric / tuple / string literal expression with no free names
    (empty builtins). Handles arithmetic such as ``100 * 1024 ** 2`` that
    ast.literal_eval rejects. Not a security sandbox; only ever fed our own
    trusted topf.py literals."""
    code = compile(ast.parse(text, mode="eval"), "<knob>", "eval")
    return eval(code, {"__builtins__": {}}, {})  # noqa: S307 (trusted input)


def _to_jsonable(value):
    """Tuples -> lists, recursively, so values serialize and compare cleanly."""
    if isinstance(value, (tuple, list)):
        return [_to_jsonable(item) for item in value]
    return value


def _parse_matchers(text):
    """Parse the DEFAULT_MATCHERS list literal into [{name, kind, regex}]."""
    tree = ast.parse(text, mode="eval")
    rows = []
    for elt in tree.body.elts:                 # each elt: (label, target, Call)
        label, target, call = elt.elts
        pattern = call.args[0].value           # re.compile(r"...").args[0]
        rows.append({"name": label.value, "kind": target.value, "regex": pattern})
    return rows


def extract_values(defaults):
    """{marker: parsed initial form value} from the captured default literals."""
    values = {}
    for marker, text in defaults.items():
        if marker == "interesting_names":
            values[marker] = _parse_matchers(text)
        else:
            values[marker] = _to_jsonable(_eval_literal(text))
    return values


def render_html(src=None):
    """Inject {template, defaults, values, knobs} as JSON into the UI scaffold
    and return the self-contained index.html text."""
    if src is None:
        with open(TOPF, encoding="utf-8") as f:
            src = f.read()
    template, defaults = build_template(src)
    data = {
        "template": template,
        "defaults": defaults,
        "values": extract_values(defaults),
        "knobs": [{"marker": m, "const": c, "group": g} for m, c, g in KNOBS],
    }
    with open(TEMPLATE_HTML, encoding="utf-8") as f:
        scaffold = f.read()
    blob = json.dumps(data).replace("</", "<\\/")    # never break out of <script>
    return scaffold.replace("/*__TOPFIG_DATA__*/null", blob, 1)


def main():
    html = render_html()
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print("wrote %s (%d bytes)" % (OUT_HTML, len(html)))


if __name__ == "__main__":
    main()
