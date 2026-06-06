# psf — sibling dedup, lifecycle section, path compression: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add render-time sibling dedup (`×N` groups), a system-wide born/died section measured over the sample window, and cwd/exe path compression to `psf.py`.

**Architecture:** Three orthogonal additions to the existing pure-core/IO-shell pipeline. Dedup and path compression are pure render-time string transforms. The lifecycle section is fed by restructuring `main()` to bracket the existing sample sleep with two full `scan()`s, reusing those two snapshots for both current-CPU and a `(pid, starttime)` set-diff. Every new behavior is a small pure function unit-tested before wiring.

**Tech Stack:** Python 3.12 stdlib only. Tests via stdlib `unittest`. Reference spec: `docs/superpowers/specs/2026-06-06-psf-grouping-lifecycle-paths-design.md`. Run all tests from the repo root `/home/shemer/backup` with `python3 -m unittest tests.test_psf -v`.

---

## File Structure

- Modify: `psf.py` — config block, new pure functions (`compress_path`, `brace_summary`, `range_str`, `group_siblings`, `diff_snapshots`, `format_lifecycle`), render refactor, `main()` restructure.
- Modify: `tests/test_psf.py` — new test classes appended; no existing tests change.

Order of tasks follows the dependency chain: leaf pure functions first (path, helpers), then the grouping core, then render integration, then the snapshot diff + lifecycle formatter, then the `main()` wiring that ties it together.

---

### Task 1: Path compression (`compress_path`)

Replaces the existing `_summarize_path` (HOME-only) with full compression. `_summarize_path` has exactly one caller, `_detail`.

**Files:**
- Modify: `psf.py` (config block; `_summarize_path` → `compress_path`; `_detail` call site)
- Test: `tests/test_psf.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_psf.py`:

```python
# --- path compression -------------------------------------------------------


class TestCompressPath(unittest.TestCase):
    def test_home_summary_and_middle_elision(self):
        p = psf.HOME + "/mss/.claude/worktrees/multimlamp"
        self.assertEqual(psf.compress_path(p), "~/mss/../multimlamp")

    def test_deleted_marker_preserved(self):
        p = psf.HOME + "/mss/.claude/worktrees/multimlamp (deleted)"
        self.assertEqual(psf.compress_path(p), "~/mss/../multimlamp (deleted)")

    def test_no_middle_unchanged(self):
        self.assertEqual(psf.compress_path(psf.HOME + "/mss/foo"), "~/mss/foo")

    def test_absolute_path_elided(self):
        self.assertEqual(psf.compress_path("/usr/lib/a/b/c"), "/usr/../c")

    def test_long_basename_compressed(self):
        name = "verylongnamethatgoesonandonandon_final"   # 38 chars
        self.assertEqual(psf.compress_path(name), "verylo..._final")

    def test_question_mark_passthrough(self):
        self.assertEqual(psf.compress_path("?"), "?")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_psf.TestCompressPath -v`
Expected: FAIL — `AttributeError: module 'psf' has no attribute 'compress_path'`.

- [ ] **Step 3: Add config constants**

In `psf.py`, in the config block (after `SAMPLE_INTERVAL = 0.2`), add:

```python
PATH_HEAD = 2             # leading path components kept when compressing
PATH_MAX_BASENAME = 30    # basename length above which it is itself shortened
PATH_BASENAME_KEEP = 6    # chars kept each side of '...' in a long basename
```

- [ ] **Step 4: Replace `_summarize_path` with `compress_path`**

In `psf.py`, replace the existing function:

```python
def _summarize_path(path):
    if path and path.startswith(HOME):
        return "~" + path[len(HOME):]
    return path
```

with:

```python
def _compress_basename(name):
    if len(name) <= PATH_MAX_BASENAME:
        return name
    k = PATH_BASENAME_KEEP
    return name[:k] + "..." + name[-k:]


def compress_path(path):
    """Shorten a cwd/exe for display: $HOME -> ~, elide the middle of deep
    paths (keep the first PATH_HEAD components + '..' + basename), and shorten
    an over-long basename to 'prefix...suffix'. A trailing ' (deleted)' marker
    (kernel-appended for a removed cwd) is preserved."""
    if not path or path == "?":
        return path
    marker = " (deleted)"
    suffix = ""
    if path.endswith(marker):
        path = path[:-len(marker)]
        suffix = marker
    if path.startswith(HOME):
        path = "~" + path[len(HOME):]
    parts = path.split("/")
    parts[-1] = _compress_basename(parts[-1])
    if len(parts) > PATH_HEAD + 1:
        parts = parts[:PATH_HEAD] + ["..", parts[-1]]
    return "/".join(parts) + suffix
```

> `HOME` is already defined in `psf.py` (just above the old `_summarize_path`). Keep its definition where it is; `compress_path` must be defined after it.

- [ ] **Step 5: Update the caller in `_detail`**

In `psf.py`, in `_detail`, change the two call sites:

```python
    if node.cwd and node.cwd not in ("?", ""):
        bits.append("cwd:" + _summarize_path(node.cwd))
    if node.exe and node.exe not in ("?", ""):
        bits.append("exe:" + _summarize_path(node.exe))
```

to:

```python
    if node.cwd and node.cwd not in ("?", ""):
        bits.append("cwd:" + compress_path(node.cwd))
    if node.exe and node.exe not in ("?", ""):
        bits.append("exe:" + compress_path(node.exe))
```

- [ ] **Step 6: Run the full suite**

Run: `python3 -m unittest tests.test_psf -v`
Expected: PASS — new `TestCompressPath` passes; `TestRender.test_render_tree_lines` still passes (`/home/shemer/proj` → `~/proj`, `/usr/bin/node` → `/usr/../node`, still contains `node`).

- [ ] **Step 7: Commit**

```bash
git add psf.py tests/test_psf.py
git commit -m "feat(psf): compress cwd/exe paths (head/../basename, ellipsis basename)"
```

---

### Task 2: Group display helpers (`brace_summary`, `range_str`)

**Files:**
- Modify: `psf.py`
- Test: `tests/test_psf.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_psf.py`:

```python
# --- group display helpers --------------------------------------------------


class TestGroupHelpers(unittest.TestCase):
    def test_brace_summary_common_prefix(self):
        self.assertEqual(psf.brace_summary(["~/a", "~/b"]), "~/{a,b}")

    def test_brace_summary_single_value(self):
        self.assertEqual(psf.brace_summary(["~/only"]), "~/only")

    def test_brace_summary_truncates(self):
        vals = ["p/%d" % i for i in range(10)]
        out = psf.brace_summary(vals, max_items=3)
        self.assertTrue(out.startswith("p/{"))
        self.assertIn(",...}", out)

    def test_range_str_low_high(self):
        self.assertEqual(psf.range_str([1024, 2048], psf.fmt_bytes), "1.0K–2.0K")

    def test_range_str_equal_is_single(self):
        self.assertEqual(psf.range_str([5, 5], str), "5")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_psf.TestGroupHelpers -v`
Expected: FAIL — `AttributeError: ... 'brace_summary'`.

- [ ] **Step 3: Implement**

In `psf.py`, add to the resource-stats section (after `fmt_duration`):

```python
def brace_summary(values, max_items=5):
    """Collapse strings to a common prefix + brace of the differing tails:
    ['a/x', 'a/y'] -> 'a/{x,y}'. A single distinct value is returned as-is."""
    uniq = sorted(set(values))
    if len(uniq) == 1:
        return uniq[0]
    prefix = os.path.commonprefix(uniq)
    tails = [v[len(prefix):] for v in uniq]
    shown = tails[:max_items]
    more = ",..." if len(tails) > max_items else ""
    return "%s{%s%s}" % (prefix, ",".join(shown), more)


def range_str(values, fmt):
    """'lo–hi' (en-dash) via fmt(); a single value when min == max. `values`
    must be non-empty."""
    lo, hi = min(values), max(values)
    if lo == hi:
        return fmt(lo)
    return "%s–%s" % (fmt(lo), fmt(hi))
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_psf.TestGroupHelpers -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add psf.py tests/test_psf.py
git commit -m "feat(psf): brace_summary and range_str group-display helpers"
```

---

### Task 3: Sibling grouping (`group_siblings`)

**Files:**
- Modify: `psf.py` (config block; new `Group` type + function)
- Test: `tests/test_psf.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_psf.py`:

```python
# --- sibling grouping -------------------------------------------------------


class TestGroupSiblings(unittest.TestCase):
    def _kids(self):
        # three identical (comm, exe) sshd siblings + one bash
        out = []
        for pid in (10, 11, 12):
            p = _p(pid, 1, comm="sshd", cmdline="sshd: shemer@pts/%d" % pid)
            p.exe = "/usr/sbin/sshd"
            out.append(p)
        b = _p(20, 1, comm="bash", cmdline="-bash")
        b.exe = "/usr/bin/bash"
        out.append(b)
        return out

    def test_forms_group_at_min(self):
        items = psf.group_siblings(self._kids(), dedup_min=3,
                                   never_merge=frozenset())
        groups = [it for it in items if isinstance(it, psf.Group)]
        singles = [it for it in items if isinstance(it, psf.Proc)]
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].members), 3)
        self.assertEqual([s.pid for s in singles], [20])    # bash stays single

    def test_pair_below_min_stays_individual(self):
        kids = self._kids()[:2] + self._kids()[3:]          # 2 sshd + bash
        items = psf.group_siblings(kids, dedup_min=3, never_merge=frozenset())
        self.assertTrue(all(isinstance(it, psf.Proc) for it in items))

    def test_never_merge_keeps_focus_procs_separate(self):
        kids = []
        for pid in (30, 31, 32):
            c = _p(pid, 1, comm="claude", cmdline="claude")
            c.exe = "/usr/bin/node"
            kids.append(c)
        items = psf.group_siblings(kids, dedup_min=3,
                                   never_merge=frozenset({"claude"}))
        self.assertTrue(all(isinstance(it, psf.Proc) for it in items))

    def test_dedup_min_none_disables(self):
        items = psf.group_siblings(self._kids(), dedup_min=None,
                                   never_merge=frozenset())
        self.assertTrue(all(isinstance(it, psf.Proc) for it in items))

    def test_ordering_stable_by_pid(self):
        items = psf.group_siblings(self._kids(), dedup_min=3,
                                   never_merge=frozenset())
        # group (min pid 10) before bash (pid 20)
        firsts = [it.members[0].pid if isinstance(it, psf.Group) else it.pid
                  for it in items]
        self.assertEqual(firsts, sorted(firsts))
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_psf.TestGroupSiblings -v`
Expected: FAIL — `AttributeError: ... 'Group'` / `'group_siblings'`.

- [ ] **Step 3: Add config constants**

In `psf.py` config block (anywhere among the top-of-file constants; placement is cosmetic), add:

```python
DEDUP_MIN = 3             # min identical siblings to merge into one ×N group
NEVER_MERGE = frozenset({"claude"})   # comms never grouped, even when identical
```

- [ ] **Step 4: Implement**

In `psf.py`, near the `SysInfo`/`Stat` namedtuples (after the `Stat` definition), add:

```python
# A merged set of >= DEDUP_MIN near-identical sibling Procs.
Group = namedtuple("Group", "members")
```

Then, in the pure-core selection section (after `select`, before `collapse`), add:

```python
def group_siblings(procs, dedup_min, never_merge):
    """Partition a node's visible sibling Procs by (comm, exe) into a render
    list. A partition with >= dedup_min members whose comm is not in
    never_merge becomes a Group; everything else stays an individual Proc.
    dedup_min falsy (None/0) disables grouping. Items are ordered by their
    smallest pid so output is stable."""
    if not dedup_min:
        return list(procs)
    buckets = {}
    order = []
    for p in procs:
        key = (p.comm, p.exe)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(p)
    items = []
    for key in order:
        members = buckets[key]
        if len(members) >= dedup_min and key[0] not in never_merge:
            items.append(Group(members=members))
        else:
            items.extend(members)
    items.sort(key=lambda it: min(m.pid for m in it.members)
               if isinstance(it, Group) else it.pid)
    return items
```

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m unittest tests.test_psf.TestGroupSiblings -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add psf.py tests/test_psf.py
git commit -m "feat(psf): group_siblings — partition siblings by (comm, exe)"
```

---

### Task 4: Render integration (group header, group detail, recursion)

Rewrite `render()` to consume `group_siblings` output and recurse over the union of group members' children. Adds `_group_label`, `_group_detail`, `_visible_children`.

**Files:**
- Modify: `psf.py` (config block; `render` and helpers)
- Test: `tests/test_psf.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_psf.py`:

```python
# --- group rendering --------------------------------------------------------


class TestGroupRender(unittest.TestCase):
    def _tree(self):
        # sshd -D with 3 identical sshd@pts children, each with a tmux child
        procs = {1: _p(1, 0, comm="sshd", cmdline="sshd: /usr/sbin/sshd -D")}
        procs[1].exe = "/usr/sbin/sshd"
        tmux_pid = 100
        for i, pts in enumerate((0, 7, 8)):
            sp = _p(10 + i, 1, comm="sshd", cmdline="sshd: shemer@pts/%d" % pts)
            sp.exe = "/usr/sbin/sshd"
            procs[10 + i] = sp
            tp = _p(tmux_pid, 10 + i, comm="tmux",
                    cmdline="tmux attach-session -t s%d" % pts)
            tp.exe = "/usr/bin/tmux"
            procs[tmux_pid] = tp
            tmux_pid += 1
        psf.build_tree(procs)
        for p in procs.values():
            p.kept = True
        return procs

    def test_group_header_and_recursion(self):
        procs = self._tree()
        lines = psf.render([procs[1]], suppressed=set(), width=80, color=False,
                           dedup_min=3, never_merge=frozenset())
        text = "\n".join(lines)
        self.assertIn("×3", text)                       # sshd@pts merged
        self.assertIn("sshd: shemer@pts/{", text)       # brace label
        self.assertIn("pids:10 11 12", text)            # member pids on detail
        # recursion: the 3 tmux children of the group are themselves merged
        self.assertIn("tmux attach-session -t {", text)

    def test_group_detail_shows_path_and_ranges(self):
        procs = self._tree()
        for i, pid in enumerate((10, 11, 12)):
            procs[pid].cwd = "/home/shemer/proj%d" % i
            procs[pid].rss_pages = 1000 + i * 1000      # differing rss
            procs[pid].starttime = 1000
        sysinfo = psf.SysInfo(clk_tck=100, page_size=4096, uptime=1100.0)
        lines = psf.render([procs[1]], suppressed=set(), width=80, color=False,
                           sysinfo=sysinfo, dedup_min=3, never_merge=frozenset())
        text = "\n".join(lines)
        self.assertIn("cwd:~/{proj0,proj1,proj2}", text)  # compressed + braced
        self.assertIn("rss:", text)
        self.assertIn("–", text)                          # an en-dash range

    def test_no_dedup_renders_individually(self):
        procs = self._tree()
        lines = psf.render([procs[1]], suppressed=set(), width=80, color=False,
                           dedup_min=None, never_merge=frozenset())
        self.assertFalse(any("×3" in ln for ln in lines))
        self.assertIn("shemer@pts/0", "\n".join(lines))   # each shown
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_psf.TestGroupRender -v`
Expected: FAIL — `render()` got an unexpected keyword argument `dedup_min`.

- [ ] **Step 3: Add config constant**

In `psf.py` config block (after `NEVER_MERGE`), add:

```python
GROUP_PIDS = 8            # member pids listed on a group's detail line
```

- [ ] **Step 4: Implement helpers + rewrite `render`**

In `psf.py`, replace the entire existing `render` function with the following (and add the three helpers just above it, after `_detail`):

```python
def _visible_children(node, suppressed):
    return [c for c in node.children if c.kept and c.pid not in suppressed]


def _group_label(members, width):
    return brace_summary([m.cmdline for m in members])[:width]


def _group_detail(members, color, sysinfo):
    """Aggregated detail line for a merged group: shared/braced cwd & exe,
    member pids, and cpu/rss/up ranges (when sysinfo is given)."""
    bits = []
    for attr, prefix in (("cwd", "cwd:"), ("exe", "exe:")):
        vals = [getattr(m, attr) for m in members
                if getattr(m, attr) and getattr(m, attr) not in ("?", "")]
        if vals:
            bits.append(prefix + brace_summary([compress_path(v) for v in vals]))
    pids = sorted(m.pid for m in members)
    extra = " +%d" % (len(pids) - GROUP_PIDS) if len(pids) > GROUP_PIDS else ""
    bits.append("pids:" + " ".join(str(x) for x in pids[:GROUP_PIDS]) + extra)
    if sysinfo is not None:
        avgs = [a for a in (
            cpu_fraction(m.utime + m.stime,
                         lifetime_secs(m.starttime, sysinfo.uptime,
                                       sysinfo.clk_tck), sysinfo.clk_tck)
            for m in members) if a is not None]
        if avgs:
            curs = [m.cpu_current for m in members if m.cpu_current is not None]
            if curs:
                bits.append("cpu %s (%s avg)" % (range_str(curs, fmt_pct),
                                                 range_str(avgs, fmt_pct)))
            else:
                bits.append("cpu %s avg" % range_str(avgs, fmt_pct))
        rss = [m.rss_pages * sysinfo.page_size for m in members
               if m.rss_pages > 0]
        if rss:
            bits.append("rss:" + range_str(rss, fmt_bytes))
        lifes = [lifetime_secs(m.starttime, sysinfo.uptime, sysinfo.clk_tck)
                 for m in members]
        bits.append("up:" + range_str(lifes, fmt_duration))
    threads = max(m.num_threads for m in members)
    if threads > 1:
        bits.append("%d threads" % threads)
    line = "  ".join(bits)
    return "\x1b[2m%s\x1b[0m" % line if color else line


def render(roots, suppressed, width=CMD_WIDTH, color=None, sysinfo=None,
           dedup_min=None, never_merge=frozenset()):
    """Render kept Procs as an ascii tree. With sysinfo, detail lines carry
    cpu/rss/elapsed. With dedup_min, near-identical sibling subtrees merge into
    one ×N entry that recurses over the union of members' children."""
    if color is None:
        color = False
    lines = []

    def walk_items(items, prefix):
        for i, item in enumerate(items):
            is_last = i == len(items) - 1
            connector = "" if prefix == "" and is_last else (
                "└─ " if is_last else "├─ ")
            child_prefix = prefix + ("   " if is_last else "│  ")
            if isinstance(item, Group):
                head = "%s%s×%d %s" % (prefix, connector, len(item.members),
                                       _group_label(item.members, width))
                lines.append(head)
                detail = _group_detail(item.members, color, sysinfo)
                if detail is not None:
                    lines.append(child_prefix + detail)
                kids = [c for m in item.members
                        for c in _visible_children(m, suppressed)]
            else:
                head = "%s%s%d %s" % (prefix, connector, item.pid,
                                      item.cmdline[:width])
                lines.append(head)
                detail = _detail(item, color, sysinfo)
                if detail is not None:
                    lines.append(child_prefix + detail)
                kids = _visible_children(item, suppressed)
            walk_items(group_siblings(kids, dedup_min, never_merge), child_prefix)
            if isinstance(item, Proc) and item.collapsed and item.collapse_note:
                lines.append(child_prefix + item.collapse_note)

    walk_items(group_siblings(list(roots), dedup_min, never_merge), "")
    return lines
```

> The old `render` walked each root with `is_last=True` (no connector). The new
> top-level `walk_items` does the same for a single root (the common case: one
> kept root). Multiple kept roots now get `├─/└─` connectors — a cosmetic change
> with no test dependency.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_psf -v`
Expected: PASS — `TestGroupRender` passes; all existing `TestRender` tests still pass (they use distinct comms / `dedup_min=None`, so no grouping).

- [ ] **Step 6: Commit**

```bash
git add psf.py tests/test_psf.py
git commit -m "feat(psf): render ×N sibling groups with aggregated detail lines"
```

---

### Task 5: Snapshot diff (`diff_snapshots`)

**Files:**
- Modify: `psf.py`
- Test: `tests/test_psf.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_psf.py`:

```python
# --- lifecycle diff ---------------------------------------------------------


class TestDiffSnapshots(unittest.TestCase):
    def test_born_and_died(self):
        before = {1: _p(1, 0), 2: _p(2, 1, comm="old")}
        after = {1: _p(1, 0), 3: _p(3, 1, comm="new")}
        born, died = psf.diff_snapshots(before, after)
        self.assertEqual([p.pid for p in born], [3])
        self.assertEqual([p.pid for p in died], [2])

    def test_pid_reuse_counts_as_both(self):
        # same pid, different starttime -> the old one died and a new one was born
        before = {5: _p(5, 1, comm="a", starttime=100)}
        after = {5: _p(5, 1, comm="b", starttime=200)}
        born, died = psf.diff_snapshots(before, after)
        self.assertEqual([p.comm for p in born], ["b"])
        self.assertEqual([p.comm for p in died], ["a"])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_psf.TestDiffSnapshots -v`
Expected: FAIL — `AttributeError: ... 'diff_snapshots'`.

- [ ] **Step 3: Implement**

In `psf.py`, add to the resource-stats section (after `range_str`):

```python
def diff_snapshots(before, after):
    """Given two {pid: Proc} snapshots, return (born, died) Proc lists keyed by
    (pid, starttime) so a reused PID is treated as a death + a birth. born are
    drawn from `after`, died from `before`. Each list is sorted by pid."""
    def keyed(snap):
        return {(p.pid, p.starttime): p for p in snap.values()}
    kb, ka = keyed(before), keyed(after)
    born = [ka[k] for k in ka if k not in kb]
    died = [kb[k] for k in kb if k not in ka]
    born.sort(key=lambda p: p.pid)
    died.sort(key=lambda p: p.pid)
    return born, died
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_psf.TestDiffSnapshots -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add psf.py tests/test_psf.py
git commit -m "feat(psf): diff_snapshots — born/died by (pid, starttime)"
```

---

### Task 6: Lifecycle formatter (`format_lifecycle`)

**Files:**
- Modify: `psf.py` (config block; `_dominant_parent`, `_lifecycle_side`, `format_lifecycle`)
- Test: `tests/test_psf.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_psf.py`:

```python
# --- lifecycle formatting ---------------------------------------------------


class TestFormatLifecycle(unittest.TestCase):
    SYS = None  # set in setUp

    def setUp(self):
        self.SYS = psf.SysInfo(clk_tck=100, page_size=4096, uptime=1100.0)

    def test_empty_is_no_section(self):
        self.assertEqual(psf.format_lifecycle([], [], {}, self.SYS, 0.2), [])

    def test_born_grouped_with_parent(self):
        born = [_p(pid, 999, comm="cc1plus") for pid in (10, 11, 12)]
        parents = {999: "bazel"}
        out = psf.format_lifecycle(born, [], parents, self.SYS, 0.21)
        text = "\n".join(out)
        self.assertIn("system-wide", text)
        self.assertIn("0.21s", text)
        self.assertIn("×3 cc1plus", text)
        self.assertIn("(←bazel)", text)

    def test_died_singleton_shows_lived(self):
        # started at tick 99999, uptime 1100 -> ~100s alive
        dead = _p(500, 1, comm="git", starttime=99999)
        out = psf.format_lifecycle([], [dead], {1: "bash"}, self.SYS, 0.2)
        text = "\n".join(out)
        self.assertIn("-500 git", text)
        self.assertIn("lived 1m40s", text)

    def test_cap_with_more(self):
        born = [_p(1000 + i, 1, comm="c%03d" % i) for i in range(psf.LIFECYCLE_MAX + 5)]
        out = psf.format_lifecycle(born, [], {}, self.SYS, 0.2)
        self.assertIn("+5 more", "\n".join(out))
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_psf.TestFormatLifecycle -v`
Expected: FAIL — `AttributeError: ... 'format_lifecycle'` / `'LIFECYCLE_MAX'`.

- [ ] **Step 3: Add config constant**

In `psf.py` config block (after `GROUP_PIDS`), add:

```python
LIFECYCLE_MAX = 40        # max born (and max died) comm-groups listed
```

- [ ] **Step 4: Implement**

In `psf.py`, add after `diff_snapshots`:

```python
def _dominant_parent(members, parents):
    """The most common parent comm among members (None if unknown)."""
    counts = Counter(parents.get(m.ppid) for m in members)
    counts.pop(None, None)
    return counts.most_common(1)[0][0] if counts else None


def _lifecycle_side(procs, sign, parents, sysinfo, is_died):
    """One 'born'/'died' line body: group by comm (×N), largest first, each
    annotated with its dominant parent comm; singletons show pid + cmdline and,
    for deaths, how long they had lived. Capped at LIFECYCLE_MAX comm-groups."""
    buckets = {}
    for p in procs:
        buckets.setdefault(p.comm, []).append(p)
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    parts = []
    for comm, members in ordered[:LIFECYCLE_MAX]:
        pcomm = _dominant_parent(members, parents)
        tag = " (←%s)" % pcomm if pcomm else ""
        if len(members) >= 2:
            parts.append("×%d %s%s" % (len(members), comm, tag))
        else:
            p = members[0]
            lived = ""
            if is_died:
                lived = " lived %s" % fmt_duration(
                    lifetime_secs(p.starttime, sysinfo.uptime, sysinfo.clk_tck))
            parts.append("%s%d %s%s%s" % (sign, p.pid, comm, tag, lived))
    if len(ordered) > LIFECYCLE_MAX:
        parts.append("+%d more" % (len(ordered) - LIFECYCLE_MAX))
    return "  ".join(parts)


def format_lifecycle(born, died, parents, sysinfo, dt, color=False):
    """The born/died section: a header naming the measured window, then a
    'born:' and/or 'died:' line. `parents` maps pid -> comm. Returns [] when
    nothing changed in the window."""
    if not born and not died:
        return []
    lines = ["lifecycle — system-wide, %.2gs window:" % dt]
    if born:
        lines.append("  born:  " + _lifecycle_side(born, "+", parents,
                                                    sysinfo, False))
    if died:
        lines.append("  died:  " + _lifecycle_side(died, "-", parents,
                                                    sysinfo, True))
    if color:
        lines = ["\x1b[2m%s\x1b[0m" % ln for ln in lines]
    return lines
```

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m unittest tests.test_psf.TestFormatLifecycle -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add psf.py tests/test_psf.py
git commit -m "feat(psf): format_lifecycle — grouped born/died section"
```

---

### Task 7: `main()` two-scan restructure, flags, glossary, smoke test

Wire everything: bracket the sample sleep with two `scan()`s, derive current-CPU and born/died from them, add flags, extend the glossary, print the lifecycle section. Remove the now-unused `sample_current_cpu`/`read_cpu_ticks` (superseded by the two-snapshot approach).

**Files:**
- Modify: `psf.py` (`glossary`, delete `sample_current_cpu`/`read_cpu_ticks`, add `_parents_map`, rewrite `main`)
- Test: manual live smoke run (the full pipeline; the pure pieces are already unit-tested)

- [ ] **Step 1: Extend the glossary**

In `psf.py`, in `glossary`, replace the `lines = [ ... ]` list with:

```python
    lines = [
        "psf — interesting process subtrees only; the dim line under each "
        "process annotates it.",
        "  sockets: LISTEN :PORT = listening   "
        "+N est = N established TCP connections   unix:PATH = named socket",
        "  stats:   cpu X% (Y avg) = recent / lifetime-average CPU   "
        "rss = resident memory   up = time since start",
        "  groups:  ×N = N near-identical siblings merged (pids/ranges on the "
        "detail line)   lifecycle = procs born/died during the sample window",
    ]
```

- [ ] **Step 2: Delete the superseded samplers and add `_parents_map`**

In `psf.py`, delete these two functions entirely (added in the prior CPU-rate change, now unused — current CPU comes from the two scans):

```python
def read_cpu_ticks(pid):
    ...

def sample_current_cpu(nodes, interval, clk_tck=CLK_TCK):
    ...
```

In their place, add:

```python
def _parents_map(*snaps):
    """pid -> comm across one or more {pid: Proc} snapshots (for annotating the
    parent of a born/died process)."""
    out = {}
    for snap in snaps:
        for p in snap.values():
            out[p.pid] = p.comm
    return out
```

- [ ] **Step 3: Add the new CLI flags**

In `psf.py`, in `main`, after the existing `--sample-interval` argument, add:

```python
    ap.add_argument("--no-dedup", action="store_true",
                    help="do not merge near-identical sibling subtrees")
    ap.add_argument("--dedup-min", type=int, default=DEDUP_MIN,
                    help="min identical siblings to merge (default %d)"
                         % DEDUP_MIN)
    ap.add_argument("--no-lifecycle", action="store_true",
                    help="suppress the born/died section")
```

- [ ] **Step 4: Rewrite the body of `main`**

In `psf.py`, replace everything in `main` from `procs = scan()` through the final `print("\n".join(out))` with:

```python
    s_a = scan()
    t_a = time.monotonic()
    roots = build_tree(s_a)
    select(s_a, DEFAULT_MATCHERS)
    suppressed = collapse(s_a, threshold=args.threshold)
    visible_roots = [r for r in roots if r.kept]
    printed = [n for n in collect_printed(visible_roots, suppressed) if n.kept]

    born, died, s_b, dt = [], [], None, 0.0
    if args.sample_interval > 0:
        time.sleep(args.sample_interval)
        s_b = scan()
        dt = time.monotonic() - t_a
        for n in printed:                      # current CPU over the same window
            other = s_b.get(n.pid)
            if other is not None and other.starttime == n.starttime:
                n.cpu_current = cpu_fraction(
                    (other.utime + other.stime) - (n.utime + n.stime),
                    dt, CLK_TCK)
        born, died = diff_snapshots(s_a, s_b)

    if args.no_cache:
        cache = Cache(os.devnull, boot_id="", now=time.time())
    else:
        cache = Cache(cache_path(), boot_id=read_boot_id(), now=time.time())
    probe(printed, cache)
    if not args.no_cache:
        cache.save(live_keys={(p.pid, p.starttime) for p in s_a.values()})

    color = sys.stdout.isatty() and not args.no_color
    sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE, uptime=read_uptime())
    dedup_min = None if args.no_dedup else args.dedup_min

    out = []
    if not args.no_glossary:
        out += glossary(color) + [""]
    out += render(visible_roots, suppressed, width=args.width, color=color,
                  sysinfo=sysinfo, dedup_min=dedup_min, never_merge=NEVER_MERGE)
    if s_b is not None and not args.no_lifecycle:
        section = format_lifecycle(born, died, _parents_map(s_a, s_b),
                                   sysinfo, dt, color=color)
        if section:
            out += [""] + section
    print("\n".join(out))

    hidden = sum(1 for p in s_a.values() if not p.kept)
    kthreads = sum(1 for p in s_a.values() if p.ppid == 2 or p.pid == 2)
    sys.stderr.write("(hidden: %d procs, %d kernel threads)\n"
                     % (hidden, kthreads))
```

> The trailing `hidden`/`kthreads` footer lines already exist after the old
> `print`; this rewrite reproduces them against `s_a`. Ensure no duplicate
> footer remains.

- [ ] **Step 5: Run the full unit suite**

Run: `python3 -m unittest tests.test_psf -v`
Expected: PASS (all classes). No reference to the deleted `sample_current_cpu`.

- [ ] **Step 6: Live smoke — dedup + paths + lifecycle**

```bash
python3 psf.py --no-color | head -40
```
Expected: glossary (4 lines incl. the `×N`/lifecycle line); a tree where repeated sshd/tmux chains collapse to `×N` entries with `pids:`/`rss:`/`up:` ranges; `cwd:`/`exe:` shown in `~/a/../b` form; and (if anything started/stopped in ~0.2s) a `lifecycle — system-wide, …s window:` section at the end. No tracebacks.

- [ ] **Step 7: Live smoke — toggles**

```bash
python3 psf.py --no-color --no-dedup --no-lifecycle | head -20
python3 psf.py --no-color -s 0 | head -20
```
Expected: first shows the old fully-expanded tree with no lifecycle section; second additionally drops current-CPU (`cpu X% avg` only) and the lifecycle section (no sleep, no second scan).

- [ ] **Step 8: Live smoke — births/deaths under load**

```bash
# in one shell: generate short-lived process churn
( for i in $(seq 1 200); do /bin/true; sleep 0.01; done ) &
python3 psf.py --no-color | sed -n '/lifecycle/,$p'
wait
```
Expected: a `lifecycle` section listing `true`/`sh` born and/or died (or grouped `×N`), annotated with the parent comm. (If your shell isn't inside a kept subtree, the churn still appears — the section is system-wide.)

- [ ] **Step 9: Commit**

```bash
git add psf.py
git commit -m "feat(psf): two-scan window for current-CPU + system-wide lifecycle section"
```

---

## Self-Review

**Spec coverage:**
- Sibling dedup, key `(comm, exe)`, `DEDUP_MIN=3`, `NEVER_MERGE={claude}` → Tasks 3, 4.
- Recurse over union of members' children → Task 4 (`walk_items` pools `_visible_children`).
- Group header (brace-summarized cmdline) + aggregated detail (pids, rss/cpu/up ranges, braced cwd/exe) → Task 4 (`_group_label`, `_group_detail`).
- Two-scan window shared by current-CPU and lifecycle; measured `dt` → Task 7.
- `diff_snapshots` by `(pid, starttime)` → Task 5.
- System-wide born/died, comm-grouped, parent annotation, `lived`, `LIFECYCLE_MAX` cap + `+M more` → Task 6.
- Path compression: head/`..`/basename, `...`-ellipsis long basename, ` (deleted)` preserved → Task 1.
- Flags `--no-dedup`, `--dedup-min`, `--no-lifecycle`; glossary update → Task 7.
- `-s 0` disables sleep → no current-CPU, no lifecycle → Task 7.
- Known limitation (born+died within window) → documented in spec; no code path needed.

**Placeholder scan:** none — every code step shows complete code; every run step shows the command and expected result.

**Type consistency:** `Group = namedtuple("Group", "members")` (Task 3) is consumed by `render`/`_group_label`/`_group_detail` via `isinstance(item, Group)` / `item.members` (Task 4). `group_siblings(procs, dedup_min, never_merge)` signature is identical in Task 3 and every Task-4 call site. `compress_path` (Task 1) is used by `_detail` (Task 1) and `_group_detail` (Task 4). `diff_snapshots(before, after)` (Task 5) and `format_lifecycle(born, died, parents, sysinfo, dt, color)` (Task 6) match their `main` call sites (Task 7). `SysInfo(clk_tck, page_size, uptime)`, `cpu_fraction`, `lifetime_secs`, `fmt_pct`, `fmt_bytes`, `fmt_duration` are all pre-existing and used with their established signatures. `range_str(values, fmt)` and `brace_summary(values, max_items)` (Task 2) match Task-4/Task-6 usage.

**Note for executor:** Config constants are introduced across tasks (PATH_* in Task 1, DEDUP_MIN/NEVER_MERGE in Task 3, GROUP_PIDS in Task 4, LIFECYCLE_MAX in Task 6) — add each where its task says, all within the existing top-of-file config block. Tasks 1–6 are pure and independently testable; Task 7 is the only one requiring a live run.
