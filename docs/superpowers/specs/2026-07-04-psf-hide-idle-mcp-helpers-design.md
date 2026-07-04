# psf: hide idle context7-mcp helper chains

## Problem

Every `claude` session spawns the same three-proc MCP helper chain:

```
[usr] claude …
   └─ [mis] npm exec @upstash/context7-mcp
      └─ [mis] sh -c context7-mcp
         └─ [cmp] node /home/hila/.npm/_npx/…/node_mo…
```

These ride along as *kept* descendants of an interesting `claude` (which matches
the `user-session` matcher, so its whole subtree is kept). They are almost always
idle — sub-1% CPU, well under 100 MB — yet they triple the line count of every
claude in the forest. `collapse()` doesn't fold them because a claude's subtree is
small (below the descendant threshold).

We want them hidden **unless one is actually doing something**, judged by CPU or
memory.

## What gets hidden — the "unit"

A *helper unit* is the shallowest process whose cmdline matches a helper marker
(`context7-mcp` / `@upstash/context7-mcp`), together with its entire kept subtree.
Anchoring on the shallowest match and taking the whole subtree matters because the
`node …` leaf's cmdline is truncated and does **not** literally contain
"context7" — matching each proc independently would miss it.

**Eligibility guard ("fits its expected place in the claude sub-tree"):** a unit is
only eligible for hiding if its **nearest visible ancestor is classified
`user-session`** (i.e. a claude). A `context7-mcp` launched standalone to debug —
with no claude parent — is left fully visible.

## The threshold — "interesting" means show

Each node in the unit is **hot** if any of:

- current CPU  > 2%   (`HELPER_CUR_CPU = 0.02`)
- average CPU  > 5%   (`HELPER_AVG_CPU = 0.05`)
- resident set > 200 MB (`HELPER_RSS = 200 * 1024**2`)

CPU thresholds are in *cores* fraction, matching the existing `CPU_TINT_ANCHORS`
convention (1.0 == one full core == 100%). Average CPU and RSS come from the
initial `scan()`; **current CPU comes from the sample window**, so helper procs
must remain in the `printed` set that gets sampled and probed — the hide decision
runs *after* sampling, not at `collapse()` time. If `--sample-interval 0` disables
the current-CPU sample, `cpu_current` is `None` and treated as 0 (only avg/rss can
trip).

Outcomes:

- **Whole unit cold** — every node below every threshold → hide the entire unit;
  emit a dim trailing note under the claude:
  `… (context7-mcp helper hidden)`.
- **Some node hot** — hide the cold nodes; **lift** each hot node so it renders as
  a direct child of the claude, prefixed `↑ `, and emit
  `… (context7-mcp helper: N idle hidden)` (N = cold nodes hidden).

## Mechanism

The view has no dangling nodes: `build_view` reaches a node only by walking down
through its parent's visible children. Suppressing a node with the existing
`suppressed` set therefore *cuts the path* to everything beneath it — which would
make a lifted hot node vanish, not dangle. So hiding-with-lift needs a
**pass-through** concept, distinct from `suppressed`.

### New post-sampling planner

`plan_helper_hiding(roots, categories, sysinfo)` walks the kept forest, finds each
eligible unit, classifies each member hot/cold, and returns:

- `transparent`: `set[pid]` — cold members. **Skipped in the walk but passed
  through**: a transparent node contributes its own visible descendants in its place
  rather than cutting them.
- `lifted`: `set[pid]` — hot members that surface through a transparent parent;
  they get the `↑ ` marker.
- `notes`: `dict[anchor_pid, list[str]]` — the trailing note(s), keyed by the
  nearest-visible-ancestor pid (the claude).

A fully-cold unit puts *all* its members in `transparent` (nothing surfaces). A
partial unit puts cold members in `transparent` and hot members in `lifted`.

### `_visible_children` gains pass-through

```python
def _visible_children(node, suppressed, transparent=frozenset()):
    out = []
    for c in node.children:
        if not c.kept:
            continue
        if c.pid in transparent:
            out.extend(_visible_children(c, suppressed, transparent))
            continue
        if c.pid in suppressed:
            continue
        out.append(c)
    return out
```

`transparent` defaults empty, so **`render()` and every existing test are
byte-for-byte unchanged**. `build_view` threads `transparent` / `lifted` / `notes`
through its internal walks (`make_nodes`, `_session_procs`, `session_signature`).
`node_of` prefixes `↑ ` when `item.pid in lifted`. `emit_lines` renders the note as
a trailing dim line under the anchor, exactly like the existing `collapse_note`
line (add a `helper_note` field to `ViewNode`, or reuse the collapse-note render
path).

### Interactions

- `--expand-all` passes empty `transparent`/`lifted`/`notes` (like it already zeroes
  `suppressed`) → full chains reappear.
- `psf tree` and the default `session` axis both hide helpers (tree is
  "less-collapsed" but the user still wants the idle helpers gone).
- `--path` / flat axis is **untouched** — it answers "what touches this path" and
  must not hide anything.
- `dir` / `consolidated` axes: cold members are transparent there too (pass-through
  keeps any hot node visible); the trailing note is a tree-axis nicety and need not
  render in the flat `dir`/pooled paths.

## Config knobs (module level, config section)

```python
HELPER_MARKERS = (re.compile(r"context7-mcp"),)   # cmdline; shallowest match roots a unit
HELPER_CUR_CPU = 0.02
HELPER_AVG_CPU = 0.05
HELPER_RSS     = 200 * 1024**2
```

The helper name shown in the note derives from the marker (e.g. `context7-mcp`).

## Testing

- **Planner** unit tests on a synthetic `claude → npm → sh → node` `Proc` tree with
  set `utime`/`stime`/`rss_pages`/`cpu_current`:
  - all-cold → whole unit `transparent`, `notes` has "context7-mcp helper hidden".
  - `node` hot (rss > 200 MB, or cur > 2%, or avg > 5%) → npm/sh `transparent`,
    node in `lifted`, note "1 idle hidden" (npm+sh = 2 idle → "2 idle hidden").
  - boundary: exactly 200 MB / 2% / 5% is **not** hot (strict `>`).
  - guard: a `context7-mcp` whose nearest visible ancestor is **not** user-session
    → not eligible, empty plan.
  - `cpu_current is None` (sampling off) → current-CPU test can't trip.
- **`build_view`** (session axis) with the plan → claude node has no npm/sh child,
  carries `helper_note`; partial case has a single `↑`-marked lifted child.
- **`_visible_children`** pass-through: transparent middle node surfaces its
  grandchild.
- **Byte-compat**: an existing `render()` output test still passes (transparent
  defaults empty).

## Out of scope

- No new CLI flag (the `--expand-all` escape hatch is enough).
- No generalization beyond a marker list; `context7-mcp` is the only entry today.
