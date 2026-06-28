# psf: grouping axes + cartographic color

Status: approved design, ready to implement. Date: 2026-06-27.

## What this is for

`psf --path ~/proj` prints a flat, category-sorted list; the default view is a
generic interesting-subtree tree with per-parent `×N` dedup. Two things wanted:

1. A way to read either view once the natural tree nesting is gone - color that
   lets you trace *which directory* and *which command* a line belongs to when
   matching lines are scattered across the screen.
2. A switch between session-first grouping (`session→children`) and
   cross-session consolidation (`<session>×2 → pooled children`, so compile
   workers from different sessions merge).

Plus: always-on merging of similar children (with an escape), and an optional
group-by-directory axis.

The load-bearing constraint sits underneath all of it: the operator has reduced
(not absent) red sensitivity. Bold, well-separated colors are fine - including
ordinary red/green. What fails is *subtle* differences along the red axis
(purple vs blue, dark-blue vs black) and *scattered near-neighbours* that only
separate side-by-side. So color must carry information on the blue↔yellow axis
plus luminance, in a small palette, validated scattered. The full physiology
and palette calibration live in `~/.claude/memory/color-vision.md`.

## Architecture: model → view → render

Today `render()` fuses three jobs in one recursive walk: it calls
`group_siblings` at every level (grouping), computes detail bits *and* bakes ANSI
via `_compose_dim` (coloring), and emits connectors (layout). The three move
apart:

- **model** - `scan()` → `build_tree` → `select` → decorate (cwd/exe/category/
  venv) → `collapse`. Unchanged, pure-ish, already tested. Plus a new
  `sessionize` step that attributes each interesting proc to a session leader.
- **view** - `build_view(roots, suppressed, axis, opts) → list[ViewNode]`. Pure,
  no `/proc` IO. Does *all* merging for the chosen axis and resolves it into the
  node tree. Annotates each node with semantic **color-keys** (`("dir", raw)`),
  not ANSI. Unit-testable as data.
- **render** - `emit_lines(nodes, colormap, color) → list[str]`. Dumb. Walks the
  node tree, emits the connector skeleton, resolves color-keys → ANSI through the
  `ColorMap`. The only place connectors or escapes are produced.

`--path` is not a separate renderer - it is a filtered node-source into the same
view+render. That is the whole point of the refactor: the path view cannot drift
from the main view because there is only one emitter.

`render()` stays as a thin public wrapper - `emit_lines(build_view(...,
axis="tree"), colormap=None, color)` - with its current keyword signature intact.
~15 tests call `render()`/`group_siblings()`/`collapse()` directly; they stay
green. `axis="tree"` is today's per-parent grouping with no session attribution;
the new axes are `build_view` modes used only by `main()`.

## Display model

```
Bit       = (text, tint_level:int, color_key|None)
            invariant: per bit, at most one of {tint_level>0, color_key} is set.
            heat colours magnitude on stat bits; identity colours dir/cmd bits;
            they never share a bit. Legacy (text,level) normalises to key=None.
ColorKey  = ("dir", raw_path) | ("cmd", raw_path) | None
            always from the RAW readlink path (HOME→components, " (deleted)"
            stripped); never from compress_path output (lossy → hue wobble).
ViewNode  = kind: "proc"|"group", payload: Proc|Group, badge,
            head_label (width-compressed cmdline or "×N <brace>", WITHOUT
            prefix/connector/badge - renderer prepends those),
            detail_bits, extra_bits (path-hit bits), collapse_note (proc-only),
            children: list[ViewNode] (already merged), sort_key
ColorMap  = anchor: tuple[str,...], families: {component: FamilySlot}
FamilySlot= (family_index 0..3, base_oklab)
```

`Group` (the existing merge namedtuple) stays untouched as the merge primitive;
`ViewNode` wraps either a `Proc` or a `Group`.

## Grouping axes - `--group {session,consolidated,dir}` (default `session`)

**session** - each interesting proc is attributed to the **nearest ancestor**
matching a `SESSION_LEADER` table (sshd / `sshd:`, tmux / `tmux: server`, login,
a login shell under a leader or pid1, a standalone claude). Fall back to the proc
itself when no ancestor matches ("standalone" = no ancestor leader anywhere). Not
the literal kept-tree root - `select()` keeps ancestors up to pid1, so a
"subtree root" would collapse every session under systemd, which is useless. A
leader nested under another leader renders as a sub-node of the outer session and
keeps its own subtree, so worker→claude attribution survives.

**consolidated** - near-identical sessions fold into one `×N`. "Near-identical" =
equal `SessionSignature = (leader_category, leader_comm, uid, sorted multiset of
child merge-keys)`. Folding on leader comm alone would merge an idle shell with a
heavy build and then mislead through pooled child counts; the uid + child-kind
multiset makes "alike" mean genuinely alike. Pooled child counts are **summed**
across folded sessions (3 sessions × 2 bash → `bash ×6`); `dedup_min` applies to
the pooled total. `NEVER_MERGE` leaders (claude) and distinct uids never fold.

**dir** - bucket by canonical cwd; `?`/`""` land in one explicit `unknown`
bucket, never mixed with real dirs; `" (deleted)"` normalises to the live dir.
Discards session structure by design (that is what color is for). Meaningless
under `--path` (the path already scopes a dir) → argparse error.

**`--pool-global`** (only with `--group consolidated`) - a flat, sessionless
global `×N`-by-kind listing. A globally-pooled entry has no single session header
to sit under, so it overrides per-session headers rather than nesting.

## Merging

Always on. Merge key = `(category, comm, exe)`. Category gates merging - this is
what fixes the badge problem: a venv python promoted to user-session no longer
merges with a bare compile-worker python, because they are genuinely different
roles, and with category in the key every group member shares the badge so
`members[0]`'s badge is always honest. `exe` ∈ {`?`, `""`, trailing
`" (deleted)"`} is a non-mergeable discriminator (per-pid unique key) so unknown
daemons don't collapse into one bogus `×N`. `DEDUP_MIN` stays 3 - "always on" is
the existing default posture, not a lowering to 2, which keeps default output
stable.

Two escapes:
- **`--verbose`/`-v`** - no sibling merging (`dedup_min=None`, each proc listed).
  Subtree collapse still applies. Absorbs the old `--no-dedup`.
- **`--expand-all`** - implies `--verbose` *and* disables subtree collapse
  (ignore the suppressed set and `.collapsed` flags; no collapse_note). When both
  given, `--expand-all` wins (strict superset).

Neither touches *selection* - still interesting subtrees only, kernel threads
still excluded - and neither disables the grouping axis.

## Color (`ColorMap`) - cartographic, protanomaly-safe

The idea is a map: a directory's hue comes from *where it sits*, so related dirs
share a colour family and you can trace identity when nesting is gone.

**Anchor.** Strip the prefix everyone shares (usually `~/proj`) so it doesn't
dominate. Computed on the **canonical raw** path (readlink, `" (deleted)"`
stripped, split on `/`), filtering `?`/`""`/pseudo (`socket:`/`pipe:`/…) *before*
anchoring. Anchor = deepest **component-prefix** (not `startswith`) shared by
≥ half of valid paths *and* by ≥ 2 paths, never consuming a path's last component
(so a family component always exists, including the all-identical case). < 2 valid
paths → no anchor, nothing coloured. `--color-anchor PATH` overrides. Keying on
raw components is the only stable choice - `compress_path` is lossy, so display
strings would give the same real dir different colours and split HOME vs non-HOME.

**Family hue.** First component past the anchor. Four scatter-validated
saturated families only: yellow `#F2E34C`, amber `#E69F00`, green `#2E9E6B`,
blue `#1565C0`. **Round-robin assignment**: every family under the anchor gets a
hue, cycling the four by weight rank (weight = node count = visual mass;
deterministic sort `(-count, name)`). With more projects than hues the
least-frequent pairs collide and the printed dir text disambiguates - chosen over
"top-4 only, rest gray" so the most projects possible are coloured.

**Gray is the no-family sentinel**, not a fifth family - gray ≈ terminal default
fg, so a gray family would be indistinguishable from "uncoloured". Outside-anchor
and unreadable paths render as terminal default fg.

**Depth shade.** Lighten *within* a family so siblings read as related, in OKLab
L* (perceptually even), bounded by a **ceiling** (~L* ∈ [0.55, 0.80] on dark bg) -
lightening needs a ceiling, not a floor, or deep paths wash into the background.
Quantise depth `min(depth_past_anchor, 2)` into 3 buckets; a protanomalous viewer
resolves only ~2-3 lightness steps per hue anyway.

**Coexistence with heat-tint.** The existing cpu/rss warm magnitude ramp
(`TINT_SGR`) is untouched and stays on the stat bits. Identity hue only lands on
dir/command text. The per-bit invariant keeps them from ever sharing a span. A
yellow directory could read as "hot" to a reduced-red viewer, so the two channels
are independently controllable: `--color {auto,heat,identity,none}` (and/or
`--no-identity-color`); `--no-color` kills both.

**Watch stability.** Single-shot is pure per-frame frequency rank. Under
`--watch`, a sticky `component→family` map with hysteresis: last frame's family is
this frame's first choice, a new family claims only a free slot, a component
reverts to uncoloured only after N absent frames (≈5) - otherwise colours flicker
as the rank order churns.

**Truecolor gating.** Identity hue needs 24-bit (`COLORTERM` ∈ {truecolor,
24bit}); else a guard-validated 4-entry 256-color fallback, else identity off and
heat-tint retained. Protects the scatter-validated distances on non-24-bit
terminals.

## CVD guard (the regression net)

A frame-independent unit test over the palette constant. Simulate the four
families × three depth levels *plus* the assumed terminal default-fg and bg with
the Machado et al. (2009) protanomaly matrix at severity ~0.5 (anomalous
trichromat, not dichromat - simulating full protanopia would over-reject a
palette that is fine for this user), also asserting at 0.4/0.6. Convert to CIELAB,
measure pairwise CIEDE2000 (CIE76 acceptable fallback). Assert min inter-family
ΔE2000 ≥ a floor locked just under the measured palette minimum (≈15), and
within-family lightness step ≥ JND (≈5). Pure-Python sim + ΔE2000 over the tiny
palette; numpy only in `[dev]`. This is what keeps the scatter-validated result
from silently eroding under a later edit - including keeping every real family
away from the gray default-fg.

## CLI surface

New: `--group {session,consolidated,dir}` / `-g` (default `session`),
`--pool-global`, `--verbose`/`-v`, `--expand-all`, `--color-anchor PATH`,
`--color {auto,heat,identity,none}`. `--no-dedup`/`--dedup-min` become
`SUPPRESS`'d aliases mapped to the new flags in one place (don't break muscle
memory). Combo validation is centralised so `--path` and `main` share it:

- `--group dir` + `--path` → error (dir grouping meaningless under a path scope).
- `--pool-global` without `--group consolidated` → error.
- `--color-anchor` under `--no-color` → accepted, no-op (documented).
- `--path` default axis is **flat** (preserves today's category-priority list);
  `--group session|consolidated` under `--path` is allowed but re-walks `/proc`
  to recover each hit's leader (a sparse hit set has no ancestors).

Glossary gains a line naming the active anchor and the two-channel (identity vs
heat) model; the active anchor prints even under `--no-glossary` (it's data).

## Pipeline order

`select → decorate (read_links + classify + detect_venv over kept candidates) →
sessionize → collapse (always; mutates model) → build_view (merge per axis,
honour verbose/expand-all, attach raw color-keys) → build_colormap (detect_anchor
+ assign_families) → emit_lines (resolve keys → ANSI)`. The expensive
socket/netmap probe set is derived by walking the finished `ViewNode` tree, not a
separate `collect_printed` - otherwise the probe set drifts out of sync with what
is actually shown.

## Build order (TDD - each step keeps tests green)

0. Fix the two env-hardcoded tests (`/home/shemer/...` → `psf.HOME`-based) so the
   suite is 100/100 on any host. True gate before touching production.
1. Pure model→view→render refactor, output byte-identical. `ViewNode` + `Bit`;
   `_detail`/`_group_detail` return `list[Bit]`; `_compose_dim` moves into
   `emit_lines`; `build_view(axis="tree")`; `render()` becomes the wrapper. Add a
   connector golden-string test + ViewNode-shape snapshots before flipping
   anything.
2. Merge-key generalisation: `group_siblings(key_fn=...)` (default `(comm,exe)`),
   unknown-exe non-merge guard, `build_view` passes `(category,comm,exe)`.
3. Session axis: `SESSION_LEADER` table + `find_session_root`;
   `build_view(axis="session")`; `main()` default axis → session.
4. Consolidated axis + folding + `--pool-global`.
5. Dir axis.
6. Escape flags in model/view (`merge`, `collapse` booleans).
7. CLI surface + combo validation + suppressed aliases.
8. `--path` unification: route through `build_view(axis="flat")`, delete
   `render_path`.
9. Pure color layer (identity OFF): canonical-path helper, `detect_anchor`,
   `assign_families`, OKLab depth-lighten, truecolor gate, the CVD guard test.
10. Flip identity ON: thread `ColorMap` into `emit_lines`; dir/cmd spans get the
    identity SGR, connectors/badge/pid stay default-fg; glossary line.

## Risks (and mitigations)

- Default output **changes** at step 3 (flat tree → session-grouped), only
  substring-smoke-tested → add ViewNode-shape snapshots before flipping the
  default.
- Connector/root-special-case indentation is easy to break when relocated → lock
  a golden-string test before step 1.
- `_compose_dim` is byte-sensitive (the `\x1b[2m  \x1b[0m` separator) → relocate
  verbatim, assert SGR reset-balance.
- Identity hue is the first SGR ever on the head line → a missing color gate
  leaks escapes into piped output; thread the single existing flag, add a "zero
  \x1b when color=False on the head line" test.
- Color key accidentally taken from `compress_path` output → hue wobble; assert
  it never is.
- Truecolor assumption on non-24-bit terminals → `COLORTERM` gate + 256 fallback.

## Out of scope / follow-ups

- Heat-tint is itself a warm/red ramp, which a reduced-red viewer reads more as
  "dimmer" than "hotter". Swapping it for a blue→yellow→white luminance ramp is a
  separate change.
- **`--color {auto,heat,identity,none}` / `--no-identity-color` not implemented.**
  Only `--no-color` (kills both channels) and `--color-anchor` ship. There's no
  way to keep heat while turning identity off (or vice-versa). Deferred.
- **256-color fallback not implemented.** Identity hue is truecolor-or-nothing,
  gated on `COLORTERM ∈ {truecolor,24bit}`; on a 16/256 terminal identity is off
  and only heat-tint shows. The spec's "guard-validated 4-entry 256-color
  fallback" is deferred.
- **Watch stickiness is moot** — psf is snapshot-only, there is no `--watch`. The
  sticky-anchor/hysteresis design applies only if `--watch` is ever added; do the
  deterministic-anchor work (done) first.
- **Yellow depth resolution is shallow** — `#F2E34C` (OKLab L≈0.90) saturates near
  the lightening ceiling, so a yellow project's dirs at depth ≥1 share a shade.
  Acceptable (sibling shades are meant to look related); revisit with a per-family
  ceiling if it matters.
- ~~`CLAUDE.md` stale~~ — **done**: rewritten to reflect the self-contained,
  model→view→render psf with the real axis/colour function names.
