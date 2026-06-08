The session died and we need to restart - we almost finished the whole plan, so most of the below is already-implemented.

Original claude conversation follows:


● Created CLAUDE.md. Here's what I included:

  - Project summary — topf (live process viewer) and topfig (config editor generator)
  - Commands — test, run, and build commands with examples of single-test invocation
  - Architecture — a section-by-section table of topf.py's ~1920 lines (pure core → I/O → render → live UI → CLI), plus build.py's AST template-injection pipeline, and test structure
  - Conventions — the generated-artifact rule for index.html, missing pytest dependency, import style, and git push policy

  I kept it concise: no generic advice, no file listings that are easy to discover, and no repetition of what AGENTS.md already covers (though I did re-state the key points since CLAUDE.md is the primary entry point for future Claude sessions).

✻ Cogitated for 1m 2s

❯ Review the code. I think I want topf to also show FSs disk space usage state. I want to make sure there's no information in the top rows of 'top' that we're missing here - if there's a high CPU process, we'll definitely see it because the coloring moves it
  up right? I want more ways to look into the 'collapsed' groups at the bottom, esp. when there's a big compile job and ~70 processes are 'hidden'. We can do small fixups - remember the index.html is a joke installation method and doesn't need to be
  maintained all the time. Make it an installable thing as in that it pulls in deps as it should - python package, I guess? wheel? something round

  Read 1 file (ctrl+o to expand)

● Let me review the code thoroughly before answering your questions. I'll focus on the areas you mentioned.

  Thought for 1s, read 1 file (ctrl+o to expand)

● Bash(top -b -n1 2>/dev/null | head -8 || echo "top not available or no /proc")
  ⎿  top - 22:35:22 up  7:21,  6 users,  load average: 0.17, 0.23, 0.91
     Tasks: 295 total,   1 running, 294 sleeping,   0 stopped,   0 zombie
     %Cpu(s):  1.8 us,  3.6 sy,  0.0 ni, 94.6 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st
     … +5 lines (ctrl+o to expand)
  ⎿  Allowed by auto mode classifier

● Now let me also check what top shows in its header for comparison:

  Read 1 file (ctrl+o to expand)

● OK. I've now read the entire codebase thoroughly. Let me give you my review, organized by the topics you raised.

  ---
  1. What top shows that topf doesn't in the header

  Here's what top puts in its first 5 lines vs what topf's single header line contains:

  ┌──────────────────────────────────────────┬──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
  │                top header                │                                                                                       topf                                                                                       │
  ├──────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ uptime + load average                    │ ❌ missing (uptime is computed internally for process lifetimes but never shown)                                                                                                 │
  ├──────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Tasks: N total, R running, S sleeping, … │ ❌ missing (topf shows N procs (M hidden) but no running/sleeping/zombie breakdown)                                                                                              │
  ├──────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ %Cpu(s): us/sy/ni/id/wa/hi/si/st         │ ✅ covered by the vmstat pane (us/sy/id/wa), but ni (nice), hi (hard IRQ), si (soft IRQ), st (steal) are folded into us/id — you lose visibility into IRQ storms and stolen time │
  ├──────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ MiB Mem: total/free/used/buff/cache      │ ❌ missing from header (vmstat pane shows free/buff/cache as snapshots, but no total or used — you can't tell at a glance how much RAM the machine has)                          │
  ├──────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ MiB Swap: total/free/used                │ ❌ missing (swap_total is parsed from meminfo but only used as a boolean swap_on for si/so column visibility — the actual swap usage is never displayed)                         │
  ├──────────────────────────────────────────┼──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ load average                             │ ❌ missing                                                                                                                                                                       │
  └──────────────────────────────────────────┴──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

  FS disk usage — neither top nor topf show this. df-style mount-point usage (/proc/mounts + statvfs) would be entirely new territory.

  The biggest gap is load average and memory total/used — those are the first things I reach for when I SSH into a box. The IRQ/steal folding is a real loss on VMs (cloud steal is a frequent mystery).

  2. Will a high-CPU process always be visible?

  Yes, but it won't necessarily "move up". Here's the critical path:

  1. A process using ≥1.0 core clears PROMOTE_LEVEL=2 → is_promoted() returns True → it's marked interesting + kept (topf.py:232–250)
  2. Top-level subtrees are sorted by subtree_window_cpu(root, sort_idx) — the root whose total subtree CPU is highest renders first (topf.py:1411–1416)
  3. But: subtree_window_cpu sums the whole subtree. A single hot leaf under an otherwise-cold bazel server won't move that bazel server above another bazel server that has 10 medium children. The hot leaf appears inside that subtree, but the order of
  top-level roots is by aggregate.

  So: a heavy process is always visible (promoted → kept → rendered). But it may be deep in a subtree that's sorted lower than you'd expect. The user has to expand and scroll. There's no "hot processes float to top regardless of tree structure" mode.

  3. Collapsed groups — the 70-process compile job problem

  The collapse mechanism (topf.py:314–342) has a single expansion axis: a node is collapsible if its non-interesting kept descendants exceed COLLAPSE_THRESHOLD=90. You expand one at a time with Space/Enter.

  Problems during a big bazel build:

  - Collapsed summary is low-information: "… (+65 descendants: clang×42, clang++×20, …)" — you can't see which clang processes are hot, what they're compiling, or what their individual CPU/RSS is. You'd need to expand the whole subtree (flooding the screen) to
  find the one that's misbehaving.
  - Group detail is aggregated: ×42 clang shows cpu/rss ranges and pid snippets, but 42 processes in a range "50%–400%" tells you very little about the distribution.
  - No "expand one group inside a collapsed subtree": You can expand the collapsed ancestor (revealing all 65 descendants), then expand a group — but you can't selectively reveal just one group without showing the other 50 noise processes.
  - No filtering/search: There's no way to say "show me only processes matching X" within the live UI.

  Concrete improvements I'd consider:

  ┌───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┬────────┬────────────────────────────────────────────────────────────────────────────────────────┐
  │                                                                           Idea                                                                            │ Effort │                                         Value                                          │
  ├───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┼────────┼────────────────────────────────────────────────────────────────────────────────────────┤
  │ Expand-all-groups key (E?): auto-expand all Group nodes inside a collapsed subtree without revealing individual non-grouped processes                     │ small  │ you'd see ×42 clang → 42 individual clang rows with per-proc CPU, while generic procs  │
  │                                                                                                                                                           │        │ stay hidden                                                                            │
  ├───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┼────────┼────────────────────────────────────────────────────────────────────────────────────────┤
  │ Hot-proc breakout: when a collapsed subtree has ≥1 promoted descendant, show those promoted ones individually (with their detail lines) even while        │ medium │ the exact thing you want during a build: the top 3–5 CPU hogs poke through the         │
  │ collapsed — a "heads up" row                                                                                                                              │        │ collapse                                                                               │
  ├───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┼────────┼────────────────────────────────────────────────────────────────────────────────────────┤
  │ Group-level CPU histogram: instead of ×42, show ×42 [cpu: ████░░░] — a tiny ASCII histogram of the CPU distribution across the group                      │ small  │ instant sense of "are they all equal or is one an outlier"                             │
  ├───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┼────────┼────────────────────────────────────────────────────────────────────────────────────────┤
  │ Filter/search key (/): type a regex, only matching procs + their ancestors are shown                                                                      │ medium │ general-purpose, solves many inspection problems                                       │
  └───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┴────────┴────────────────────────────────────────────────────────────────────────────────────────┘

  4. Small fixups I noticed

  - header_line is information-sparse (topf.py:1761–1766): no load avg, no mem summary, no task breakdown. Even just adding load %.2f (from /proc/loadavg) and mem: 10G/63G would bring it to parity with top.
  - Swap shown as boolean only: parse_meminfo reads SwapTotal but read_vmstat_sample only passes it as swap_total to the VmstatSample namedtuple, and the only consumer checks any(s.swap_total for s in vmring) to decide whether to show si/so columns. The actual
  swap usage (SwapFree → used = total − free) is never surfaced.
  - _once_defaults() is fragile (topf.py:1842–1850): every new CLI flag must be manually added there or render_once breaks with a missing-attribute AttributeError. Consider building it from _parse_args([]) plus overrides instead.
  - build_frame ignores expanded (topf.py:1790–1817): it calls prepare_frame(cur, args, sysinfo) without passing expanded, so the once/piped path can never show expanded groups. The render call at line 1808 also omits expanded. This means --once always shows
  the collapsed view even if the user wanted to see inside.
  - Cache is saved with live_keys=set() (topf.py:1758): the live loop's finally block passes an empty set, so Cache.save persists nothing — only entries whose (pid, starttime) is in live_keys survive. After quitting and restarting, every entry is gone. The
  prepare_frame path (line 1786) saves correctly with the full live set, but that's only the last frame's snapshot; on exit, the empty-set save wipes it.

  5. Packaging (the "something round" request)

  Right now topf is a bare script with no packaging. Making it an installable wheel:

  - pyproject.toml with [project] metadata (name, version, entry-points for the topf CLI), [build-system] (hatchling or flit — both are zero-config for single-module projects), and a [project.scripts] entry pointing topf = "topf:main"
  - topf.py stays at repo root — hatchling/flit can pick it up directly, no need to move it into a package directory
  - No external dependencies — topf is pure-stdlib, so the wheel is a zero-dep wheel
  - pytest stays a dev dependency — [project.optional-dependencies] dev = ["pytest"]
  - The build.py / topfig_template.html / index.html generation stays out of the wheel — it's a build-time tool, not a runtime dep
  - pip install -e . for development, python -m build to produce the wheel

  This is straightforward. The only decision is the build backend — flit is the lightest for single-file modules (no src layout needed).

  ---
  Summary of what I'd prioritize:

  1. Header line enrichment — load average + memory summary (small, high daily value)
  2. Hot-proc breakout from collapsed subtrees — promoted processes poke through the collapse (medium, directly addresses the 70-process problem)
  3. Expand-all-groups key — E on a collapsed node (small, complementary to #2)
  4. Swap usage display — actually show swap used/total, not just si/so column visibility (small)
  5. FS disk space pane — new feature, /proc/mounts + statvfs (medium, adds something top doesn't have)
  6. pyproject.toml — wheel packaging (small, mechanical)

  Want me to plan the implementation for any subset of these?

✻ Sautéed for 1m 20s

❯ Yes, let's do them all. I also want to 'revive' psf - it was renamed, but long ago this script printed out just once. I want a variant of the old psf, that unlike topf is not focused on recent history but the longer-running history - I want psf to show
  which claude sessions are running where, ideally what venv they're in, and ignore (as in summarize, not delete from view) the redudnant glue - ssh/bash/tmux/etc. - there's stuff under bazel and such background daemons, and there's user sessions. If there's
  a large cluster of new processes, it would be good to show them. But most of the time there will be very few 'new' processes, and the focus isn't to show the oldest ones but like in topf and original psf to show the most interesting ones. This variant will
  go deeper into classifying what kind of process it is and what metadata is interesting about it.

● Entered plan mode
  Claude is now exploring and designing an implementation approach.

● 2 Explore agents finished (ctrl+o to expand)
   ├ Explore psf/process metadata · 46 tool uses · 0 tokens
   │ ⎿  Done
   └ Explore packaging patterns · 19 tool uses · 0 tokens
     ⎿  Done

● Let me explore the codebase to understand what I need for the plan, then design the implementation.

● Bash(cat /proc/loadavg 2>/dev/null && echo "---" && head -3 /proc/1/environ 2>/dev/null | tr '\0' '\n' | head -5 && echo "---" && cat /proc/mounts 2>/dev/null | head…)
  ⎿  0.43 0.41 0.76 1/1101 662997
     ---
     ---
     … +7 lines (ctrl+o to expand)
  ⎿  Allowed by auto mode classifier

● Now let me check a few more things before designing the plan:

  Thought for 1s, searched for 1 pattern (ctrl+o to expand)

● Bash(# Check what a claude process environ looks like
      pgrep -a claude 2>/dev/null | head -3; echo "---"…)
  ⎿  661123 claude
     ---
     661724 python3 /home/shemer/.local/bin/topf.py
     … +3 lines (ctrl+o to expand)
  ⎿  Allowed by auto mode classifier

● Bash(# Check what /proc/PID/environ contains for a claude process
      CLAude_PID=$(pgrep -n claude 2>/dev/null); echo "claude pid: $CLAude_PID"; cat /proc/$CLAude_PID/en…)
  ⎿  claude pid: 661123
     SHELL=/bin/bash
     CLAUDE_CODE_SUBAGENT_MODEL=z-ai/glm-5.1
     … +28 lines (ctrl+o to expand)
  ⎿  Allowed by auto mode classifier

● Bash(# Check a python process for VIRTUAL_ENV
      PY_PID=$(pgrep -n python3 2>/dev/null); echo "python pid: $PY_PID"; cat /proc/$PY_PID/environ 2>/dev/null | tr '\0' '…)
  ⎿  python pid: 661724
     ---
     lrwxrwxrwx 1 shemer shemer 0 Jun  7 22:36 /proc/661724/exe -> /usr/bin/python3.12
     … +2 lines (ctrl+o to expand)
  ⎿  Allowed by auto mode classifier

● Good. Now I have all the information I need. Let me design the plan.

● Plan(Design implementation plan)
  ⎿  Done (41 tool uses · 66.4k tokens · 5m 13s)
  (ctrl+o to expand)

  Read 1 file (ctrl+o to expand)

● Good, I've verified the bug. Now let me write the final plan:

● Updated plan
  ⎿  /plan to preview

● Let me verify one more detail I want to double-check before presenting the plan:

  Searched for 1 pattern (ctrl+o to expand)

● _read is a private function (underscore prefix). psf importing it is technically fine but not clean. Let me check what psf actually needs from topf vs what it could just call directly:

● User answered Claude's questions:
  ⎿  · psf needs _read(), PAGE_SIZE, CLK_TCK from topf — all private/underscore-prefixed. How should psf get these? → psf calls /proc directly (Recommended)
     · How should the disk pane and vmstat pane coexist in the live TUI? → Both panes stack (Recommended)
     · psf's --watch mode: simple reprint loop or full TUI? → Reprint loop only (Recommended)

● Good — those are the choices I recommended. Let me finalize the plan with those decisions baked in, then submit for approval.

  Read 1 file (ctrl+o to expand)

● Now let me update the plan to reflect the user's decisions on the three questions:

● Updated plan
  ⎿  /plan to preview

● Updated plan
  ⎿  /plan to preview

● Updated plan
  ⎿  /plan to preview

● Good. The plan is now updated with the three decisions. Let me submit it for approval.

────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Ready to code?

 Here is Claude's plan:
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Implementation Plan: topf enrichment + psf revival + packaging

 Context

 topf is a top-like live process viewer (~1920 lines, single-file, pure stdlib). The original psf.py was a one-shot snapshot tool renamed to topf.py early on. The user wants:
 1. Header line enrichment (load avg, memory, task breakdown)
 2. Hot-proc breakout from collapsed subtrees
 3. Expand-all-groups key
 4. Swap usage display
 5. FS disk space pane
 6. pyproject.toml / wheel packaging
 7. A new psf tool — a session-classification process finder (not windowed, not TUI — snapshot-first)

 Plus three bug fixes discovered during review.

 ---
 Phase 0: Bug fixes (topf.py)

 Bug 1: Cache wiped on exit

 run_live() line 1758: Cache(...).save(live_keys=set()) saves zero entries, wiping the cache. Fix: store live_keys from the last sample_and_build() and pass it to the exit save. Or just remove the exit-time save — prepare_frame already saves every frame.

 Fix: Add a _last_live_keys local in run_live(), set it in sample_and_build(), pass it to the finally block.

 Bug 2: build_frame ignores expanded

 Line 1796: prepare_frame(cur, args, sysinfo) — doesn't pass expanded, so --once mode can never show expanded subtrees.

 Fix: Add expanded parameter to build_frame(), forward to prepare_frame(). Default frozenset().

 Bug 3: _once_defaults() is fragile

 Lines 1842–1850: manually lists every flag. Adding a new CLI flag breaks it silently.

 Fix: Replace with:
 def _once_defaults():
     ns = _parse_args([])
     ns.no_cache = True
     ns.no_color = True
     return ns

 Files: topf.py only
 Tests: existing test_once_defaults_have_vmstat_fields still passes; add test_once_defaults_has_all_parse_args_attrs

 ---
 Phase 1: pyproject.toml packaging

 New file: pyproject.toml
 [build-system]
 requires = ["hatchling"]
 build-backend = "hatchling.build"

 [project]
 name = "psf"
 version = "0.1.0"
 description = "topf — focused live process viewer; psf — process session finder"
 requires-python = ">=3.9"
 license = "MIT"

 [project.scripts]
 topf = "topf:main"
 psf = "psf:main"

 [project.optional-dependencies]
 dev = ["pytest"]

 Update: .gitignore — add dist/, *.egg-info

 topf.py stays at repo root, psf.py also at repo root. Both are single-module packages hatchling discovers automatically.

 ---
 Phase 2: Swap + header enrichment (combined — they share parse_meminfo/header_line)

 parse_meminfo — add MemTotal, SwapFree

 Currently returns {free, buff, cache, swap_total}. Add mem_total and swap_free:
 return {"free": raw.get("MemFree"), "buff": raw.get("Buffers"),
         "cache": raw.get("Cached"), "mem_total": raw.get("MemTotal"),
         "swap_total": raw.get("SwapTotal", 0), "swap_free": raw.get("SwapFree")}

 VmstatSample — add swap_free, mem_total

 Add two fields to the namedtuple and forward them in read_vmstat_sample().

 New: read_loadavg()

 def read_loadavg():
     """(load_1m, load_5m, load_15m, running, total) from /proc/loadavg."""
 Format: 0.43 0.41 0.76 1/1101 662997

 New: count_states(procs)

 def count_states(procs):
     """(running, sleeping, zombie) from a {pid: Proc} snapshot."""
 Pure function, testable with synthetic data. scan() already reads state.

 Enhanced header_line()

 Two-line header:
 topf — 1.2s  8 cores  load 0.43/0.41/0.76  Mem: 10G/63G  Swap: 0/0K
 1101 procs (1 run, 1098 sleep, 2 zombie, 990 hidden)  every 1.0s  [q]uit ...
 mem_total and mem_used = mem_total - free - buff - cache (same as top). Swap: swap_total - swap_free / swap_total.

 Callers: repaint() and build_frame() pass loadavg + meminfo + task counts.

 Test coverage

 - test_parse_meminfo_includes_swap_free_and_mem_total
 - test_count_states_classifies_states (pure)
 - test_header_line_includes_load_avg
 - test_header_line_shows_swap_when_present
 - Update _vs() test helper for new VmstatSample fields

 ---
 Phase 3: Hot-proc breakout from collapsed subtrees

 New constant

 BREAKOUT_MAX = 5   # max hot procs shown under a collapsed subtree

 New: find_breakouts(procs, suppressed, collapsible, page_size, promote_level, rss_needs_cpu)

 Returns (breakout_pids: set, breakout_map: {proc_id(ancestor): [Proc]}).
 - For each collapsible ancestor, walk descendants in suppressed
 - If is_promoted(d, ...) → add to breakout_map
 - Sort by total CPU ticks descending, cap at BREAKOUT_MAX

 Modify prepare_frame()

 After collapse(), call find_breakouts() and remove breakout pids from suppressed:
 breakout_pids, breakout_map = find_breakouts(...)
 suppressed -= breakout_pids
 Return becomes 4-tuple: (visible_roots, suppressed, collapsible, breakout_map).

 Modify build_rows()

 New param breakout_map={}. After a collapsed node's collapse_note, emit breakout rows:
 └─ … (+65 descendants: clang×42, clang++×20, …)
    >> 42891 clang   cpu 340% 120% 50%  rss:450M  up:2m5s
    >> 42855 clang++ cpu 280%  90% 30%  rss:380M  up:2m5s
 >> marker distinguishes breakout rows. Selectable (cursor can land on them) but not expandable.

 Update callers

 run_live(), build_frame(), render_once() — handle 4-tuple return from prepare_frame().

 Test coverage

 - test_find_breakouts_identifies_promoted_descendants
 - test_find_breakouts_respects_max
 - test_build_rows_includes_breakout_rows

 ---
 Phase 4: Expand-all-groups key

 New key binding in run_live()

 elif key == "E":
     for r in rows:
         if r.expandable and r.item_id[0] == "g":
             ui.expanded.add(r.item_id)
     sample_and_build()

 Update header legend

 Add [E]groups to the key hint string.

 Test

 - test_E_key_adds_group_ids_to_expanded (simulate on synthetic rows)

 ---
 Phase 5: FS disk space pane

 New constants

 DISK_PANE_GUTTER = "diskfs"
 MIN_ROWS_FOR_DISK = 18
 MIN_COLS_FOR_DISK = 60
 DISK_ROWS_DEFAULT = 10
 DISK_USAGE_WARN_PCT = 80
 DISK_USAGE_CRIT_PCT = 95

 New namedtuple

 MountInfo = namedtuple("MountInfo", "mount_point device fstype total used avail pct")

 New I/O functions

 - read_mounts() — parse /proc/mounts, filter out virtual fs (proc/sysfs/devtmpfs/tmpfs/cgroup*/debugfs/tracefs/securityfs/etc.)
 - statvfs_mount(mount_point) — os.statvfs(), compute total/used/avail/pct
 - read_disk_sample() — calls both, returns [MountInfo]

 New render function

 format_disk_pane(mounts, width, height, color) — sorts by pct descending, tints at warn/crit thresholds.

 Layout integration

 Both panes stack at the bottom simultaneously. The v key toggles vmstat on/off; new d key toggles disk pane on/off. When both are visible, vmstat sits above disk. If terminal is too small, disk pane hides first (lower priority than vmstat). Enhance UIState:
 @dataclass
 class UIState:
     ...
     disk_on: bool = True

 split_regions() gets extended to return (tree_rows, vmstat_rows, disk_rows, show_vmstat, show_disk) instead of the current 3-tuple. When both panes are on, each gets a configurable slice of the remaining height after the header + tree minimum.

 CLI flags

 --no-disk       hide the disk pane
 --disk-rows N   max mount rows (default 10)

 Test coverage

 - test_read_mounts_filters_virtual_fs (Linux-only)
 - test_format_disk_pane_sorts_by_pct
 - test_format_disk_pane_tints_high_usage
 - test_split_regions_with_both_panes

 ---
 Phase 6: psf.py — Process Session Finder

 New file: psf.py at repo root

 Imports from topf (public functions only)

 psf imports only topf's public functions — no underscore-prefixed internals. For things psf needs that are private in topf (file I/O, system constants), psf reimplements thin equivalents locally:

 from topf import (parse_stat, clean_cmdline, build_tree, scan, read_uptime,
                   cores_count, read_links, fmt_bytes, fmt_count, fmt_duration,
                   fmt_pct, compress_path, compress_cmdline, Proc, SysInfo,
                   parse_meminfo, read_loadavg, count_states,
                   lifetime_secs, cpu_fraction, read_boot_id,
                   parse_proc_stat_counters, parse_vmstat_counters)

 # Local equivalents for topf internals psf needs:
 CLK_TCK = os.sysconf("SC_CLK_TCK")
 PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

 def _read_bin(path):
     """Read /proc file as bytes (for environ). Returns b'' on failure."""
     try:
         with open(path, "rb") as fh:
             return fh.read()
     except OSError:
         return b""

 def _read_text(path):
     """Read /proc file as text. Returns '' on failure."""
     try:
         with open(path) as fh:
             return fh.read()
     except OSError:
         return ""

 Architecture (sections)

 ┌───────────────────────┬────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │        Section        │                                                    Purpose                                                     │
 ├───────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ config                │ Constants: CMD_WIDTH, GLUE_COMMS, SESSION_LEADER_PATTERNS, VENV_PATH_MARKERS, CLASSIFICATION_RULES, CATEGORIES │
 ├───────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ classification        │ classify(proc) → Category — rule-based, first-match-wins                                                       │
 ├───────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ venv detection        │ read_environ(pid) → dict, detect_venv(proc) → str|None                                                         │
 ├───────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ session grouping      │ find_sessions(procs, categories) → [Session] — trace session leaders to their subtrees                         │
 ├───────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ glue summarization    │ summarize_glue(procs, categories) → [str] — collapse ssh/bash/tmux/etc.                                        │
 ├───────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ new-process detection │ find_new_clusters(procs, prev) → [str] — burst of new procs is surfaced                                        │
 ├───────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ render                │ render_psf(procs, sysinfo, args) → [str] — header + sessions + glue + new-clusters                             │
 ├───────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ CLI                   │ _parse_args, render_once_psf, main                                                                             │
 └───────────────────────┴────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

 Classification model

 ┌────────────────┬───────┬──────────┬───────────────────────────────────────────────────┐
 │    Category    │ Badge │ Priority │                     Examples                      │
 ├────────────────┼───────┼──────────┼───────────────────────────────────────────────────┤
 │ user-session   │ usr   │ 5        │ claude, vim, nvim, emacs, VS Code, python-in-venv │
 ├────────────────┼───────┼──────────┼───────────────────────────────────────────────────┤
 │ build-daemon   │ bld   │ 4        │ bazel, buck, gradle, ninja                        │
 ├────────────────┼───────┼──────────┼───────────────────────────────────────────────────┤
 │ compile-worker │ cmp   │ 3        │ clang, gcc, javac, rustc, node                    │
 ├────────────────┼───────┼──────────┼───────────────────────────────────────────────────┤
 │ infrastructure │ inf   │ 2        │ sshd, bash, zsh, tmux, login                      │
 ├────────────────┼───────┼──────────┼───────────────────────────────────────────────────┤
 │ system-service │ sys   │ 1        │ systemd, dbus, cron                               │
 ├────────────────┼───────┼──────────┼───────────────────────────────────────────────────┤
 │ misc           │ mis   │ 0        │ anything else                                     │
 └────────────────┴───────┴──────────┴───────────────────────────────────────────────────┘

 Rules are (target, regex, category_name) tuples. target is "comm" or "cmdline". First match wins. Python processes with VIRTUAL_ENV or exe containing .venv/bin/ → override from compile-worker to user-session.

 Venv detection

 - read_environ(pid) — binary read of /proc/PID/environ, split on \0, parse K=V pairs
 - detect_venv(proc) — check VIRTUAL_ENV, CONDA_PREFIX in environ, and .venv/bin/ in exe path
 - Display: venv:~/psf/.venv in the detail line

 Session grouping

 A "session" is the subtree rooted at a session leader. Session leaders are identified by comm matching patterns:
 - claude (any process named claude)
 - sshd:  (sshd session process — the one with the colon in cmdline)
 - tmux: client / tmux: server

 For each session leader, walk descendants. Infrastructure processes in the path (bash → bash → claude) are collapsed; the session shows leader → interesting children.

 Glue summarization

 Processes classified as infrastructure or system-service that aren't session leaders are summarized:
 inf: bash×5  sshd×3  tmux×2
 sys: systemd×8  dbus×2
 Not shown individually unless --show-all.

 New-process clusters

 If --watch mode: diff successive snapshots. If ≥5 processes with the same comm appeared, surface as a cluster:
 +12 new: clang (under bazel pid 42891)

 Render

 psf — load 0.43/0.41/0.76  Mem: 10G/63G  Swap: 0/0K  8 cores
 1201 procs (3 run, 1195 sleep, 3 zombie)

 [usr] 42187 claude  cpu 12%  rss:450M  up:2h15m  venv:~/psf/.venv
   ├─ 42210 python3 /home/shemer/.local/bin/topf.py  cpu 5%  rss:120M
   └─ 42215 node ...  cpu 3%  rss:80M

 [usr] 16284 tmux:client  cpu 0.1%  rss:8M  up:3d2h
   ├─ 16285 bash  (inf, summarized)
   └─ [bld] 42891 bazel  cpu 2%  rss:210M  up:45m
       └─ [cmp] clang×42  cpu 50%–400%  rss:100M–450M  up:45s–5m

 inf: bash×12  sshd×4  tmux×2
 sys: systemd×15  dbus×3

     inf: bash×12  sshd×4  tmux×2
     sys: systemd×15  dbus×3

     CLI

     psf                  # snapshot (once, like original psf)
     psf --watch          # continuous refresh (simple reprint loop, no TUI — clear screen + reprint)
     psf --no-color       # no ANSI
     psf --show-all       # show every process, not just sessions + glue
     psf --venv           # always show venv for python processes
     psf --width N        # cmdline width

     --watch mode is a simple reprint loop: clear screen, render snapshot, sleep, repeat. No cursor, no expand/interact. A full TUI can be added later if desired.

     Test coverage

     New file: tests/test_psf.py
     - test_classify_matches_known_categories — pure, synthetic Procs
     - test_classify_venv_python_overrides_to_user_session
     - test_detect_venv_from_exe_path — synthetic Proc with .venv/bin/python exe
     - test_find_sessions_groups_subtrees — synthetic tree
     - test_summarize_glue_collapses_infrastructure
     - test_render_once_psf_smoke — Linux-only, reads real /proc

     ---
     Verification

     After each phase:
     1. pytest tests/ — all existing + new tests pass
     2. python topf.py --once — verify header shows load/mem/swap
     3. python topf.py — live TUI: verify E key, d key, breakout rows under collapsed subtrees
     4. python psf.py — snapshot output with sessions, venv, glue summaries
     5. pip install -e . && topf --once && psf — verify entry points work
     6. python build.py — verify topfig still generates (topf.py constants unchanged)

     ---
     File summary

     ┌────────────────────┬────────┬────────────────────────────────────────────────────────────────┐
     │        File        │ Action │                             Phases                             │
     ├────────────────────┼────────┼────────────────────────────────────────────────────────────────┤
     │ topf.py            │ Modify │ 0–5 (bug fixes, header, swap, breakout, expand-key, disk pane) │
     ├────────────────────┼────────┼────────────────────────────────────────────────────────────────┤
     │ psf.py             │ Create │ 6 (new tool)                                                   │
     ├────────────────────┼────────┼────────────────────────────────────────────────────────────────┤
     │ pyproject.toml     │ Create │ 1 (packaging)                                                  │
     ├────────────────────┼────────┼────────────────────────────────────────────────────────────────┤
     │ .gitignore         │ Modify │ 1 (add dist/, *.egg-info)                                      │
     ├────────────────────┼────────┼────────────────────────────────────────────────────────────────┤
     │ tests/test_topf.py │ Modify │ 0–5 (tests for all topf changes)                               │
     ├────────────────────┼────────┼────────────────────────────────────────────────────────────────┤
     │ tests/test_psf.py  │ Create │ 6 (psf tests)                                                  │
     └────────────────────┴────────┴────────────────────────────────────────────────────────────────┘

  Read 1 file (ctrl+o to expand)

● Starting Phase 0: Bug fixes. Let me re-read the exact lines to make precise edits.

