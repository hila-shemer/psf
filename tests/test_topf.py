import os

import topf


def test_import_smoke():
    assert hasattr(topf, "render")
    assert hasattr(topf, "scan")


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


def test_cores_count_positive():
    assert topf.cores_count() >= 1


def test_render_once_smoke():
    # Drive render_once against the real /proc but with a tiny interval; assert
    # it returns a non-empty list of strings and includes the header.
    lines = topf.render_once(interval=0.05, args=topf._once_defaults())
    assert isinstance(lines, list) and lines
    assert any(ln.startswith("topf —") for ln in lines)


def test_cache_get_expires_with_advancing_now(tmp_path):
    # Mirror the live loop: frame 1 probes + saves; later frames build a fresh
    # Cache that loads from the file and must honour TTL against ITS now.
    path = str(tmp_path / "cache.json")
    c1 = topf.Cache(path=path, boot_id="b", now=100.0, ttl=30)
    c1.put(5, 1, fdcount=3, sockets="LISTEN :22")
    c1.save(live_keys={(5, 1)})

    # a frame 5s later: within TTL -> hit
    c2 = topf.Cache(path=path, boot_id="b", now=105.0, ttl=30)
    assert c2.get(5, 1, 3) == "LISTEN :22"

    # a frame 200s later: beyond TTL -> miss (get reads c.now, not a frozen value)
    c3 = topf.Cache(path=path, boot_id="b", now=300.0, ttl=30)
    assert c3.get(5, 1, 3) is None

    # a stale fd count also misses
    c4 = topf.Cache(path=path, boot_id="b", now=105.0, ttl=30)
    assert c4.get(5, 1, 99) is None


# --- vmstat parsing ---------------------------------------------------------


def test_parse_proc_stat_counters_basic():
    txt = ("cpu  100 5 30 1000 20 1 2 0 0 0\n"
           "cpu0 50 2 15 500 10 0 1 0 0 0\n"
           "intr 12345 0 0\n"
           "ctxt 67890\n"
           "procs_running 3\n"
           "procs_blocked 1\n")
    c = topf.parse_proc_stat_counters(txt)
    assert c["cpu_user"] == 100 and c["cpu_nice"] == 5
    assert c["cpu_system"] == 30 and c["cpu_idle"] == 1000 and c["cpu_iowait"] == 20
    assert c["cpu_total"] == 100 + 5 + 30 + 1000 + 20 + 1 + 2  # all fields on the cpu line
    assert c["intr"] == 12345 and c["ctxt"] == 67890
    assert c["procs_running"] == 3 and c["procs_blocked"] == 1


def test_parse_proc_stat_counters_missing_fields_are_none():
    c = topf.parse_proc_stat_counters("cpu 1 1 1 1 1\n")
    assert c["intr"] is None and c["procs_blocked"] is None


def test_parse_meminfo_to_bytes():
    txt = "MemFree:  1024 kB\nBuffers: 2048 kB\nCached: 4096 kB\nSwapTotal: 0 kB\n"
    m = topf.parse_meminfo(txt)
    assert m["free"] == 1024 * 1024 and m["buff"] == 2048 * 1024
    assert m["cache"] == 4096 * 1024 and m["swap_total"] == 0


def test_parse_vmstat_counters_basic():
    txt = "pgpgin 10\npgpgout 20\npswpin 3\npswpout 4\nnr_free_pages 999\n"
    v = topf.parse_vmstat_counters(txt)
    assert v["pgpgin"] == 10 and v["pgpgout"] == 20
    assert v["pswpin"] == 3 and v["pswpout"] == 4


def test_parse_net_dev_sums_excluding_lo():
    txt = ("Inter-|   Receive                    |  Transmit\n"
           " face |bytes    packets ... |bytes    packets ...\n"
           "    lo: 500 1 0 0 0 0 0 0 600 1 0 0 0 0 0 0\n"
           "  eth0: 1000 5 0 0 0 0 0 0 2000 7 0 0 0 0 0 0\n"
           "  eth1: 30 1 0 0 0 0 0 0 40 1 0 0 0 0 0 0\n")
    rx, tx = topf.parse_net_dev(txt)
    assert rx == 1000 + 30 and tx == 2000 + 40   # lo excluded


# --- vmstat sample model ----------------------------------------------------


def _vs(t, **kw):
    base = dict(procs_running=0, procs_blocked=0, cpu_user=0, cpu_nice=0,
                cpu_system=0, cpu_idle=0, cpu_iowait=0, cpu_total=0, intr=0,
                ctxt=0, pgpgin=0, pgpgout=0, pswpin=0, pswpout=0, rx=0, tx=0,
                free=0, buff=0, cache=0, swap_total=0)
    base.update(kw)
    return topf.VmstatSample(t=t, **base)


def test_vmstat_rate_rows_deltas_per_second():
    a = _vs(0.0, pgpgin=0, pgpgout=0, rx=0, tx=0, intr=0, ctxt=0,
            cpu_user=0, cpu_total=0, procs_running=2)
    b = _vs(2.0, pgpgin=2048, pgpgout=0, rx=4000, tx=8000, intr=200, ctxt=400,
            cpu_user=50, cpu_total=100, procs_running=3)
    rows = topf.vmstat_rate_rows([a, b])
    assert len(rows) == 1
    row = rows[0]
    assert row["r"] == 3                       # instantaneous (from newest)
    assert row["bi"] == 2048 * 1024 / 2.0      # pgpgin kB -> bytes/s
    assert row["ni"] == 4000 / 2.0 and row["no"] == 8000 / 2.0
    assert row["in"] == 200 / 2.0 and row["cs"] == 400 / 2.0
    assert row["us"] == 50.0                    # 50 of 100 total jiffies -> 50%


def test_vmstat_rate_rows_needs_two_samples():
    assert topf.vmstat_rate_rows([_vs(0.0)]) == []


def test_vmstat_rate_rows_none_counter_gives_none_cell():
    a = _vs(0.0, intr=None)
    b = _vs(1.0, intr=None)
    assert topf.vmstat_rate_rows([a, b])[0]["in"] is None


def test_fmt_count():
    assert topf.fmt_count(0) == "0"
    assert topf.fmt_count(950) == "950"
    assert topf.fmt_count(9100) == "9.1k"
    assert topf.fmt_count(44000) == "44k"
    assert topf.fmt_count(None) == "—"


# --- vmstat outlier coloring ------------------------------------------------


def test_outlier_level_flat_window_is_zero():
    assert topf.outlier_level(5, [5, 5, 5, 5]) == 0
    assert topf.outlier_level(99, [5, 5, 5]) == 0     # zero spread -> no tint


def test_outlier_level_spike_is_high():
    window = [10, 11, 9, 10, 200]            # 200 is a gross outlier
    assert topf.outlier_level(200, window) == 3


def test_outlier_level_small_deviation_is_zero():
    window = [10, 11, 9, 10, 12]
    assert topf.outlier_level(11, window) == 0


def test_outlier_level_too_few_or_none():
    assert topf.outlier_level(5, [5, 5]) == 0        # < 3 samples
    assert topf.outlier_level(None, [1, 2, 3, 4]) == 0


# --- vmstat pane rendering --------------------------------------------------


def _rate_row(**kw):
    row = {k: 0 for k, _h, _ki in topf.VMSTAT_COLS}
    row.update(kw)
    return row


def test_format_vmstat_pane_header_and_swap_off():
    rows = [_rate_row(free=3 * 1024**3, bi=0, ni=1024**2)]
    rows[0]["in"] = 9100                 # "in" is an ordinary dict key here
    lines = topf.format_vmstat_pane(rows, swap_on=False, width=200, height=4,
                                    color=False)
    header = lines[0]
    assert header.startswith(topf.VMSTAT_GUTTER)
    assert " si " not in header and " so " not in header   # swap off -> dropped
    assert " ni " in header and " no " in header           # network present
    assert " us " in header and " id " in header


def test_format_vmstat_pane_swap_on_includes_si_so():
    lines = topf.format_vmstat_pane([_rate_row()], swap_on=True, width=200,
                                    height=3, color=False)
    assert " si " in lines[0] and " so " in lines[0]


def test_format_vmstat_pane_uses_human_units():
    rows = [_rate_row(free=2 * 1024**3, ni=4 * 1024**2)]
    lines = topf.format_vmstat_pane(rows, swap_on=False, width=200, height=3,
                                    color=False)
    body = lines[-1]
    assert "2.0G" in body and "4.0M" in body


def test_format_vmstat_pane_dashes_when_empty():
    lines = topf.format_vmstat_pane([], swap_on=False, width=200, height=3,
                                    color=False)
    assert lines and lines[0].startswith(topf.VMSTAT_GUTTER)   # header still drawn
    assert len(lines) == 1                                     # no data rows


# --- row identities & collapse/expand ---------------------------------------


def test_proc_and_group_id():
    p = _rproc(7, starttime=3, comm="clang")
    p.exe = "/usr/bin/clang"
    assert topf.proc_id(p) == ("p", 7, 3)
    assert topf.group_id(topf.ROOT_ID, "clang", "/usr/bin/clang") == \
        ("g", topf.ROOT_ID, "clang", "/usr/bin/clang")


def _kept(p):
    p.kept = True
    return p


def test_collapse_returns_collapsible_and_suppresses():
    root = _kept(_rproc(1, ppid=0, comm="root"))
    root.interesting = True
    kids = {1: root}
    for i in range(2, 8):                       # 6 noise children > threshold 3
        c = _kept(_rproc(i, ppid=1, comm="noise"))
        kids[i] = c
    topf.build_tree(kids)
    suppressed, collapsible = topf.collapse(kids, threshold=3)
    assert topf.proc_id(root) in collapsible
    assert len(suppressed) == 6


def test_collapse_expanded_node_not_suppressed_but_still_collapsible():
    root = _kept(_rproc(1, ppid=0, comm="root"))
    root.interesting = True
    kids = {1: root}
    for i in range(2, 8):
        kids[i] = _kept(_rproc(i, ppid=1, comm="noise"))
    topf.build_tree(kids)
    suppressed, collapsible = topf.collapse(
        kids, threshold=3, expanded={topf.proc_id(root)})
    assert topf.proc_id(root) in collapsible    # still a candidate
    assert suppressed == set()                  # but nothing hidden


# --- build_rows / render wrapper --------------------------------------------


def test_build_rows_proc_is_selectable_detail_is_not():
    p = _rproc(20, ppid=0, comm="hot", windows=[5.0, 0, 0])
    procs = {20: p}
    topf.build_tree(procs)
    p.kept = True
    rows = topf.build_rows([p], set(), sysinfo=None)
    heads = [r for r in rows if r.selectable]
    assert len(heads) == 1
    assert heads[0].item_id == topf.proc_id(p)
    assert heads[0].selectable and not heads[0].expandable


def test_build_rows_group_is_expandable_with_group_id():
    members = {i: _rproc(i, ppid=1, comm="clang") for i in range(10, 14)}
    for m in members.values():
        m.kept = True
        m.exe = "/usr/bin/clang"
    root = _rproc(1, ppid=0, comm="root")
    root.kept = True
    procs = {1: root, **members}
    topf.build_tree(procs)
    rows = topf.build_rows([root], set(), dedup_min=3)
    groups = [r for r in rows if r.expandable and r.item_id[0] == "g"]
    assert len(groups) == 1
    gid = topf.group_id(topf.proc_id(root), "clang", "/usr/bin/clang")
    assert groups[0].item_id == gid


def test_build_rows_expanded_group_shows_members():
    members = {i: _rproc(i, ppid=1, comm="clang") for i in range(10, 14)}
    for m in members.values():
        m.kept = True
        m.exe = "/usr/bin/clang"
    root = _rproc(1, ppid=0, comm="root")
    root.kept = True
    procs = {1: root, **members}
    topf.build_tree(procs)
    gid = topf.group_id(topf.proc_id(root), "clang", "/usr/bin/clang")
    rows = topf.build_rows([root], set(), dedup_min=3, expanded={gid})
    member_ids = {topf.proc_id(m) for m in members.values()}
    sel_ids = {r.item_id for r in rows if r.selectable}
    assert member_ids <= sel_ids        # all 4 members now individual rows
    assert gid in sel_ids               # group header still present (re-collapse target)


def test_render_still_returns_strings():
    cold = _rproc(10, ppid=0, comm="cold", windows=[0.1, 0, 0])
    hot = _rproc(20, ppid=0, comm="hot", windows=[5.0, 0, 0])
    procs = {10: cold, 20: hot}
    topf.build_tree(procs)
    for p in procs.values():
        p.kept = True
    key = lambda item: topf.subtree_window_cpu(
        item.members[0] if isinstance(item, topf.Group) else item, 0)
    lines = topf.render([cold, hot], set(), top_sort_key=key)
    assert all(isinstance(ln, str) for ln in lines)
    assert lines[0].endswith("hot")
