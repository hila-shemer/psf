# topfig Config Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `topfig` — a tested author-time `build.py` plus a self-contained static `index.html` that lets you twiddle `topf.py`'s knobs and edit its source, then downloads a freshly-mutated, always-valid `topf.py`.

**Architecture:** `build.py` (stdlib only) AST-parses the real `topf.py`, replaces each knob-controlled module constant's value with a `"""@@TOPFIG:<marker>@@"""` sentinel (capturing the original literal text as its default and parsing initial form values), then injects `{template, defaults, values, knobs}` as JSON into a committed `topfig_template.html` scaffold and writes `index.html`. The browser does dumb sentinel substitution — no runtime regex against Python, so output is always valid Python with zero stray markers.

**Tech Stack:** Python 3 stdlib (`ast`, `json`), pytest, vanilla HTML/CSS/JS (no external libs, no Node test runner).

**Note on artifacts:** The spec lists two committed artifacts (`build.py`, `index.html`). This plan adds a third committed source file, `topfig_template.html` — the static UI scaffold with one injection point — so the large vanilla-JS UI lives outside `build.py`'s tested logic and the data-injection stays trivially testable. `index.html` remains the generated deliverable.

---

## File Structure

- **`build.py`** (repo root, create) — author-time tool. Knob registry, `build_template`, `extract_values`, `render_html`, `main`. Pure stdlib, fully unit-tested.
- **`topfig_template.html`** (repo root, create) — static UI scaffold: CSS + the four knob forms + custom textarea + all client JS. Contains the single injection point `/*__TOPFIG_DATA__*/null`.
- **`index.html`** (repo root, generated + committed) — `topfig_template.html` with the JSON data blob injected. The deliverable.
- **`tests/test_build.py`** (create) — pytest coverage for the fragile Python-parsing core of `build.py`.

Imports work because the empty root `conftest.py` puts the repo root on `sys.path` (same mechanism that lets `tests/test_topf.py` do `import topf`).

The knob → constant → group registry (15 markers), used throughout:

| marker | topf.py constant | group |
|---|---|---|
| `interesting_names` | `DEFAULT_MATCHERS` | matchers |
| `cmd_width` | `CMD_WIDTH` | sizes |
| `collapse_threshold` | `COLLAPSE_THRESHOLD` | sizes |
| `group_pids` | `GROUP_PIDS` | sizes |
| `lifecycle_max` | `LIFECYCLE_MAX` | sizes |
| `dedup_min` | `DEDUP_MIN` | sizes |
| `repr_comms` | `REPR_COMMS` | sizes |
| `cpu_windows` | `DEFAULT_WINDOWS` | timing |
| `refresh_interval` | `REFRESH_INTERVAL` | timing |
| `sample_interval` | `SAMPLE_INTERVAL` | timing |
| `cache_ttl` | `CACHE_TTL` | timing |
| `rss_tint_anchors` | `RSS_TINT_ANCHORS` | colors |
| `cpu_tint_anchors` | `CPU_TINT_ANCHORS` | colors |
| `vmstat_outlier_anchors` | `VMSTAT_OUTLIER_ANCHORS` | colors |
| `tint_sgr` | `TINT_SGR` | colors |

---

## Task 1: `build_template` — sentinel substitution + round-trip

**Files:**
- Create: `build.py`
- Test: `tests/test_build.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/proj/topf && python -m pytest tests/test_build.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'build'`

- [ ] **Step 3: Write minimal implementation**

```python
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
    spans = []          # (start, end, marker), filled right-to-left later
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
        raise SystemExit("topf.py is missing knob constants: %s"
                         % ", ".join(sorted(missing)))
    out = src
    for start, end, marker in sorted(spans, reverse=True):
        out = out[:start] + '"""@@TOPFIG:%s@@"""' % marker + out[end:]
    return out, defaults
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/proj/topf && python -m pytest tests/test_build.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/proj/topf && git add build.py tests/test_build.py
git commit -m "feat(topfig): build_template sentinel substitution + round-trip"
```

---

## Task 2: `extract_values` — parse initial knob form state

**Files:**
- Modify: `build.py` (append helpers + `extract_values`)
- Test: `tests/test_build.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_build.py
def test_value_extraction():
    """Initial knob values parsed from source match current topf defaults."""
    _, defaults = build.build_template(SRC)
    values = build.extract_values(defaults)
    assert values["cmd_width"] == 50
    assert values["cache_ttl"] == 30
    names = {row["name"] for row in values["interesting_names"]}
    assert {"bazel", "sshd", "tmux", "claude"} <= names
    kinds = {row["kind"] for row in values["interesting_names"]}
    assert kinds <= {"comm", "cmdline"}
    assert values["cpu_windows"] == [2.0, 10.0, 60.0]
    assert values["tint_sgr"] == ["2", "2;33", "33", "1;31"]
    # arithmetic literal (100 * 1024**2) evaluates to a number
    assert values["rss_tint_anchors"][0] == 100 * 1024 ** 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/proj/topf && python -m pytest tests/test_build.py::test_value_extraction -v`
Expected: FAIL with `AttributeError: module 'build' has no attribute 'extract_values'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to build.py (after build_template)

def _eval_literal(text):
    """Evaluate a numeric / tuple / string literal expression with no names or
    builtins. Handles arithmetic such as ``100 * 1024 ** 2`` that
    ast.literal_eval rejects. Only ever fed our own trusted topf.py literals."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/proj/topf && python -m pytest tests/test_build.py::test_value_extraction -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/proj/topf && git add build.py tests/test_build.py
git commit -m "feat(topfig): extract_values parses initial knob form state"
```

---

## Task 3: `topfig_template.html` — the static UI scaffold

**Files:**
- Create: `topfig_template.html`

This task creates the full deliverable UI. It is data-driven off the injected
`TOPFIG` object, so it has no per-knob hardcoding beyond the render dispatch.

- [ ] **Step 1: Write the file**

Create `topfig_template.html` with exactly this content:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>topfig — configure topf.py</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.4 system-ui, sans-serif;
         background: #14161a; color: #e6e6e6; }
  header { display: flex; align-items: center; gap: 12px;
           padding: 10px 16px; background: #1d2026; border-bottom: 1px solid #2c2f36; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .spacer { flex: 1; }
  button { font: inherit; color: #e6e6e6; background: #2a6df4; border: 0;
           padding: 6px 14px; border-radius: 6px; cursor: pointer; }
  button.ghost { background: #2c2f36; }
  button.ghost.active { background: #3a5; }
  .wrap { display: flex; min-height: calc(100vh - 53px); }
  nav { width: 190px; padding: 12px; border-right: 1px solid #2c2f36; }
  nav button { display: block; width: 100%; text-align: left; margin-bottom: 6px;
               background: #20232a; }
  nav button.active { background: #2a6df4; }
  main { flex: 1; padding: 18px 22px; overflow: auto; }
  h2 { font-size: 14px; margin: 0 0 12px; color: #9aa4b2; text-transform: uppercase;
       letter-spacing: .05em; }
  label { display: block; margin: 10px 0 3px; color: #9aa4b2; font-size: 12px; }
  input, textarea, select { font: 13px/1.4 ui-monospace, monospace; color: #e6e6e6;
           background: #0e1013; border: 1px solid #2c2f36; border-radius: 5px;
           padding: 6px 8px; }
  input[type=number] { width: 120px; }
  input[type=text] { width: 260px; }
  table { border-collapse: collapse; margin-top: 6px; }
  td { padding: 3px 5px 3px 0; }
  #custom textarea { width: 100%; height: 70vh; white-space: pre; tab-size: 4; }
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
  .hint { color: #6b7280; font-size: 12px; margin: 4px 0 0; }
</style>
</head>
<body>
<header>
  <h1>topfig</h1>
  <span class="hint">a config tool whose output is your program back</span>
  <span class="spacer"></span>
  <button id="save">Save (download topf.py)</button>
</header>
<div class="wrap">
  <nav id="nav"></nav>
  <main id="main"></main>
</div>

<script>
const TOPFIG = /*__TOPFIG_DATA__*/null;

// ---- state ----------------------------------------------------------------
const D = TOPFIG;
let K = JSON.parse(JSON.stringify(D.values));   // working knob values
const dirty = new Set();                          // markers the user has touched
let preBuf = D.template;                           // Layer-0 text (markered)
let postBuf = null;                                // Layer-2 text (baked + edited)
let bakeKey = null;                                // hash(preBuf, K, dirty) at last bake
let mode = "matchers";                             // active left-nav tab
let customLayer = "pre";                           // pre | post

const GROUPS = [
  ["matchers", "Interesting names"],
  ["sizes",    "Sizes & limits"],
  ["timing",   "CPU windows & timing"],
  ["colors",   "Colors & thresholds"],
  ["custom",   "Custom (raw source)"],
];
const markersIn = (group) => D.knobs.filter(k => k.group === group).map(k => k.marker);

// ---- Python literal rendering --------------------------------------------
function pyStr(s) { return '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"'; }
function pyRaw(s) {                                 // raw-string regex literal
  const q = s.includes('"') && !s.includes("'") ? "'" : '"';
  return "r" + q + s.replace(new RegExp(q, "g"), "\\" + q) + q;
}
function pyFloat(n) { return Number.isInteger(n) ? n.toFixed(1) : String(n); }
function pyTupleFloat(a) { return "(" + a.map(pyFloat).join(", ") + ")"; }
function pyTupleInt(a) { return "(" + a.map(n => String(Math.round(n))).join(", ") + ")"; }
function pyTupleStr(a) { return "(" + a.map(pyStr).join(", ") + ")"; }
function renderMatchers(rows) {
  const body = rows.map(r =>
    "    (" + pyStr(r.name) + ", " + pyStr(r.kind) +
    ", re.compile(" + pyRaw(r.regex) + ")),").join("\n");
  return "[\n" + body + "\n]";
}
const RENDER = {
  interesting_names: renderMatchers,
  cmd_width: String, collapse_threshold: String, group_pids: String,
  lifecycle_max: String, dedup_min: String, repr_comms: String, cache_ttl: String,
  cpu_windows: pyTupleFloat, cpu_tint_anchors: pyTupleFloat,
  vmstat_outlier_anchors: pyTupleFloat,
  refresh_interval: pyFloat, sample_interval: pyFloat,
  rss_tint_anchors: pyTupleInt, tint_sgr: pyTupleStr,
};
function renderMarker(marker, value) { return RENDER[marker](value); }

// ---- pipeline -------------------------------------------------------------
function token(marker) { return '"""@@TOPFIG:' + marker + '@@"""'; }
function applyKnobs(text) {                         // Layer 0 + Layer 1
  let out = text;
  for (const k of D.knobs) {
    const rep = dirty.has(k.marker) ? renderMarker(k.marker, K[k.marker]) : D.defaults[k.marker];
    out = out.split(token(k.marker)).join(rep);
  }
  return out;
}
function backfill(text) {                            // strip any stray sentinels
  let out = text;
  for (const k of D.knobs) {
    out = out.split(token(k.marker)).join(D.defaults[k.marker]);
    out = out.split("@@TOPFIG:" + k.marker + "@@").join(D.defaults[k.marker]);
  }
  return out;
}
function keyOf() { return JSON.stringify([preBuf, K, [...dirty].sort()]); }
function bake() {
  const key = keyOf();
  if (postBuf === null || key !== bakeKey) { postBuf = applyKnobs(preBuf); bakeKey = key; }
}
function finalSource() {
  if (mode === "custom" && customLayer === "post") return backfill(postBuf);
  return applyKnobs(preBuf);
}
function markStale() { /* postBuf re-bakes on next flip because keyOf() changed */ }

// ---- DOM helpers ----------------------------------------------------------
const el = (tag, attrs = {}, kids = []) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "value") n.value = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of [].concat(kids)) n.append(kid);
  return n;
};
function setKnob(marker, value) { K[marker] = value; dirty.add(marker); markStale(); }
function parseFloatList(s) { return s.split(",").map(x => parseFloat(x.trim())).filter(x => !isNaN(x)); }
function parseStrList(s) { return s.split(",").map(x => x.trim()).filter(Boolean); }

// ---- knob forms -----------------------------------------------------------
function formMatchers() {
  const wrap = el("div");
  const table = el("table");
  function row(r, i) {
    return el("tr", {}, [
      el("td", {}, el("input", { type: "text", value: r.name,
        oninput: e => { r.name = e.target.value; setKnob("interesting_names", K.interesting_names); } })),
      el("td", {}, el("select", { onchange: e => { r.kind = e.target.value; setKnob("interesting_names", K.interesting_names); } },
        ["comm", "cmdline"].map(opt => el("option", opt === r.kind ? { value: opt, selected: "" } : { value: opt }, opt)))),
      el("td", {}, el("input", { type: "text", value: r.regex,
        oninput: e => { r.regex = e.target.value; setKnob("interesting_names", K.interesting_names); } })),
      el("td", {}, el("button", { class: "ghost", onclick: () => {
        K.interesting_names.splice(i, 1); setKnob("interesting_names", K.interesting_names); render(); } }, "✕")),
    ]);
  }
  K.interesting_names.forEach((r, i) => table.append(row(r, i)));
  wrap.append(el("h2", {}, "Interesting process names"),
    el("p", { class: "hint" }, "name · match comm or cmdline · Python regex"), table,
    el("button", { class: "ghost", onclick: () => {
      K.interesting_names.push({ name: "", kind: "comm", regex: "" });
      setKnob("interesting_names", K.interesting_names); render(); } }, "+ add matcher"));
  return wrap;
}
function intField(marker, caption) {
  return el("div", {}, [
    el("label", {}, caption + "  (" + D.knobs.find(k => k.marker === marker).const + ")"),
    el("input", { type: "number", value: K[marker],
      oninput: e => setKnob(marker, parseInt(e.target.value, 10)) }),
  ]);
}
function floatField(marker, caption) {
  return el("div", {}, [
    el("label", {}, caption),
    el("input", { type: "number", step: "0.1", value: K[marker],
      oninput: e => setKnob(marker, parseFloat(e.target.value)) }),
  ]);
}
function listField(marker, caption, parse, join) {
  return el("div", {}, [
    el("label", {}, caption),
    el("input", { type: "text", value: join(K[marker]),
      oninput: e => setKnob(marker, parse(e.target.value)) }),
  ]);
}
function formSizes() {
  return el("div", {}, [el("h2", {}, "Sizes & limits"),
    intField("cmd_width", "cmdline chars shown"),
    intField("collapse_threshold", "collapse threshold"),
    intField("group_pids", "pids listed per group"),
    intField("lifecycle_max", "lifecycle groups listed"),
    intField("dedup_min", "min siblings to merge"),
    intField("repr_comms", "comms named in summary")]);
}
function formTiming() {
  return el("div", {}, [el("h2", {}, "CPU windows & timing"),
    listField("cpu_windows", "CPU windows, seconds (comma-separated)", parseFloatList, a => a.join(", ")),
    floatField("refresh_interval", "refresh interval (s)"),
    floatField("sample_interval", "sample interval (s)"),
    intField("cache_ttl", "socket cache TTL (s)")]);
}
function formColors() {
  return el("div", {}, [el("h2", {}, "Colors & thresholds"),
    listField("rss_tint_anchors", "RSS tint anchors, bytes", s => parseFloatList(s), a => a.join(", ")),
    listField("cpu_tint_anchors", "CPU tint anchors, cores", parseFloatList, a => a.join(", ")),
    listField("vmstat_outlier_anchors", "vmstat outlier z-levels", parseFloatList, a => a.join(", ")),
    listField("tint_sgr", "SGR codes (dim, dim-yellow, yellow, bold-red)", parseStrList, a => a.join(", "))]);
}
function formCustom() {
  bake();
  const text = customLayer === "pre" ? preBuf : postBuf;
  const ta = el("textarea", { spellcheck: "false" });
  ta.value = text;
  ta.addEventListener("input", () => {
    if (customLayer === "pre") preBuf = ta.value; else postBuf = ta.value;
  });
  const flip = (layer) => () => {
    if (customLayer === "pre") preBuf = ta.value; else postBuf = ta.value;
    customLayer = layer; render();
  };
  return el("div", { id: "custom" }, [
    el("div", { class: "toolbar" }, [
      el("span", { class: "hint" }, "edit raw source ·"),
      el("button", { class: "ghost" + (customLayer === "pre" ? " active" : ""), onclick: flip("pre") }, "pre-knob"),
      el("button", { class: "ghost" + (customLayer === "post" ? " active" : ""), onclick: flip("post") }, "post-knob"),
    ]),
    el("p", { class: "hint" }, customLayer === "pre"
      ? "Layer 0: the markered template. Knobs re-apply on top at download."
      : "Layer 2: fully materialized. Your edits are final; knobs are frozen in."),
    ta,
  ]);
}
const FORMS = { matchers: formMatchers, sizes: formSizes, timing: formTiming, colors: formColors, custom: formCustom };

// ---- shell ----------------------------------------------------------------
function render() {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...GROUPS.map(([id, label]) =>
    el("button", { class: id === mode ? "active" : "", onclick: () => { mode = id; render(); } }, label)));
  document.getElementById("main").replaceChildren(FORMS[mode]());
}
function save() {
  const blob = new Blob([finalSource()], { type: "text/x-python" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "topf.py" });
  document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
}
document.getElementById("save").addEventListener("click", save);
render();
</script>
</body>
</html>
```

- [ ] **Step 2: Verify it is valid standalone HTML (renders, but inert without data)**

Run: `cd ~/proj/topf && python -c "import build, pathlib; t = pathlib.Path('topfig_template.html').read_text(); assert '/*__TOPFIG_DATA__*/null' in t; assert t.count('/*__TOPFIG_DATA__*/null') == 1; print('injection point OK')"`
Expected: `injection point OK`

- [ ] **Step 3: Commit**

```bash
cd ~/proj/topf && git add topfig_template.html
git commit -m "feat(topfig): static UI scaffold (forms, custom textarea, client JS)"
```

---

## Task 4: `render_html` + `main` — inject data, write index.html

**Files:**
- Modify: `build.py` (append `render_html`, `main`, `__main__` guard)
- Test: `tests/test_build.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_build.py
def test_render_html_smoke():
    """Generated HTML is non-empty, carries the template + all knob markers,
    and has the injection point consumed."""
    html = build.render_html(SRC)
    assert html.strip()
    assert "@@TOPFIG:interesting_names@@" in html      # template baked in
    for marker in build.CONST_TO_MARKER.values():
        assert marker in html                           # four knob groups + ids
    assert "/*__TOPFIG_DATA__*/null" not in html        # injection happened
    assert "</script>" not in html.split("const TOPFIG =")[1].split(";")[0]  # no break-out


def test_render_html_data_is_valid_json():
    """The injected blob parses as JSON and exposes the contract keys."""
    import json
    html = build.render_html(SRC)
    blob = html.split("const TOPFIG = ", 1)[1].split(";\n", 1)[0]
    data = json.loads(blob)
    assert set(data) == {"template", "defaults", "values", "knobs"}
    assert data["values"]["cmd_width"] == 50
    assert len(data["knobs"]) == len(build.KNOBS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/proj/topf && python -m pytest tests/test_build.py::test_render_html_smoke -v`
Expected: FAIL with `AttributeError: module 'build' has no attribute 'render_html'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to build.py (after extract_values)

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/proj/topf && python -m pytest tests/test_build.py -v`
Expected: PASS (all build tests green)

- [ ] **Step 5: Commit**

```bash
cd ~/proj/topf && git add build.py tests/test_build.py
git commit -m "feat(topfig): render_html injects data blob; main writes index.html"
```

---

## Task 5: Generate `index.html`, verify, and document

**Files:**
- Create (generated): `index.html`
- Modify: `docs/superpowers/specs/2026-06-07-topfig-config-editor-design.md` (status line)

- [ ] **Step 1: Generate the deliverable**

Run: `cd ~/proj/topf && python build.py`
Expected: `wrote /home/nadav/proj/topf/index.html (NNNNN bytes)`

- [ ] **Step 2: Run the whole suite (build + topf, nothing regressed)**

Run: `cd ~/proj/topf && python -m pytest -q`
Expected: all tests pass (existing `tests/test_topf.py` + new `tests/test_build.py`)

- [ ] **Step 3: Confirm generated output is self-contained and consistent**

Run: `cd ~/proj/topf && python -c "import pathlib,re; h=pathlib.Path('index.html').read_text(); assert '/*__TOPFIG_DATA__*/null' not in h; assert 'src=' not in h and 'cdn' not in h.lower(); print('self-contained, %d bytes' % len(h))"`
Expected: prints `self-contained, NNNNN bytes` (no external script/CDN references)

- [ ] **Step 4: Manual smoke (documented, no Node runner)**

Open `index.html` in a browser. Verify: the four knob tabs + Custom render; changing `cmd_width` then **Save** downloads a `topf.py` whose `CMD_WIDTH = <new value>`; an untouched config downloads a `topf.py` byte-identical to the source (diff it). Record the result in the commit message.

Run (automated proxy for the untouched-download invariant):
`cd ~/proj/topf && python -c "import build; src=open(build.TOPF,encoding='utf-8').read(); t,d=build.build_template(src); restored=t;\nimport functools\nfor m,x in d.items(): restored=restored.replace('\"\"\"@@TOPFIG:%s@@\"\"\"'%m, x)\nassert restored==src; print('untouched download == source: OK')"`
Expected: `untouched download == source: OK`

- [ ] **Step 5: Mark spec implemented**

Edit `docs/superpowers/specs/2026-06-07-topfig-config-editor-design.md` line 3 from
`**Status:** design approved (2026-06-07)` to
`**Status:** implemented (2026-06-07)`

- [ ] **Step 6: Commit**

```bash
cd ~/proj/topf && git add index.html docs/superpowers/specs/2026-06-07-topfig-config-editor-design.md
git commit -m "feat(topfig): generate index.html deliverable; mark spec implemented"
```

---

## Self-Review

**1. Spec coverage:**
- Magic-marker reframe (build-time templating, sentinels inside string literals) → Task 1.
- `build.py` stdlib-only, captures defaults, extracts knob values → Tasks 1, 2.
- Four knobs (matchers / sizes / timing / colors) over the 15 named constants → registry in Task 1, forms in Task 3.
- Client `render` per type (matcher block, ints, float tuples, string tuples) → `RENDER` map, Task 3.
- Dumb marker substitution with default backfill; always-valid output, zero stray sentinels → `applyKnobs`/`backfill`, Task 3; round-trip invariant tested Tasks 1 & 5.
- Layered source model + pre/post toggle with deterministic sticky bake (`bakeKey`) → `bake`/`keyOf`/`finalSource`, Task 3.
- Download via Blob + `<a download="topf.py">`, no server → `save`, Task 3.
- Self-contained `index.html`, no CDN/deps → Task 3 scaffold; verified Task 5 Step 3.
- Tests: marker coverage, round-trip, template validity, value extraction, smoke → Tasks 1, 2, 4.
- Layout (top bar Save + pre/post toggle, left nav tabs, main form/textarea) → Task 3.

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". All code blocks are complete and self-contained.

**3. Type consistency:** `build_template(src) -> (template, defaults)`, `extract_values(defaults) -> {marker: value}`, `render_html(src=None) -> str`, `CONST_TO_MARKER`/`KNOBS` used identically across tasks. Marker names match the registry table everywhere. Client `K[marker]`, `dirty`, `preBuf`/`postBuf`/`bakeKey`, `applyKnobs`/`backfill`/`bake`/`finalSource` names are consistent across the scaffold.

**Accepted trade-offs (per spec "Open risks"):**
- Template drift fails loudly via the `missing` check in `build_template` (raises `SystemExit`); round-trip test catches reshaped literals.
- `rss_tint_anchors` render emits plain ints (e.g. `104857600`), losing the original `100 * 1024**2` arithmetic form — only when the knob is touched; untouched downloads keep the original text via default backfill.
- `pyRaw` picks a quote that avoids escaping; pathological regexes (containing both quote types, or a trailing backslash) are out of scope, matching the spec's "best-effort regex knob."
```
