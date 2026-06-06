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
