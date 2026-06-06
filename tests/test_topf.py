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
