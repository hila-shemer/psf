# topfig — a static config editor that emits a topf.py

**Status:** design approved (2026-06-07)

## Summary

`topf.py` is customized today by editing its Python source. `topfig` is a
single static `index.html` that presents a friendly config panel, lets you
twiddle a handful of preprogrammed knobs *and* edit the raw script, and then
hands you back a freshly-mutated `topf.py` as a browser download. No server,
no filesystem access, no third-party deps — deployable as-is to Cloudflare
Pages/Workers free tier.

It is, deliberately, a config tool whose output is "here is your program
back." That is the point. The design below keeps the joke well-built:
deterministic, always-valid output, and a tested build step.

## Non-goals

- Not a "proper" config system for `topf.py` (no config file, no CLI flags
  added to topf). Customization == editing source, by design.
- No server runtime. The deliverable is a static file.
- No CDN / external JS libraries. The "editor" is a styled monospace
  `<textarea>`, not CodeMirror.
- Does not run `topf.py` or touch the user's filesystem beyond a download.

## Artifacts

1. **`build.py`** — author-time tool, Python stdlib only. Reads the real
   `topf.py`, replaces each knob-controlled literal with a magic marker while
   capturing the original text, extracts initial knob values, and emits the
   self-contained `index.html` with the template + defaults + initial knob
   state baked in. Re-run whenever `topf.py` changes.

2. **`index.html`** — the deliverable. Self-contained vanilla JS/CSS. This is
   what gets committed and deployed to Cloudflare. An HTML file that emits a
   Python script.

Both are committed to the repo.

## The magic-marker reframe

Rather than have the browser run fragile regex against arbitrary Python, the
build step **pre-templates** `topf.py`: every knob-controlled literal is
swapped for a sentinel held inside a Python string literal, so the template is
*itself* valid, importable Python (this matters for the round-trip test):

```python
DEFAULT_MATCHERS = """@@TOPFIG:interesting_names@@"""
CMD_WIDTH        = """@@TOPFIG:cmd_width@@"""
DEFAULT_WINDOWS  = """@@TOPFIG:cpu_windows@@"""
RSS_TINT_ANCHORS = """@@TOPFIG:rss_tint_anchors@@"""
...
```

Marker syntax: `@@TOPFIG:<name>@@`. One marker per knob-controlled constant.

The client's edit engine is then **dumb marker substitution**:

```
apply(K, text):
    for each marker @@TOPFIG:x@@ in text:
        replace with render(K[x])   if the knob has a value
        else replace with DEFAULTS[x]  (the captured original literal)
```

There is no "couldn't find target" failure mode, because the markers are
placed by the build step, not discovered at runtime. Any marker still present
at download time is backfilled with its default, so **the emitted file is
always valid, runnable Python with zero stray sentinels.**

## Layered source model

The downloaded file is the output of a 3-layer pipeline:

```
Layer 0  base template   ← topf.py with knob literals replaced by markers
Layer 1  knob transforms  ← the preprogrammed tools fill markers
Layer 2  manual edits      ← the giant textarea in custom mode
         ─────────────────
         = downloaded topf.py  (markers backfilled to defaults)
```

### Modes

- **Knob mode** — four dedicated forms (below). Each form prefills from the
  captured defaults / current knob state and updates `K` on change.
- **Custom mode** — a giant monospace `<textarea>` holding the whole script,
  with a `[ pre-knob | post-knob ]` toggle at the top:
  - **pre-knob** — the textarea holds the *markered template* (Layer 0). You
    see the seams; that is part of the charm. Knobs re-apply on top. Download
    = `apply(K, your_text)`.
  - **post-knob** — the textarea holds the *marker-substituted* result, fully
    materialized and editable. Knobs are frozen in; your edits are the final
    layer. Download = the textarea verbatim (with any stray markers
    backfilled). "Custom + post" == "save whatever is in this box."

### Sticky toggle semantics (deterministic)

State: `preBuf` (Layer-0 text), `postBuf` (Layer-2 text), `K` (knob values),
`bakeKey` = hash of `preBuf + K` at the last bake.

- Editing in pre mode updates `preBuf`. Editing a knob updates `K`. Either
  marks `postBuf` stale.
- **Flip pre→post:** if `hash(preBuf, K) == bakeKey`, keep the existing
  `postBuf` (manual post-edits survive). Otherwise re-bake
  `postBuf = apply(K, preBuf)` and refresh `bakeKey`.
- **Flip post→pre:** show `preBuf`; `postBuf` stays parked for a later flip
  back, subject to the same staleness rule.

Net effect: manual post-edits persist across flips **as long as you do not
change the base or the knobs underneath them**; once you do, the next flip
re-bakes and stale post-edits give way to fresh knob output. No 3-way merge.

## The four knobs

Each knob targets one or more markers. `build.py` captures the original
literal text for each (for defaults + the round-trip test) and parses it into
initial form values.

1. **Interesting process names** — `DEFAULT_MATCHERS`. Editable table of
   rows `{name, kind: comm|cmdline, regex}`; add/remove/edit; the knob
   regenerates the list literal. The marquee feature.
2. **Display sizes & limits** — scalar ints: `CMD_WIDTH`,
   `COLLAPSE_THRESHOLD`, `GROUP_PIDS`, `LIFECYCLE_MAX`, `DEDUP_MIN`,
   `REPR_COMMS`. Number inputs.
3. **CPU windows & timing** — `DEFAULT_WINDOWS` (float tuple),
   `REFRESH_INTERVAL`, `SAMPLE_INTERVAL`, `CACHE_TTL`.
4. **Colors & thresholds** — `RSS_TINT_ANCHORS`, `CPU_TINT_ANCHORS`,
   `VMSTAT_OUTLIER_ANCHORS`, `TINT_SGR`.

The raw custom-mode textarea covers everything not given a dedicated knob.

## Code generation (client `render`)

Each knob value renders back to a Python literal:

- matchers → a multi-line `[ (name, kind, re.compile(r"...")), ... ]` block
  matching topf's existing style.
- scalar ints → the integer.
- float tuples → `(a, b, c)`.
- string tuples (`TINT_SGR`) → `("2", "2;33", ...)`.

`render` output replaces the marker *and its surrounding `"""..."""`* so the
result is the bare literal, not a string. (Build step records the full
`"""@@TOPFIG:x@@"""` span as the replacement target.)

## Download

Client builds the final source = pipeline output with all remaining markers
backfilled to defaults, wraps it in a `Blob`, and triggers an
`<a download="topf.py">`. Server never involved.

## Layout

- Top bar: title, **Save (download)** button, and the pre/post toggle
  (visible in custom mode).
- Left: mode switch — the four knob tabs + "Custom".
- Main: the active knob form, or the custom textarea.
- Plain dependency-free CSS; monospace textarea.

## Testing

The fragile part — parsing real Python literals — lives in `build.py`, which
is pure Python and fully testable with `pytest`:

- **marker coverage:** every expected `@@TOPFIG:<name>@@` marker appears in the
  generated template exactly once.
- **round-trip:** substituting each captured default back into the template
  reproduces the original `topf.py` byte-for-byte.
- **template validity:** the markered template is importable / `ast.parse`-able
  Python (markers live inside string literals).
- **value extraction:** initial knob values parsed from the source match the
  known current defaults (e.g. `CMD_WIDTH == 50`, matcher names include
  `bazel`/`sshd`/`tmux`/`claude`).
- **smoke:** generated `index.html` is non-empty, contains the template
  string and the four knob identifiers.

Client-side substitution is trivial enough to need only a light check (a small
JS sanity assertion or a documented manual smoke step); no Node test runner is
introduced.

## Open risks / accepted trade-offs

- **Template drift:** if `topf.py`'s literals are reshaped so a marker can't be
  placed, `build.py` fails loudly (a test catches it). Acceptable — that is the
  signal to re-run the build.
- **Regex knob best-effort in pre mode:** if a user hand-edits the template and
  deletes a marker, that knob simply has nothing to fill; the default backfill
  still yields valid Python.
- **It is a code editor wearing a config-tool trench coat.** Known. Intended.
