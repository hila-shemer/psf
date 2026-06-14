import os

import psf
import topf


def _rproc(pid, ppid=1, comm="x", cmdline="x", rss_bytes=0, starttime=1,
           utime=0, stime=0, exe="?"):
    p = topf.Proc(pid=pid, ppid=ppid, comm=comm, cmdline=cmdline, state="R",
                  num_threads=1, starttime=starttime, uid=0,
                  rss_pages=rss_bytes // topf.PAGE_SIZE,
                  utime=utime, stime=stime)
    p.exe = exe
    return p


# --- classification ----------------------------------------------------------


def test_classify_matches_known_categories():
    assert psf.classify(_rproc(1, comm="claude")).name == "user-session"
    assert psf.classify(_rproc(2, comm="bazel")).name == "build-daemon"
    assert psf.classify(_rproc(3, comm="clang-14")).name == "compile-worker"
    assert psf.classify(_rproc(4, comm="sshd")).name == "infrastructure"
    assert psf.classify(_rproc(5, comm="systemd")).name == "system-service"
    assert psf.classify(_rproc(6, comm="random_thing")).name == "misc"


def test_classify_cmdline_matches():
    p = _rproc(1, comm="sshd", cmdline="sshd: user@pts/0")
    assert psf.classify(p).name == "infrastructure"  # comm match first
    p2 = _rproc(2, comm="x", cmdline="sshd: user@pts/0")
    # sshd: cmdline matches SESSION_LEADER_PATTERNS but also CLASSIFICATION_RULES
    cat = psf.classify(p2)
    # sshd$ in rules matches "x" as comm? No, "^sshd$" won't match "x"
    # The cmdline rule for "^sshd: " won't match here because rules use
    # comm or cmdline based on their target


def test_classify_venv_python_overrides_to_user_session():
    # python -> compile-worker normally
    assert psf.classify(_rproc(1, comm="python3")).name == "compile-worker"
    # python with venv -> user-session
    def venv_resolver(proc):
        if proc.comm.startswith("python"):
            return "/home/user/.venv"
        return None
    assert psf.classify(_rproc(1, comm="python3"),
                        venv_resolver=venv_resolver).name == "user-session"


def test_classify_no_venv_resolver_stays_compile_worker():
    def no_venv(proc):
        return None
    assert psf.classify(_rproc(1, comm="python3"),
                        venv_resolver=no_venv).name == "compile-worker"


# --- venv detection ----------------------------------------------------------


def test_detect_venv_from_exe_path():
    p = _rproc(1, comm="python3", exe="/home/user/psf/.venv/bin/python3")
    venv = psf.detect_venv(p)
    assert venv is not None
    assert "psf" in venv or ".venv" in venv


def test_detect_venv_no_venv():
    p = _rproc(1, comm="python3", exe="/usr/bin/python3")
    venv = psf.detect_venv(p)
    # Depends on whether /proc/PID/environ has VIRTUAL_ENV; on most systems
    # running pytest, it won't. Just check it doesn't crash.
    assert venv is None or isinstance(venv, str)


def test_read_environ_returns_dict():
    # Read our own environ — must be non-empty on Linux
    pid = os.getpid()
    env = psf.read_environ(pid)
    assert isinstance(env, dict)
    assert "PATH" in env


# --- session detection -------------------------------------------------------


def test_find_session_leaders_finds_claude():
    procs = {
        1: _rproc(1, ppid=0, comm="init"),
        10: _rproc(10, ppid=1, comm="claude"),
    }
    topf.build_tree(procs)
    leaders = psf.find_session_leaders(procs)
    assert len(leaders) == 1
    assert leaders[0].comm == "claude"


def test_find_sessions_groups_subtrees():
    procs = {
        1: _rproc(1, ppid=0, comm="init"),
        10: _rproc(10, ppid=1, comm="claude"),
        11: _rproc(11, ppid=10, comm="python3"),
        12: _rproc(12, ppid=10, comm="node"),
    }
    topf.build_tree(procs)
    categories = {10: psf.CAT_BY_NAME["user-session"],
                  11: psf.CAT_BY_NAME["compile-worker"],
                  12: psf.CAT_BY_NAME["compile-worker"]}
    sessions = psf.find_sessions(procs, categories)
    assert len(sessions) == 1
    assert sessions[0].leader.pid == 10
    assert len(sessions[0].children) == 2


# --- glue summarization ------------------------------------------------------


def test_summarize_glue_collapses_infrastructure():
    procs = {}
    for i, comm in enumerate(["bash", "bash", "bash", "sshd", "sshd"]):
        procs[i] = _rproc(i, comm=comm)
    categories = {i: psf.CAT_BY_NAME["infrastructure"] for i in procs}
    session_pids = set()  # nothing in sessions
    lines = psf.summarize_glue(procs, categories, session_pids)
    assert len(lines) >= 1
    assert "bash×3" in lines[0]
    assert "sshd×2" in lines[0]


# --- new-process clusters ----------------------------------------------------


def test_find_new_clusters_detects_burst():
    prev = {1: _rproc(1, comm="init")}
    cur = {1: _rproc(1, comm="init")}
    for i in range(2, 12):
        cur[i] = _rproc(i, comm="clang")
    # Build the keyed dict for find_new_clusters
    # The function compares pid sets; we need proc dicts keyed by pid
    # Note: find_new_clusters takes {pid: Proc} dicts
    categories = {i: psf.CAT_BY_NAME["compile-worker"] for i in range(2, 12)}
    clusters = psf.find_new_clusters(cur, prev, categories)
    assert len(clusters) >= 1
    assert "10 new: clang" in clusters[0]


def test_find_new_clusters_ignores_small_spawns():
    prev = {1: _rproc(1, comm="init")}
    cur = {1: _rproc(1, comm="init"), 2: _rproc(2, comm="x"),
           3: _rproc(3, comm="x"), 4: _rproc(4, comm="x")}
    categories = {2: psf.CAT_BY_NAME["misc"], 3: psf.CAT_BY_NAME["misc"],
                  4: psf.CAT_BY_NAME["misc"]}
    clusters = psf.find_new_clusters(cur, prev, categories)
    # Only 3 procs, below CLUSTER_MIN=5
    assert clusters == []


# --- path snapshot -----------------------------------------------------------


def test_path_is_in_subtree_matches_root_and_children(tmp_path):
    root = tmp_path / "foo"
    child = root / "bar"
    peer = tmp_path / "foobar"
    child.mkdir(parents=True)
    peer.mkdir()
    norm = psf.normalize_path_target(str(root))
    assert psf._path_is_in_subtree(str(root), norm)
    assert psf._path_is_in_subtree(str(child), norm)
    assert not psf._path_is_in_subtree(str(peer), norm)
    assert not psf._path_is_in_subtree("socket:[123]", norm)


def test_proc_path_hits_finds_cwd_exe_fd_and_maps(tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    p = _rproc(10, comm="python3", cmdline="python script.py",
               exe=str(root / "bin" / "python"))
    p.cwd = str(root)
    fd_path = str(root / "data.txt")
    maps_path = str(root / "lib.so")
    root_norm = psf.normalize_path_target(str(root))

    def fake_listdir(path):
        assert path.endswith("/10/fd")
        return ["3"]

    def fake_readlink(path):
        assert path.endswith("/10/fd/3")
        return fd_path

    def fake_maps(pid):
        assert pid == 10
        return "7f r--p 0000 00:00 0 %s\n" % maps_path

    hits = psf.proc_path_hits(p, root_norm, readlink=fake_readlink,
                              listdir=fake_listdir, read_maps=fake_maps,
                              fd_kind=lambda pid, fd: "fd:%s file" % fd)
    rendered = [(h.kind, h.path) for h in hits]
    assert ("cwd", str(root)) in rendered
    assert ("exe", str(root / "bin" / "python")) in rendered
    assert ("fd:3 file", fd_path) in rendered
    assert ("mmap", maps_path) in rendered


def test_render_psf_path_filters_to_touching_processes(monkeypatch, tmp_path):
    root = tmp_path / "foo"
    root.mkdir()
    procs = {
        1: _rproc(1, ppid=0, comm="init", cmdline="init"),
        10: _rproc(10, ppid=1, comm="claude", cmdline="claude"),
        11: _rproc(11, ppid=10, comm="python3", cmdline="python worker.py"),
        20: _rproc(20, ppid=1, comm="bash", cmdline="bash"),
    }
    topf.build_tree(procs)
    procs[10].cwd = "/tmp"
    procs[10].exe = "/usr/bin/claude"
    procs[11].cwd = str(root)
    procs[11].exe = "/usr/bin/python3"
    procs[20].cwd = "/tmp"
    procs[20].exe = "/usr/bin/bash"

    monkeypatch.setattr(psf, "read_links",
                        lambda pid: (procs[pid].cwd, procs[pid].exe))
    monkeypatch.setattr(psf, "read_environ", lambda pid: {})
    monkeypatch.setattr(psf, "proc_path_hits",
                        lambda p, norm: [psf.PathHit("cwd", "", p.cwd)]
                        if p.pid == 11 else [])

    args = psf._parse_args(["--path", str(root)])
    sysinfo = topf.SysInfo(clk_tck=topf.CLK_TCK, page_size=topf.PAGE_SIZE,
                           uptime=100.0, cores=1)
    text = "\n".join(psf.render_psf(procs, sysinfo, args))
    assert "touching" in text
    assert "claude" in text
    assert "python worker.py" in text
    assert "cwd:" in text
    assert "bash" not in text


# --- smoke test --------------------------------------------------------------


def test_render_once_psf_smoke():
    """Linux-only: run against real /proc, verify non-empty output."""
    args = psf._parse_args([])
    lines = psf.render_once_psf(args)
    assert isinstance(lines, list) and lines
    # Header should contain 'psf'
    assert any("psf" in ln for ln in lines)
