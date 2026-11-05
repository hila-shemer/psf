context:
- /home/hila/proj/manager-apps/psf-topfig/docs/next-manager-2026-11-05-psf-topfig-config-editor.md
- /home/hila/cc-cmds/psf-topfig-land.check
- /home/hila/cc-cmds/psf-topfig-land.sh
- /home/hila/cc-cmds/psf-topfig-land.bg.sh
- /home/hila/cc-cmds/psf-topfig-land-tail.sh
- /home/hila/cc-cmds/psf-topfig-land.why
- /home/hila/cc-cmds/psf-topfig-land.reject
- /home/hila/proj/manager-apps/psf/docs/superpowers/specs/2026-06-07-topfig-config-editor-design.md
- /home/hila/proj/manager-apps/psf-topfig/build.py

# psf topfig config editor - landed, awaiting the press

## Where it stands

The deploy row for this branch is the newest pending `apps` row on the board
(`claude-control buttons --category apps` names it and its seq). It is `pending`; Hila
has not pressed it. The row's bytes are the five `~/cc-cmds/psf-topfig-land.*` files
listed above, plus the tail script; the check passes now and passed the sandbox
rehearsal at filing, and `claude-control advise` returned **good**.

This document does not name its own row's seq: the row's check pins the sha of the
commit that contains this file, so a seq written here could never match the row that
files it. Two earlier filings of this same land (#22707, then #22712) were superseded
while the document was being finished - the last edit to it is what moved the pin, which
is why they are gone.

## What was landed, and from where

The branch `feat/topfig-config-editor` in `/home/hila/proj/manager-apps/psf`, pushed to
`origin` on 2026-11-05. The row pins the branch tip's sha and its tree; both live in
`/home/hila/cc-cmds/psf-topfig-land.check` rather than being repeated here, for the same
reason. Ten commits: eight cherry-picked from `other_config` (authorship kept, `-x`
recorded), one new retarget commit, and this handoff document - which rides the branch
so it lands with the change rather than sitting above the landed sha.

`other_config`'s tip was `44ab666` (2026-06-23). Three of its commits were **not**
taken, because their content is already on main in another form:

- `4aa61fe docs: spec for topfig static config editor` - main carries this spec, in its
  final "Status: implemented" wording.
- `bb21da9 docs: implementation plan for topfig config editor` - main carries the plan.
- `44ab666 stop tracking pyc` - main never tracked `__pycache__`; its `.gitignore`
  already ignores it, so there was nothing to untrack and nothing to add.

The one code change on top of the branch is `12f9c1df` (the second-newest commit; the
newest is this handoff):

    fix(topfig): retarget the vmstat knob at VMSTAT_PCT_ANCHORS

`build.py` reads the real `topf.py` and refuses with `topf.py is missing knob
constants: vmstat_outlier_anchors` when a knob-controlled constant is gone. main's
`topf.py` no longer has `VMSTAT_OUTLIER_ANCHORS` - the robust z-score outlier tinting
it fed was replaced by history-grounded cdf thresholds. So the knob now points at
`VMSTAT_PCT_ANCHORS`: three numbers to a `TINT_SGR` level 1..3, the same shape and the
same job as the old anchors, and the same thing the client already knew how to render
(`pyTupleFloat`). The marker was renamed with it (`vmstat_outlier_anchors` ->
`vmstat_pct_anchors`) in `build.py` and in `topfig_template.html`'s two places, and
`index.html` regenerated.

**The only judgement call in this land** is that retarget. The alternative was to drop
the vmstat entry from `KNOBS` and leave those vmstat constants to the raw custom
textarea. Retargeting was chosen because the knob's UI role is unchanged and the
accepted spec's "Colors & thresholds" group names four knobs; if a reviewer disagrees,
the change is three lines.

## What was measured

- `nice -n 10 python3 -m pytest tests/ -q -n 14` on the rebased tree: **317 passed**,
  ~5 s (4.97 s and 5.58 s on two runs). main alone is 310; the 7 are
  `tests/test_build.py`.
- `tests/test_build.py` alone: **7 passed**, 0.22 s.
- `python3 -I build.py` is deterministic: a re-run produces a byte-identical
  `index.html`.
- The client substitution reimplemented independently against the JSON baked into
  `index.html`: untouched knobs reproduce `topf.py` byte for byte; `CMD_WIDTH` 77 lands
  and the emitted source compiles; zero stray `@@TOPFIG:` sentinels.
- `node --check` on the template's `<script>` block: parses.
- The tail rehearsed on a scratch clone at `/tmp/psf-neg/work`: **3/3, exit 0** on the
  tree as it will land; **2/3, exit 1** with `index.html` drifted one line from its
  source; **1/3, exit 1** with `build.py` removed. The middle one is the control that
  matters - it exercises the comparison the tail actually makes.

## What is NOT proven

- No browser has ever loaded `index.html`. The DOM, the Save/download button and the
  pre/post textarea toggle are unexercised; everything verified above is the build half
  plus a re-implementation of the substitution in Python.
- `index.html` has never been deployed, and the press does not deploy it.

## One thing the advisor added, which the row's why does not carry

`advise` returned **good**, with one note: the rollback line in the why deletes the four
files but leaves the tag `topfig-config-editor` pushed on psf's origin. A why-only amend
is not available - `--modify` insists on a script part - and re-filing again for one doc
line was not worth the churn, so it is recorded here. If this land is ever undone, also

    git -C /home/hila/proj/manager-apps/psf push origin :refs/tags/topfig-config-editor

## Repo and serving facts checked for the brief

- **Nothing serves psf.** No systemd unit (user or system) names `psf` or `topf`, no
  nginx site, no `gh-pages` or pages ref on `origin`, and
  `/home/hila/proj/manager-apps/psf` is a plain checkout. `revive-topf` is a tag on the
  tree main already carries - `origin/main^{tree}` and `feat/revive-topf^{tree}` are
  both `ce02db0b3b0087215908b40a02efeeecedc5e89a`, so the tag names a lineage main has
  and the divergence is only in commit shas (main's tip `bb895b3` was rebased from
  `feat/revive-topf`'s `29f7299`). So this land changes nothing that is served or run.
- **The generated `index.html` belongs in git here**: the accepted design says so in
  its Artifacts section - "Both are committed to the repo", and the committed
  `index.html` is named as the artifact that gets deployed. The repo has no other
  committed generated file, so the spec is the whole habit.
- **`topf`, the older clone at `/home/hila/proj/manager-apps/topf`, was left alone.**
  Its `main` is `49c8623`, an ancestor of psf's main, its `other_config` is the same
  `44ab666`, it is clean, on `main`, with no extra worktrees. psf supersedes it; no
  action taken and none needed.
- `psf-revive-topf.bg.sh` (the previous psf land, Oct 2026) still points its `topf` shim
  at `~/proj/psf/topf.py`, which the `~/proj` eviction moved. **Not touched here** - it
  is a mercury-side shim and out of this task's scope, and it only matters if
  `~/proj/psf` is gone. Worth a look by whoever next works on psf's shims.

## If the press fails

- The bg job is idempotent: a re-press whose land already happened skips the land by
  `git cherry` and goes straight to the tail.
- exit 9 from `cc-land-branch land` means **landed** - main and the tag are pushed -
  with the checkout left behind. The job says so; run the line `cc-land-branch` printed
  on stderr, then press again.
- The `--reject-why` covers the deny case: main already carries `build.py`, or she does
  not want the tool at all.

## After the press

Nothing to install, restart or verify outside the job. The tag `topfig-config-editor`
marks the landed head; the checkout fast-forwards itself as part of `land`.
