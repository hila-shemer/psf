# topf — windowed-CPU live viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the one-shot `psf` snapshot tool into `topf`, a full-screen, continuously-sampling `top`-like viewer that keeps the focused/deduped/collapsed tree and adds windowed CPU, resource promotion, top-like ordering, and a piped `--once` fallback.

**Architecture:** Keep one self-contained, stdlib-only file (`git mv psf.py topf.py`). All new logic lands as *pure* functions (windowed rate, history ring, promotion, subtree ordering, ANSI-aware truncate, frame clipping) that are unit-tested; the live TUI is a thin imperative shell (`run_live`) over `render()` using raw ANSI + `termios` cbreak + `select`. Both live and `--once` modes feed the same `history`-ring → `windowed_rate` code path.

**Tech Stack:** Python 3, stdlib only (`os`, `re`, `time`, `termios`, `tty`, `select`, `argparse`, `json`); pytest for tests.

---

## Design decisions resolved during spec review (read before starting)

These were ambiguous in the design doc and are now fixed. Implement exactly as stated:

1. **Cooling = per-window tint only.** There is **no** separate "dim the cooling row" concept. Each of the (up to 3) cpu numbers in the headline carries its own tint via `_tint_level`; a cooling proc shows e.g. a dim 2s figure next to a hot 60s figure. Do **not** add a `cooling` flag to `Proc` or special-case the head line.
2. **Top-level ordering metric = sum over ALL descendants.** A subtree's sort weight is the sum of the active-window CPU over the root proc **and every descendant** (including suppressed/collapsed ones), so a heavily-collapsed busy subtree still ranks by its true total. Sort descending, stable pid tiebreak.
3. **`--once` cpu headline = shortest window + lifetime avg.** In `--once`/piped mode (only ~2 samples), show the one real windowed figure plus the old lifetime average as a labelled trailing bit, e.g. `cpu 42% (3.1% avg)`. In live mode there is no avg.
4. **Window references are positional, not hardcoded durations.** The RSS-only-promotion gate keys off the **longest** window (`cpu_windows[-1]`); ordering keys off the **active** window (the `w`-selected index, default 0 = shortest). Never hardcode "2s"/"60s" in logic — only `DEFAULT_WINDOWS` holds the default numbers.
5. **Promotion sets `.interesting`** (not just `.kept`) so promoted procs survive `collapse()` and get deep-probed, exactly like matched procs.
6. **`prev` snapshot is dropped.** The history ring is the single source of truth; the shortest window's rate *is* the per-frame diff. There is no separate `cpu_current` field anymore.

## Shared signatures (keep these names/types exact across all tasks)

- `update_history(history, procs, now, longest_window) -> None` — mutates `history` (dict `(pid, starttime) -> list[(t, ticks)]`).
- `windowed_rate(ring, window, clk_tck) -> float | None` — `ring` is `list[(t, ticks)]` ascending; result in cores.
- `compute_windows(procs, history, windows, clk_tck) -> None` — sets `proc.cpu_windows` (a `list` aligned to `windows`).
- `is_promoted(proc, page_size, promote_level, rss_needs_cpu, is_kthread) -> bool`
- `select(procs, matchers, page_size, promote_level, rss_needs_cpu) -> None` — extended signature.
- `subtree_window_cpu(node, widx) -> float`
- `visible_truncate(s, width) -> str`
- `clip_frame(lines, rows, cols) -> list[str]`
- `parse_windows(text) -> tuple[float, ...]`
- `_cpu_bit(windows_fracs, avg_frac=None) -> (str, int)`

## File structure

- `topf.py` (renamed from `psf.py`) — the whole tool.
- `conftest.py` (new, repo root, empty) — puts the repo root on `sys.path` so `import topf` works under pytest.
- `tests/test_topf.py` (new) — all unit tests for the pure logic.

---

## Task 0: Rename psf → topf, scaffold tests

**Files:**
- Rename: `psf.py` → `topf.py`
- Create: `conftest.py`
- Create: `tests/test_topf.py`

- [ ] **Step 1: Rename the file with git**

```bash
git mv psf.py topf.py
```

- [ ] **Step 2: Update the module docstring, shebang stays**

In `topf.py`, replace the top docstring (lines 1-12) so the name reads `topf` and mentions it is a live viewer. Replace the first `"""psf - focused process-snapshot tool.` block with:

```python
#!/usr/bin/env python3
"""topf - focused live process viewer (windowed CPU).

A top-like, full-screen, continuously-sampling viewer that shows only the
*interesting* process subtrees: those matched by comm/cmdline (bazel, ssh
sessions, tmux, claude) AND those that are interesting because they are heavy
(promoted by windowed CPU or RSS). Each node is annotated with the start of its
command line, a summarized cwd, the executing binary, open ports/sockets, and
per-window CPU / RSS / uptime.

Deep-probes only the nodes it prints, and caches the expensive socket analysis
across frames (keyed by (pid, starttime), validated by fd-count + TTL).

With stdout piped or --once, prints a single plain frame (the old psf
behaviour). Run under sudo/root to see other users' processes.
"""
```

- [ ] **Step 3: Create the empty root conftest (path fix for pytest)**

```bash
: > conftest.py
```

- [ ] **Step 4: Create the test file with the import smoke test**

Create `tests/test_topf.py`:

```python
import topf


def test_import_smoke():
    assert hasattr(topf, "render")
    assert hasattr(topf, "scan")
```

- [ ] **Step 5: Run it**

Run: `python -m pytest tests/test_topf.py -v`
Expected: PASS (1 passed)

- [ ] **Step 6: Commit**

```bash
git add topf.py conftest.py tests/test_topf.py
git commit -m "refactor: rename psf to topf; add test scaffold"
```

---

## Task 1: Windowed CPU rate from a sample ring

**Files:**
- Modify: `topf.py` (add `windowed_rate` near the resource-stats section, after `cpu_fraction` ~line 354)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_topf.py`:

```python
TCK = 100  # synthetic clock ticks per second


def test_windowed_rate_constant_one_core():
    # one full core: cpu_ticks advance by TCK every wall-second
    ring = [(0.0, 0), (1.0, TCK), (2.0, 2 * TCK)]
    assert abs(topf.windowed_rate(ring, 2.0, TCK) - 1.0) < 1e-9


def test_windowed_rate_uses_actual_elapsed_on_late_frame():
    # frame was late: 1.5s of wall time, 1.5 cores of work in it
    ring = [(0.0, 0), (1.5, 150)]  # 150 ticks / (100 * 1.5s) = 1.0 core
    assert abs(topf.windowed_rate(ring, 2.0, TCK) - 1.0) < 1e-9


def test_windowed_rate_window_larger_than_span_uses_oldest():
    # only 1s of history but a 60s window requested -> rate over the 1s we have
    ring = [(10.0, 0), (11.0, 200)]  # 200 ticks / (100 * 1s) = 2.0 cores
    assert abs(topf.windowed_rate(ring, 60.0, TCK) - 2.0) < 1e-9


def test_windowed_rate_picks_sample_at_or_before_target():
    # newest "now" = t=3; window 1s -> target t=2; base must be the t=2 sample
    ring = [(0.0, 0), (1.0, 100), (2.0, 200), (3.0, 350)]
    # delta over [2,3] = 150 ticks / (100 * 1s) = 1.5 cores
    assert abs(topf.windowed_rate(ring, 1.0, TCK) - 1.5) < 1e-9


def test_windowed_rate_too_few_samples_is_none():
    assert topf.windowed_rate([(0.0, 0)], 2.0, TCK) is None
    assert topf.windowed_rate([], 2.0, TCK) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k windowed_rate -v`
Expected: FAIL with `AttributeError: module 'topf' has no attribute 'windowed_rate'`

- [ ] **Step 3: Implement**

In `topf.py`, immediately after `cpu_fraction` (currently ending ~line 354), add:

```python
def windowed_rate(ring, window, clk_tck):
    """CPU rate (in cores) over the trailing `window` seconds of a sample ring.
    `ring` is [(monotonic_t, cpu_ticks)] ascending. Uses the most recent sample
    at or before now-window as the baseline (or the oldest sample if the ring is
    younger than the window), and the ACTUAL elapsed wall time between that
    baseline and the latest sample (frames can be late). None if < 2 samples or
    a non-positive span."""
    if len(ring) < 2:
        return None
    now_t, now_ticks = ring[-1]
    target = now_t - window
    base = ring[0]
    for sample in ring:
        if sample[0] <= target:
            base = sample
        else:
            break
    t0, ticks0 = base
    elapsed = now_t - t0
    if elapsed <= 0:
        return None
    return ((now_ticks - ticks0) / clk_tck) / elapsed
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_topf.py -k windowed_rate -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: windowed CPU rate from a sample ring"
```

---

## Task 2: History ring — append, evict, drop dead pids

**Files:**
- Modify: `topf.py` (add `update_history` after `windowed_rate`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_topf.py`:

```python
def _proc(pid, starttime=1, ticks=0):
    return topf.Proc(pid=pid, ppid=1, comm="x", cmdline="x", state="R",
                     num_threads=1, starttime=starttime, uid=0,
                     utime=ticks, stime=0)


def test_update_history_appends_and_keys_by_pid_starttime():
    hist = {}
    topf.update_history(hist, {5: _proc(5, starttime=7, ticks=100)}, 1.0, 60.0)
    assert hist[(5, 7)] == [(1.0, 100)]
    topf.update_history(hist, {5: _proc(5, starttime=7, ticks=250)}, 2.0, 60.0)
    assert hist[(5, 7)] == [(1.0, 100), (2.0, 250)]


def test_update_history_evicts_old_but_keeps_one_before_cutoff():
    hist = {(5, 1): [(0.0, 0), (1.0, 100), (50.0, 200)]}
    # now=100, longest window=60 -> cutoff=40; keep last sample < 40 (t=1) + rest
    topf.update_history(hist, {5: _proc(5, ticks=300)}, 100.0, 60.0)
    assert hist[(5, 1)] == [(1.0, 100), (50.0, 200), (100.0, 300)]


def test_update_history_drops_dead_pids():
    hist = {(5, 1): [(0.0, 0)], (6, 1): [(0.0, 0)]}
    topf.update_history(hist, {5: _proc(5)}, 1.0, 60.0)
    assert (5, 1) in hist
    assert (6, 1) not in hist
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k update_history -v`
Expected: FAIL with `AttributeError: ... 'update_history'`

- [ ] **Step 3: Implement**

In `topf.py`, after `windowed_rate`, add:

```python
def update_history(history, procs, now, longest_window):
    """Append (now, utime+stime) to each live proc's ring (keyed by
    (pid, starttime)); evict samples older than now-longest_window while keeping
    the single most recent sample before the cutoff (so the longest window stays
    fully covered); drop rings for pids no longer present. Mutates `history`."""
    cutoff = now - longest_window
    seen = set()
    for p in procs.values():
        key = (p.pid, p.starttime)
        seen.add(key)
        ring = history.setdefault(key, [])
        ring.append((now, p.utime + p.stime))
        keep_from = 0
        for i, (ts, _ticks) in enumerate(ring):
            if ts < cutoff:
                keep_from = i
            else:
                break
        if keep_from:
            del ring[:keep_from]
    for key in list(history):
        if key not in seen:
            del history[key]
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_topf.py -k update_history -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: history ring append/evict/drop"
```

---

## Task 3: Per-proc window computation + drop cpu_current

**Files:**
- Modify: `topf.py` — `Proc` dataclass (~line 88), add `compute_windows`
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_topf.py`:

```python
def test_compute_windows_sets_aligned_list():
    procs = {5: _proc(5, starttime=1, ticks=300)}
    hist = {(5, 1): [(0.0, 0), (1.0, 100), (2.0, 200), (3.0, 300)]}
    topf.compute_windows(procs, hist, (1.0, 2.0), TCK)
    w = procs[5].cpu_windows
    assert len(w) == 2
    # 1s window [2,3]: 100 ticks/(100*1s)=1.0 ; 2s window [1,3]: 200/(100*2)=1.0
    assert abs(w[0] - 1.0) < 1e-9 and abs(w[1] - 1.0) < 1e-9


def test_compute_windows_young_proc_gets_none():
    procs = {5: _proc(5, starttime=1, ticks=100)}
    hist = {(5, 1): [(3.0, 100)]}   # only one sample
    topf.compute_windows(procs, hist, (1.0, 2.0), TCK)
    assert procs[5].cpu_windows == [None, None]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k compute_windows -v`
Expected: FAIL (`compute_windows` / `cpu_windows` missing)

- [ ] **Step 3: Implement**

In `topf.py`, in the `Proc` dataclass, **remove** the line:

```python
    cpu_current: float = None       # recent CPU fraction from probe sampling
```

and **add** in its place:

```python
    cpu_windows: list = None        # per-window CPU rate (cores), aligned to windows
```

Then add after `update_history`:

```python
def compute_windows(procs, history, windows, clk_tck):
    """Set proc.cpu_windows: a list of per-window CPU rates (cores) aligned to
    `windows`, computed from the proc's history ring. Entries are None where the
    ring has < 2 samples."""
    for p in procs.values():
        ring = history.get((p.pid, p.starttime), [])
        p.cpu_windows = [windowed_rate(ring, w, clk_tck) for w in windows]
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_topf.py -k compute_windows -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: compute per-window CPU; drop cpu_current field"
```

---

## Task 4: Resource promotion + extended select()

**Files:**
- Modify: `topf.py` — add config constants (~line 45), `is_promoted`, rewrite `select` (~line 174)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Add config constants**

In `topf.py`, after the `CPU_TINT_ANCHORS` line (~line 45) add:

```python
DEFAULT_WINDOWS = (2.0, 10.0, 60.0)   # CPU window seconds (shortest..longest)
PROMOTE_LEVEL = 2         # tint-anchor level required to promote (>= 1.0 core / >= 1G)
RSS_GATE_LEVEL = 1        # longest-window CPU floor for RSS-only promotion (~10%)
REFRESH_INTERVAL = 1.0    # default sample == redraw cadence (seconds)
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_topf.py`. Note: `_tint_level` uses `CPU_TINT_ANCHORS=(0.10,1.0,4.0)` and `RSS_TINT_ANCHORS=(100M,1G,5G)`.

```python
G = 1024 ** 3


def _rproc(pid, ppid=1, comm="x", windows=None, rss_bytes=0, starttime=1):
    p = topf.Proc(pid=pid, ppid=ppid, comm=comm, cmdline=comm, state="R",
                  num_threads=1, starttime=starttime, uid=0,
                  rss_pages=rss_bytes // topf.PAGE_SIZE)
    p.cpu_windows = windows if windows is not None else [None, None, None]
    return p


def test_promote_by_cpu_level2():
    p = _rproc(5, windows=[1.5, 0.0, 0.0])   # 1.5 cores -> cpu level 2
    assert topf.is_promoted(p, topf.PAGE_SIZE, 2, True, False) is True


def test_no_promote_below_level():
    p = _rproc(5, windows=[0.5, 0.5, 0.5])   # 0.5 cores -> level 1 only
    assert topf.is_promoted(p, topf.PAGE_SIZE, 2, True, False) is False


def test_rss_only_promotion_gated_off_when_idle():
    p = _rproc(5, windows=[0.0, 0.0, 0.0], rss_bytes=2 * G)  # rss level 2, no cpu
    assert topf.is_promoted(p, topf.PAGE_SIZE, 2, True, False) is False   # gate on
    assert topf.is_promoted(p, topf.PAGE_SIZE, 2, False, False) is True   # gate off


def test_rss_only_promotion_passes_gate_with_floor_cpu():
    # rss level 2 AND longest-window cpu >= level 1 (>=0.10 cores)
    p = _rproc(5, windows=[0.0, 0.0, 0.2], rss_bytes=2 * G)
    assert topf.is_promoted(p, topf.PAGE_SIZE, 2, True, False) is True


def test_kthread_promotes_by_cpu_only_never_rss():
    heavy = _rproc(5, windows=[2.0, 0.0, 0.0], rss_bytes=0)
    assert topf.is_promoted(heavy, topf.PAGE_SIZE, 2, True, True) is True
    # a kthread reporting rss is still never promoted by rss
    rssonly = _rproc(6, windows=[0.0, 0.0, 0.0], rss_bytes=2 * G)
    assert topf.is_promoted(rssonly, topf.PAGE_SIZE, 2, True, True) is False


def test_select_promotes_and_marks_interesting():
    # root(1) -> hog(5) heavy; hog must be kept AND interesting (survives collapse)
    root = _rproc(1, ppid=0, comm="init")
    hog = _rproc(5, ppid=1, comm="qemu", windows=[3.0, 3.0, 3.0])
    procs = {1: root, 5: hog}
    topf.build_tree(procs)
    topf.select(procs, [], topf.PAGE_SIZE, 2, True)
    assert hog.interesting is True and hog.kept is True
    assert root.kept is True   # ancestor kept to keep tree rooted
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k "promote or select_promotes" -v`
Expected: FAIL (`is_promoted` missing / `select` signature)

- [ ] **Step 4: Implement `is_promoted` and rewrite `select`**

In `topf.py`, add before `select` (~line 174):

```python
def is_promoted(proc, page_size, promote_level, rss_needs_cpu, is_kthread):
    """A process is promoted (interesting because heavy) when it clears
    tint-anchor level >= promote_level on any CPU window, or (non-kthreads only)
    on RSS. RSS-only promotion is gated: it also requires the longest window's
    CPU to clear RSS_GATE_LEVEL unless rss_needs_cpu is False. Kernel threads
    promote by CPU alone (they have no meaningful RSS)."""
    cpu_level = max((_tint_level(f, CPU_TINT_ANCHORS)
                     for f in proc.cpu_windows if f is not None), default=0)
    if cpu_level >= promote_level:
        return True
    if is_kthread:
        return False
    rss = proc.rss_pages * page_size
    if _tint_level(rss, RSS_TINT_ANCHORS) >= promote_level:
        if not rss_needs_cpu:
            return True
        longest = proc.cpu_windows[-1] if proc.cpu_windows else None
        return _tint_level(longest, CPU_TINT_ANCHORS) >= RSS_GATE_LEVEL
    return False
```

Then **replace** the existing `select` function body (lines ~174-196) with the extended version:

```python
def select(procs, matchers, page_size, promote_level, rss_needs_cpu):
    """Mark .interesting and .kept. Interesting = matched (bazel/ssh/tmux/claude)
    OR resource-promoted (heavy CPU/RSS). Kept = interesting + their descendants
    + their ancestors (so the tree stays rooted). Kernel-thread subtrees (under
    pid 2) are never matched, but ARE promotable when heavy (CPU only)."""
    kthreadd = procs.get(2)
    kthread_pids = set()
    if kthreadd is not None:
        kthread_pids = {2} | {d.pid for d in _descendants(kthreadd)}

    for p in procs.values():
        is_kthread = p.pid in kthread_pids
        matched = (not is_kthread) and is_interesting(p, matchers)
        promoted = is_promoted(p, page_size, promote_level, rss_needs_cpu,
                               is_kthread)
        p.interesting = matched or promoted
        p.kept = False

    for p in list(procs.values()):
        if not p.interesting:
            continue
        p.kept = True
        for d in _descendants(p):       # subtree
            d.kept = True
        anc = procs.get(p.ppid)         # ancestors up to a root
        while anc is not None and not anc.kept:
            anc.kept = True
            anc = procs.get(anc.ppid)
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_topf.py -k "promote or select_promotes" -v`
Expected: PASS (6 passed)

- [ ] **Step 6: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: resource promotion (CPU/RSS) with kthread + RSS-gate rules"
```

---

## Task 5: Subtree ordering by active window

**Files:**
- Modify: `topf.py` — add `subtree_window_cpu`; add `top_sort_key` param to `render` (~line 873)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_topf.py`:

```python
def test_subtree_window_cpu_sums_all_descendants():
    root = _rproc(1, ppid=0, windows=[1.0, 0, 0])
    a = _rproc(2, ppid=1, windows=[2.0, 0, 0])
    b = _rproc(3, ppid=2, windows=[0.5, 0, 0])
    procs = {1: root, 2: a, 3: b}
    topf.build_tree(procs)
    assert abs(topf.subtree_window_cpu(root, 0) - 3.5) < 1e-9
    assert abs(topf.subtree_window_cpu(a, 0) - 2.5) < 1e-9


def test_subtree_window_cpu_treats_none_as_zero():
    root = _rproc(1, ppid=0, windows=[None, None, None])
    assert topf.subtree_window_cpu(root, 0) == 0.0


def test_render_orders_top_level_by_window_desc_pid_tiebreak():
    # two top-level roots; the busier one (higher 0-window cpu) must render first
    cold = _rproc(10, ppid=0, comm="cold", windows=[0.1, 0, 0])
    hot = _rproc(20, ppid=0, comm="hot", windows=[5.0, 0, 0])
    procs = {10: cold, 20: hot}
    topf.build_tree(procs)
    for p in procs.values():
        p.kept = True
    roots = [cold, hot]
    key = lambda item: topf.subtree_window_cpu(
        item.members[0] if isinstance(item, topf.Group) else item, 0)
    lines = topf.render(roots, set(), top_sort_key=key)
    assert lines[0].endswith("hot")
    assert any(ln.endswith("cold") for ln in lines)
    assert lines.index(next(l for l in lines if l.endswith("hot"))) < \
           lines.index(next(l for l in lines if l.endswith("cold")))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k "subtree or render_orders" -v`
Expected: FAIL (`subtree_window_cpu` missing / `render` has no `top_sort_key`)

- [ ] **Step 3: Implement**

In `topf.py`, add near the other resource helpers (after `cpu_fraction`/`windowed_rate`, before render is fine — but place it just above `render` for locality):

```python
def subtree_window_cpu(node, widx):
    """Sum of the window `widx` CPU rate over `node` and ALL its descendants
    (suppressed/collapsed included). None rates count as 0. Used to order
    top-level subtrees by their true total load."""
    total = 0.0
    nodes = [node] + _descendants(node)
    for n in nodes:
        if n.cpu_windows:
            v = n.cpu_windows[widx]
            if v is not None:
                total += v
    return total
```

Then modify `render`'s signature and the final `walk_items` call. Change the signature line (~line 873) to add `top_sort_key=None`:

```python
def render(roots, suppressed, width=CMD_WIDTH, color=None, sysinfo=None,
           dedup_min=None, never_merge=frozenset(), top_sort_key=None,
           show_avg=False):
```

(`show_avg` is consumed in Task 7; declare it now so the signature is stable.)

Replace the final line of `render` (currently `walk_items(group_siblings(list(roots), dedup_min, never_merge), "")`) with:

```python
    top_items = group_siblings(list(roots), dedup_min, never_merge)
    if top_sort_key is not None:
        # group_siblings already orders by min-pid (stable); a stable sort by
        # descending key therefore gives "load desc, pid asc" tiebreak.
        top_items.sort(key=top_sort_key, reverse=True)
    walk_items(top_items, "")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_topf.py -k "subtree or render_orders" -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: order top-level subtrees by active-window CPU"
```

---

## Task 6: ANSI-aware truncate + frame clipping

**Files:**
- Modify: `topf.py` — add `visible_truncate`, `clip_frame`, and a module-level `_SGR_RE`
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_topf.py`:

```python
def test_visible_truncate_plain():
    assert topf.visible_truncate("hello world", 5) == "hello"


def test_visible_truncate_counts_visible_not_escapes():
    s = "\x1b[33mhello\x1b[0m"
    # width 3 keeps the opening SGR, 3 visible chars, and appends a reset
    assert topf.visible_truncate(s, 3) == "\x1b[33mhel\x1b[0m"


def test_visible_truncate_no_cut_keeps_everything():
    s = "\x1b[33mhi\x1b[0m"
    assert topf.visible_truncate(s, 10) == s


def test_visible_truncate_zero_width():
    assert topf.visible_truncate("anything", 0) == ""


def test_visible_truncate_never_splits_escape():
    s = "a\x1b[1;31mB"   # width 2 must not cut inside the \x1b[1;31m
    out = topf.visible_truncate(s, 2)
    assert out == "a\x1b[1;31mB\x1b[0m"


def test_clip_frame_within_bounds():
    lines = ["aaa", "bbb"]
    assert topf.clip_frame(lines, rows=5, cols=10) == ["aaa", "bbb"]


def test_clip_frame_overflow_adds_more_footer():
    lines = ["l0", "l1", "l2", "l3", "l4"]
    out = topf.clip_frame(lines, rows=3, cols=20)
    assert len(out) == 3
    assert out[:2] == ["l0", "l1"]
    assert out[2] == "… +3 more"


def test_clip_frame_truncates_columns():
    out = topf.clip_frame(["hello world"], rows=5, cols=5)
    assert out == ["hello"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k "truncate or clip_frame" -v`
Expected: FAIL (`visible_truncate` / `clip_frame` missing)

- [ ] **Step 3: Implement**

In `topf.py`, add near the top (after the imports / regex usage; placing next to render helpers is fine):

```python
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_truncate(s, width):
    """Truncate `s` to `width` VISIBLE characters, counting through SGR escapes
    (\\x1b[..m) without splitting them. If the cut lands while a non-reset color
    is active, a reset (\\x1b[0m) is appended so the colour doesn't bleed."""
    if width <= 0:
        return ""
    out = []
    vis = 0
    has_color = False
    i = 0
    n = len(s)
    while i < n:
        m = _SGR_RE.match(s, i)
        if m:
            esc = m.group()
            out.append(esc)
            has_color = esc != "\x1b[0m"
            i = m.end()
            continue
        if vis >= width:
            break
        out.append(s[i])
        vis += 1
        i += 1
    truncated = i < n
    res = "".join(out)
    if truncated and has_color:
        res += "\x1b[0m"
    return res


def clip_frame(lines, rows, cols):
    """Clip a list of rendered lines to a `rows` x `cols` terminal: every line
    is column-truncated (ANSI-aware); if there are more than `rows` lines, keep
    rows-1 and replace the rest with a '… +K more' footer."""
    clipped = [visible_truncate(ln, cols) for ln in lines]
    if len(clipped) <= rows:
        return clipped
    keep = clipped[:rows - 1]
    more = len(clipped) - (rows - 1)
    keep.append(visible_truncate("… +%d more" % more, cols))
    return keep
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_topf.py -k "truncate or clip_frame" -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: ANSI-aware truncate and frame clipping"
```

---

## Task 7: Windowed CPU rendering (detail lines, groups, glossary, cores)

**Files:**
- Modify: `topf.py` — `SysInfo` (~line 53), rewrite `_cpu_bit` (~line 766), `_detail` (~line 795), `_group_detail` (~line 828), `glossary` (~line 913)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_topf.py`:

```python
def test_cpu_bit_live_three_windows():
    text, level = topf._cpu_bit([4.0, 2.0, 0.5])
    assert text == "cpu 400% 200% 50%"
    # level = max tint across windows: 4.0 cores clears all 3 anchors -> 3
    assert level == 3


def test_cpu_bit_none_window_renders_dash():
    text, _ = topf._cpu_bit([4.0, None, None])
    assert text == "cpu 400% — —"


def test_cpu_bit_once_mode_appends_avg():
    text, _ = topf._cpu_bit([0.42, None, None], avg_frac=0.031)
    assert text == "cpu 42% — — (3.1% avg)"


def test_cpu_bit_tint_ignores_none():
    _, level = topf._cpu_bit([0.05, None, None])  # 0.05 cores -> level 0
    assert level == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k cpu_bit -v`
Expected: FAIL (`_cpu_bit` has the old signature)

- [ ] **Step 3: Add `cores` to SysInfo**

Change the `SysInfo` definition (~line 53) from:

```python
SysInfo = namedtuple("SysInfo", "clk_tck page_size uptime")
```

to:

```python
SysInfo = namedtuple("SysInfo", "clk_tck page_size uptime cores")
```

- [ ] **Step 4: Rewrite `_cpu_bit`**

Replace the entire `_cpu_bit` function (~lines 766-781) with:

```python
def _cpu_bit(windows_fracs, avg_frac=None):
    """Format a per-window CPU headline: 'cpu 400% 200% 50%' (one figure per
    window; None -> '—'). Tint level = max _tint_level across the non-None
    windows. In --once mode, avg_frac adds a trailing '(Y avg)' lifetime bit.
    Returns (text, level)."""
    parts = [(fmt_pct(f) or "—") for f in windows_fracs]
    level = max((_tint_level(f, CPU_TINT_ANCHORS)
                 for f in windows_fracs if f is not None), default=0)
    text = "cpu " + " ".join(parts)
    if avg_frac is not None:
        a = fmt_pct(avg_frac)
        if a is not None:
            text += " (%s avg)" % a
    return (text, level)
```

- [ ] **Step 5: Update `_detail` to use windows**

In `_detail` (~line 795), change its signature to thread `show_avg`:

```python
def _detail(node, color, sysinfo=None, show_avg=False):
```

Replace the CPU block (currently):

```python
    if sysinfo is not None:
        cpu = _cpu_bit(node, sysinfo)
        if cpu is not None:
            bits.append(cpu)
```

with:

```python
    if sysinfo is not None:
        if node.cpu_windows and any(f is not None for f in node.cpu_windows):
            avg = None
            if show_avg:
                life = lifetime_secs(node.starttime, sysinfo.uptime,
                                     sysinfo.clk_tck)
                avg = cpu_fraction(node.utime + node.stime, life,
                                   sysinfo.clk_tck)
            bits.append(_cpu_bit(node.cpu_windows, avg))
```

- [ ] **Step 6: Update `_group_detail` to use windows**

In `_group_detail` (~line 828), change its signature:

```python
def _group_detail(members, color, sysinfo, show_avg=False):
```

Replace the CPU block (the part computing `avgs`/`curs` and appending the `cpu ...` bit, currently ~lines 843-858) with a per-window range block:

```python
    if sysinfo is not None:
        nwin = max((len(m.cpu_windows) for m in members if m.cpu_windows),
                   default=0)
        if nwin:
            parts = []
            sums = []
            for w in range(nwin):
                vals = [m.cpu_windows[w] for m in members
                        if m.cpu_windows and m.cpu_windows[w] is not None]
                parts.append(range_str(vals, fmt_pct) if vals else "—")
                sums.append(sum(vals))
            # tint tracks the heaviest window's summed load across members
            level = max(_tint_level(s, CPU_TINT_ANCHORS) for s in sums)
            text = "cpu " + " ".join(parts)
            if show_avg:
                avgs = [a for a in (
                    cpu_fraction(m.utime + m.stime,
                                 lifetime_secs(m.starttime, sysinfo.uptime,
                                               sysinfo.clk_tck), sysinfo.clk_tck)
                    for m in members) if a is not None]
                if avgs:
                    text += " (%s avg)" % range_str(avgs, fmt_pct)
            bits.append((text, level))
```

- [ ] **Step 7: Thread `show_avg` through `render`'s `walk_items` calls**

In `render`'s `walk_items` (~lines 888-905), update the two detail calls:

```python
                detail = _group_detail(item.members, color, sysinfo, show_avg)
```

and

```python
                detail = _detail(item, color, sysinfo, show_avg)
```

- [ ] **Step 8: Rewrite the glossary stats line**

In `glossary` (~line 913), replace the `"  stats:   cpu X% (Y avg) ..."` line with:

```python
        "  stats:   cpu A% B% C% = CPU over the short/med/long windows "
        "(cores; 100% = 1 core)   rss = resident memory   up = time since start",
```

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest tests/test_topf.py -v`
Expected: PASS (all tests so far). The `render_orders` test still passes (its procs have `cpu_windows` set and `sysinfo=None`, so no detail line is produced).

- [ ] **Step 10: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: render windowed CPU in detail/group lines, glossary, cores"
```

---

## Task 8: CLI args + window parsing

**Files:**
- Modify: `topf.py` — `main`'s argparse (~lines 964-986), add `parse_windows`
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_topf.py`:

```python
def test_parse_windows_basic():
    assert topf.parse_windows("2,10,60") == (2.0, 10.0, 60.0)


def test_parse_windows_single_and_floats():
    assert topf.parse_windows("0.2") == (0.2,)
    assert topf.parse_windows("1, 5 , 30") == (1.0, 5.0, 30.0)


def test_parse_windows_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        topf.parse_windows("2,abc")
    with pytest.raises(ValueError):
        topf.parse_windows("")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k parse_windows -v`
Expected: FAIL (`parse_windows` missing)

- [ ] **Step 3: Implement `parse_windows`**

In `topf.py`, add just above `main`:

```python
def parse_windows(text):
    """Parse a '2,10,60' window spec into a tuple of positive floats (ascending
    order is the caller's responsibility). Raises ValueError on empty/garbage."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError("no windows given")
    vals = tuple(float(p) for p in parts)   # float() raises ValueError on garbage
    if any(v <= 0 for v in vals):
        raise ValueError("windows must be positive")
    return vals
```

- [ ] **Step 4: Add the new argparse flags**

In `main` (~line 965), set `prog="topf"` and the description, and change the `-s` default. Replace:

```python
    ap = argparse.ArgumentParser(description="Focused process snapshot.")
```

with:

```python
    ap = argparse.ArgumentParser(prog="topf",
                                 description="Focused live process viewer.")
```

Change the `-s/--sample-interval` argument block to default to `REFRESH_INTERVAL` and reword help:

```python
    ap.add_argument("-s", "--sample-interval", type=float,
                    default=REFRESH_INTERVAL,
                    help="sample == redraw cadence in seconds (default %.2g)"
                         % REFRESH_INTERVAL)
```

Add these new arguments alongside the others (before `args = ap.parse_args(argv)`):

```python
    ap.add_argument("--once", action="store_true",
                    help="take a single plain frame and exit (auto when piped)")
    ap.add_argument("--windows", type=parse_windows, default=DEFAULT_WINDOWS,
                    metavar="A,B,C",
                    help="CPU window seconds, shortest first (default 2,10,60)")
    ap.add_argument("--promote-level", type=int, default=PROMOTE_LEVEL,
                    help="tint-anchor level to promote a heavy proc (default %d)"
                         % PROMOTE_LEVEL)
    ap.add_argument("--rss-needs-cpu", dest="rss_needs_cpu",
                    action="store_true", default=True,
                    help="RSS-only promotion also needs some CPU (default on)")
    ap.add_argument("--no-rss-needs-cpu", dest="rss_needs_cpu",
                    action="store_false",
                    help="allow promotion by large RSS alone")
```

(Leave the existing body of `main` for now; Task 9 rewires it. The file must still import — verify next.)

- [ ] **Step 5: Verify parse + import**

Run: `python -m pytest tests/test_topf.py -k parse_windows -v`
Expected: PASS (3 passed)
Run: `python -c "import topf; topf.main(['--help'])"`
Expected: help text prints showing `--once`, `--windows`, `--promote-level`, `--rss-needs-cpu`. (The body may error at runtime because `select()`'s signature changed — that's fixed in Task 9. `--help` exits before the body runs.)

- [ ] **Step 6: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: topf CLI flags (--once/--windows/--promote-level/--rss-needs-cpu)"
```

---

## Task 9: Rewire one-shot path (`--once` / piped) onto the new pipeline

**Files:**
- Modify: `topf.py` — add `cores_count`, `frame_pipeline`, `render_once`; rewrite the body of `main` (~lines 988-1036)
- Test: `tests/test_topf.py` (a smoke test that `render_once` returns lines)

This task makes `topf` runnable end-to-end in `--once`/piped mode using the new `history`+`windowed_rate` path (two samples → shortest window real, longer windows `—`, plus lifetime avg). Live mode comes in Task 10.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_topf.py`:

```python
def test_cores_count_positive():
    assert topf.cores_count() >= 1


def test_render_once_smoke(monkeypatch):
    # Drive render_once against the real /proc but with a tiny interval; assert
    # it returns a non-empty list of strings and includes the header.
    lines = topf.render_once(interval=0.05, args=topf._once_defaults())
    assert isinstance(lines, list) and lines
    assert any(ln.startswith("topf —") for ln in lines)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_topf.py -k "cores_count or render_once" -v`
Expected: FAIL (`cores_count` / `render_once` / `_once_defaults` missing)

- [ ] **Step 3: Implement helpers + header + the one-shot frame**

In `topf.py`, add `cores_count` near the other `/proc` readers (after `read_uptime`):

```python
def cores_count():
    """Number of online CPUs (for the header and for context, not for math —
    CPU figures are already in cores). Falls back to 1."""
    return os.cpu_count() or 1
```

Add a header builder and the one-shot frame assembler just above `main`:

```python
def header_line(frame_dt, sysinfo, nprocs, hidden, interval, frozen=False):
    """Top-style status line."""
    state = "  FROZEN" if frozen else ""
    return ("topf — %.2gs, %d cores, %d procs (%d hidden)   every %.2gs   "
            "[q]uit  [space]freeze  [w]indow%s"
            % (frame_dt, sysinfo.cores, nprocs, hidden, interval, state))


def build_frame(prev, cur, history, t_prev, t_now, args, color, sysinfo,
                sort_idx, show_avg, frozen=False):
    """Pure-ish assembly of one frame's lines (no clipping, no terminal I/O):
    tree (ordered) + optional lifecycle, with a header on top. `prev` may be
    None (first frame / once-mode primes). `history` already updated & windows
    already computed for `cur`. Returns a list of lines."""
    roots = build_tree(cur)
    select(cur, DEFAULT_MATCHERS, sysinfo.page_size, args.promote_level,
           args.rss_needs_cpu)
    suppressed = collapse(cur, threshold=args.threshold)
    visible_roots = [r for r in roots if r.kept]
    printed = [n for n in collect_printed(visible_roots, suppressed) if n.kept]

    if args.no_cache:
        cache = Cache(os.devnull, boot_id="", now=time.time())
    else:
        cache = Cache(cache_path(), boot_id=read_boot_id(), now=time.time())
    probe(printed, cache)
    if not args.no_cache:
        cache.save(live_keys={(p.pid, p.starttime) for p in cur.values()})

    dedup_min = None if args.no_dedup else args.dedup_min
    key = lambda item: subtree_window_cpu(
        item.members[0] if isinstance(item, Group) else item, sort_idx)

    frame_dt = (t_now - t_prev) if prev is not None else 0.0
    hidden = sum(1 for p in cur.values() if not p.kept)
    out = [header_line(frame_dt, sysinfo, len(cur), hidden,
                       args.sample_interval, frozen)]
    if not args.no_glossary:
        out += [""] + glossary(color)
    out += [""]
    out += render(visible_roots, suppressed, width=args.width, color=color,
                  sysinfo=sysinfo, dedup_min=dedup_min, never_merge=NEVER_MERGE,
                  top_sort_key=key, show_avg=show_avg)
    if prev is not None and not args.no_lifecycle:
        born, died = diff_snapshots(prev, cur)
        section = format_lifecycle(born, died, _parents_map(prev, cur),
                                   sysinfo, frame_dt, color=color)
        if section:
            out += [""] + section
    return out


def render_once(interval, args):
    """Take two samples `interval` apart and return one frame's lines (no alt
    screen). Shortest window is real; longer windows show '—'; a lifetime avg is
    appended (show_avg=True). This is the piped / --once path."""
    windows = args.windows
    longest = max(windows)
    history = {}
    s_a = scan()
    t_a = time.monotonic()
    update_history(history, s_a, t_a, longest)
    time.sleep(interval)
    s_b = scan()
    t_b = time.monotonic()
    update_history(history, s_b, t_b, longest)
    compute_windows(s_b, history, windows, CLK_TCK)
    sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE,
                      uptime=read_uptime(), cores=cores_count())
    color = sys.stdout.isatty() and not args.no_color
    return build_frame(s_a, s_b, history, t_a, t_b, args, color, sysinfo,
                       sort_idx=0, show_avg=True)
```

Add a tiny test helper (so the test can build a defaults namespace without argparse) right after `render_once`:

```python
def _once_defaults():
    """A defaults namespace for render_once in tests."""
    import types
    return types.SimpleNamespace(
        width=CMD_WIDTH, threshold=COLLAPSE_THRESHOLD, no_cache=True,
        no_color=True, no_glossary=False, sample_interval=REFRESH_INTERVAL,
        no_dedup=False, dedup_min=DEDUP_MIN, no_lifecycle=False,
        windows=DEFAULT_WINDOWS, promote_level=PROMOTE_LEVEL,
        rss_needs_cpu=True)
```

- [ ] **Step 4: Rewrite the body of `main`**

Replace everything in `main` from `s_a = scan()` (~line 988) to the end of the function with:

```python
    use_once = args.once or not sys.stdout.isatty()
    if use_once:
        lines = render_once(args.sample_interval, args)
        print("\n".join(lines))
        return
    run_live(args)   # implemented in Task 10
```

- [ ] **Step 5: Run the smoke test + manual once**

Run: `python -m pytest tests/test_topf.py -k "cores_count or render_once" -v`
Expected: PASS (2 passed)
Run: `python topf.py --once --no-color | head -40`
Expected: a header line `topf — ...`, the glossary, then a tree with `cpu N% — —` style figures and a lifecycle section. (`run_live` not defined yet, but `--once` doesn't reach it.)

- [ ] **Step 6: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: one-shot/--once frame via the windowed pipeline"
```

---

## Task 10: Live TUI shell (alt screen, cbreak, freeze, w-cycle)

**Files:**
- Modify: `topf.py` — add `import` for terminal modules at top; add `run_live`, `_draw_frame`
- Test: manual (the loop is a thin imperative shell; pure logic is already covered)

- [ ] **Step 1: Add terminal imports**

At the top of `topf.py`, with the other imports, add:

```python
import select as _select      # stdlib selector; avoid clash with select() below
import termios
import tty
```

- [ ] **Step 2: Implement `_draw_frame`**

Add above `main`:

```python
def _draw_frame(out, lines):
    """Home the cursor, write each line with clear-to-EOL, then clear to end of
    screen so a shorter frame doesn't leave stale rows behind."""
    buf = ["\x1b[H"]
    for ln in lines:
        buf.append(ln + "\x1b[K\r\n")
    buf.append("\x1b[J")
    out.write("".join(buf))
    out.flush()
```

- [ ] **Step 3: Implement `run_live`**

Add above `main`:

```python
def run_live(args):
    """Full-screen live loop: raw ANSI alt-screen + termios cbreak + select
    polling. Read-only keys: q/Ctrl-C quit, space freeze, w cycle sort window.
    Terminal state is always restored (finally), even on exception/signal."""
    fd = sys.stdin.fileno()
    out = sys.stdout
    old_attr = termios.tcgetattr(fd)
    windows = args.windows
    longest = max(windows)
    history = {}
    cache = (Cache(os.devnull, boot_id="", now=time.time()) if args.no_cache
             else Cache(cache_path(), boot_id=read_boot_id(), now=time.time()))
    sysinfo_base = (read_uptime(), cores_count())
    sort_idx = 0
    frozen = False
    prev, t_prev = None, None

    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h")
        out.flush()
        while True:
            if not frozen:
                cur = scan()
                t_now = time.monotonic()
                update_history(history, cur, t_now, longest)
                compute_windows(cur, history, windows, CLK_TCK)
                color = not args.no_color
                sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE,
                                  uptime=read_uptime(), cores=sysinfo_base[1])
                lines = build_frame(prev, cur, history, t_prev or t_now, t_now,
                                    args, color, sysinfo, sort_idx,
                                    show_avg=False, frozen=frozen)
                rows, cols = os.get_terminal_size()
                _draw_frame(out, clip_frame(lines, rows, cols))
                prev, t_prev = cur, t_now

            r, _w, _e = _select.select([fd], [], [], args.sample_interval)
            if r:
                ch = sys.stdin.read(1)
                if ch in ("q", "\x03"):     # q or Ctrl-C
                    break
                if ch == " ":
                    frozen = not frozen
                    if frozen:              # repaint once to show FROZEN marker
                        rows, cols = os.get_terminal_size()
                        lines[0] = header_line(
                            (t_now - (t_prev or t_now)), sysinfo, len(cur),
                            sum(1 for p in cur.values() if not p.kept),
                            args.sample_interval, frozen=True)
                        _draw_frame(out, clip_frame(lines, rows, cols))
                elif ch == "w":
                    sort_idx = (sort_idx + 1) % len(windows)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        if not args.no_cache:
            cache.now = time.time()
            cache.save(live_keys=set())     # nothing forced live; entries already pruned
```

Note on `cache` in live mode: `build_frame` constructs its own per-frame `Cache` (so `cache.now` is fresh each frame and TTL works). The `run_live`-level `cache` object exists only to satisfy the finally-block contract; per-frame caches already persist via their own `save()` inside `build_frame`. This keeps Task 9 and Task 10 sharing one `build_frame`.

- [ ] **Step 4: Manual verification**

Run: `python topf.py -s 0.5`
Expected: alternate screen clears, header shows `topf — ...`, tree refreshes ~2x/sec, heavy procs float to the top. Press `w` → header behaviour unchanged but ordering switches to the next window (verify by starting a CPU spike: `yes > /dev/null &` then watch it rise/fall across windows; `kill %1` after). Press `space` → `FROZEN` appears and updates stop; `space` again resumes. Press `q` → returns cleanly to the normal screen with the shell prompt intact and echo working.

Run (regression — terminal restored after Ctrl-C): `python topf.py` then Ctrl-C; confirm the prompt is usable (typed chars echo).

- [ ] **Step 5: Run the full unit suite (no regressions)**

Run: `python -m pytest tests/test_topf.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add topf.py
git commit -m "feat: live full-screen TUI loop (cbreak + alt screen + keys)"
```

---

## Task 11: Per-frame cache freshness in build_frame

**Files:**
- Modify: `topf.py` — confirm/finish `Cache.now` handling (`build_frame` already constructs a fresh `Cache` per frame with `now=time.time()`)
- Test: `tests/test_topf.py`

This task pins down the C1 review item (cache TTL must use a current `now`) with a test, and throttles disk writes by only saving from the per-frame cache when something was actually (re)probed.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_topf.py`:

```python
def test_cache_get_expires_with_advancing_now():
    c = topf.Cache(path=os.devnull, boot_id="b", now=100.0, ttl=30)
    c.put(5, 1, fdcount=3, sockets="LISTEN :22")
    # within TTL at the same now -> hit
    assert c.get(5, 1, 3) == "LISTEN :22"
    # advance now beyond TTL -> miss
    c.now = 200.0
    assert c.get(5, 1, 3) is None
```

(Needs `import os` at the top of the test file — add `import os` next to `import topf` if not present.)

- [ ] **Step 2: Run to verify behaviour**

Run: `python -m pytest tests/test_topf.py -k cache_get_expires -v`
Expected: PASS already — `Cache.get` (`topf.py` ~line 522) compares `self.now - e["ts"]` against `self.ttl`, and `build_frame` sets `now=time.time()` per frame. This test documents and locks that contract. If it FAILS, fix `Cache.get` to read `self.now` (not a captured value).

- [ ] **Step 3: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "test: lock per-frame cache TTL freshness"
```

---

## Task 12: Docs — mark design implemented, refresh README/help notes

**Files:**
- Modify: `docs/superpowers/specs/2026-06-06-topf-windowed-cpu-design.md` (status line)
- Modify: `topf.py` module docstring if anything drifted

- [ ] **Step 1: Update the design status**

In `docs/superpowers/specs/2026-06-06-topf-windowed-cpu-design.md`, change line 4 from:

```
Status: design approved, pending spec review
```

to:

```
Status: implemented (see docs/superpowers/plans/2026-06-07-topf-implementation.md)
```

- [ ] **Step 2: Sanity-run both modes**

Run: `python topf.py --once | head -20`
Expected: plain one-shot frame.
Run: `python topf.py --once --windows 1,5 | head -8`
Expected: `cpu N% —` (two-window headline) — confirms positional windows, no hardcoded 60s.

- [ ] **Step 3: Final full suite**

Run: `python -m pytest tests/test_topf.py -v`
Expected: PASS (all).

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-06-topf-windowed-cpu-design.md topf.py
git commit -m "docs: mark topf design implemented"
```

---

## Self-review notes (coverage map)

- **Substrate/shape** → Task 0 (rename, stdlib), Task 10 (raw ANSI alt screen, termios cbreak, select, restore-on-exit).
- **Sampling loop** → Tasks 2/3 (history + windows), Task 10 (frame loop, prev for lifecycle). `prev`-for-CPU dropped per decision 6 (windows cover it).
- **Windowed CPU** → Tasks 1/3/7 (rate, compute, render incl. None→`—`, tint=max). Lifetime avg only in `--once` (Task 9, decision 3).
- **Resource promotion** → Task 4 (CPU/RSS levels, RSS gate on longest window, kthread CPU-only, sets `.interesting`).
- **Persistence/cooling** → decision 1: per-window tint only; no extra flag — satisfied by Task 7's per-window tint.
- **Ordering** → Task 5 (sum over all descendants, active window, pid tiebreak) + Task 10 (`w` cycles `sort_idx`).
- **Groups & lifecycle** → Task 7 (group windowed ranges + summed tint), Task 9/10 (lifecycle over the frame interval).
- **Header & clipping** → Task 6 (`visible_truncate`, `clip_frame`), Task 9 (`header_line`), Task 10 (`_draw_frame` clear-to-EOS for shrinking frames, `FROZEN` marker, resize via per-frame `get_terminal_size`).
- **CLI** → Task 8 (all flags, `parse_windows`), Task 9 (`--once`/piped auto-select).
- **Testing** → Tasks 1–9, 11 cover windowed rate (late/young/<2), history evict/drop, promotion (CPU/RSS/gate/kthread), ordering, ANSI truncate/clip, window parsing, cache TTL.
- **Backward compatibility** → Task 9 (`--once` reproduces psf's one-shot substance with windowed CPU + retained avg + promoted procs).

Known limitations recorded for reviewers: top-level reorder uses windowed smoothing only (no hysteresis); group-sum does not itself promote sub-threshold members (decision/limitation B6 from the review).
