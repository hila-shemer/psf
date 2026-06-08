#!/usr/bin/env python3
"""psf — process session finder: classify, group, and summarize running processes.

Shows what user sessions are running where, classifies process types, detects
virtual environments. Not windowed (lifetime CPU average). Snapshot mode by
default; --watch for continuous refresh.
"""
import argparse
import os
import re
import sys
import time
from collections import Counter, namedtuple
from dataclasses import dataclass, field

from topf import (parse_stat, clean_cmdline, build_tree, scan, read_uptime,
                  cores_count, read_links, fmt_bytes, fmt_count, fmt_duration,
                  fmt_pct, compress_path, compress_cmdline, Proc, SysInfo,
                  parse_meminfo, read_loadavg, count_states,
                  lifetime_secs, cpu_fraction, read_boot_id,
                  parse_proc_stat_counters, parse_vmstat_counters,
                  is_promoted, _descendants, proc_id)

# --- local I/O (topf's _read is private) ------------------------------------

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
PROC = "/proc"


def _read_text(path):
    """Read a /proc file as text. Returns '' on failure."""
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _read_bin(path):
    """Read a /proc file as bytes (for environ). Returns b'' on failure."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


# --- config -----------------------------------------------------------------

CMD_WIDTH = 60           # chars of cmdline shown per process
GLUE_COMMS = frozenset({
    "sshd", "bash", "zsh", "sh", "dash", "tmux", "login", "systemd",
    "systemd-user", "dbus-daemon", "cron", "agetty",
})
SESSION_LEADER_PATTERNS = [
    ("comm", re.compile(r"claude")),
    ("cmdline", re.compile(r"^sshd: ")),
    ("comm", re.compile(r"^tmux.*server")),
    ("comm", re.compile(r"^vim$")),
    ("comm", re.compile(r"^nvim$")),
    ("comm", re.compile(r"^emacs$")),
    ("cmdline", re.compile(r"\bcode\b")),
]
VENV_PATH_MARKERS = (".venv/bin/", "env/bin/", "venv/bin/")

Category = namedtuple("Category", "name badge priority")
CATEGORIES = [
    Category("user-session",  "usr", 5),
    Category("build-daemon",  "bld", 4),
    Category("compile-worker", "cmp", 3),
    Category("infrastructure", "inf", 2),
    Category("system-service", "sys", 1),
    Category("misc",          "mis", 0),
]
CAT_BY_NAME = {c.name: c for c in CATEGORIES}

CLASSIFICATION_RULES = [
    # (target, compiled_regex, category_name)
    ("comm", re.compile(r"^claude"),             "user-session"),
    ("comm", re.compile(r"^vim$"),               "user-session"),
    ("comm", re.compile(r"^nvim$"),              "user-session"),
    ("comm", re.compile(r"^emacs"),              "user-session"),
    ("cmdline", re.compile(r"\bcode\b"),         "user-session"),
    ("comm", re.compile(r"^bazel"),              "build-daemon"),
    ("cmdline", re.compile(r"\bbazel\("),        "build-daemon"),
    ("comm", re.compile(r"^buck"),               "build-daemon"),
    ("comm", re.compile(r"^gradle"),             "build-daemon"),
    ("comm", re.compile(r"^ninja"),              "build-daemon"),
    ("comm", re.compile(r"^clang"),              "compile-worker"),
    ("comm", re.compile(r"^cc1"),                "compile-worker"),
    ("comm", re.compile(r"^gcc"),                "compile-worker"),
    ("comm", re.compile(r"^g\+\+"),              "compile-worker"),
    ("comm", re.compile(r"^javac"),              "compile-worker"),
    ("comm", re.compile(r"^rustc"),              "compile-worker"),
    ("comm", re.compile(r"^go$"),                "compile-worker"),
    ("comm", re.compile(r"^python"),             "compile-worker"),
    ("comm", re.compile(r"^node"),               "compile-worker"),
    ("comm", re.compile(r"^java$"),              "build-daemon"),
    ("comm", re.compile(r"^systemd$"),           "system-service"),
    ("comm", re.compile(r"^dbus"),               "system-service"),
    ("comm", re.compile(r"^cron"),               "system-service"),
    ("comm", re.compile(r"^sshd$"),              "infrastructure"),
    ("comm", re.compile(r"^bash$"),              "infrastructure"),
    ("comm", re.compile(r"^zsh$"),               "infrastructure"),
    ("comm", re.compile(r"^tmux"),              "infrastructure"),
    ("comm", re.compile(r"^login"),              "infrastructure"),
]

CLUSTER_MIN = 5     # min same-comm procs to be called a "cluster"


# --- classification ---------------------------------------------------------


def classify(proc, matchers=None, venv_resolver=None):
    """Return Category for a process. First matching rule wins.
    Python processes in a venv override from compile-worker to user-session.
    venv_resolver is a callable(proc)->str|None that returns the venv path."""
    if matchers is None:
        matchers = CLASSIFICATION_RULES
    for target, rx, cat_name in matchers:
        hay = proc.comm if target == "comm" else proc.cmdline
        if rx.search(hay or ""):
            cat = CAT_BY_NAME[cat_name]
            # Override: python in a venv -> user-session, not compile-worker
            if cat_name == "compile-worker" and proc.comm.startswith("python"):
                if venv_resolver is not None:
                    venv = venv_resolver(proc)
                    if venv:
                        return CAT_BY_NAME["user-session"]
            return cat
    return CAT_BY_NAME["misc"]


# --- venv detection ---------------------------------------------------------


def read_environ(pid):
    """Read /proc/PID/environ as dict. Returns {} on failure."""
    raw = _read_bin("%s/%d/environ" % (PROC, pid))
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return {}
    return dict(pair.split("=", 1) for pair in text.split("\0") if "=" in pair)


def detect_venv(proc):
    """Detect if a process is running inside a Python venv.
    Returns the venv base path (str) or None. Checks VIRTUAL_ENV,
    CONDA_PREFIX in environ, and .venv/bin/ in exe path."""
    env = read_environ(proc.pid)
    if "VIRTUAL_ENV" in env:
        return env["VIRTUAL_ENV"]
    if "CONDA_PREFIX" in env:
        return env["CONDA_PREFIX"]
    exe = proc.exe or ""
    for marker in VENV_PATH_MARKERS:
        idx = exe.find(marker)
        if idx >= 0:
            return exe[:idx].rstrip("/")
    return None


# --- session detection -------------------------------------------------------

SessionInfo = namedtuple("SessionInfo", "leader children venv category")


def find_session_leaders(procs):
    """Identify session leader processes. Returns [Proc] of leaders found."""
    leaders = []
    for p in procs.values():
        for target, rx in SESSION_LEADER_PATTERNS:
            hay = p.comm if target == "comm" else p.cmdline
            if rx.search(hay or ""):
                leaders.append(p)
                break
    # Sort by pid for stable output
    leaders.sort(key=lambda p: p.pid)
    return leaders


def find_sessions(procs, categories, venv_map=None):
    """Build sessions from session leaders + their subtrees.
    Returns [SessionInfo] sorted by category priority (desc) then pid."""
    leaders = find_session_leaders(procs)
    sessions = []
    seen_pids = set()
    for leader in leaders:
        if leader.pid in seen_pids:
            continue
        children = [d for d in _descendants(leader) if d.pid not in seen_pids]
        seen_pids.add(leader.pid)
        seen_pids.update(d.pid for d in children)
        cat = categories.get(leader.pid, CAT_BY_NAME["misc"])
        venv = venv_map.get(leader.pid) if venv_map else None
        # A claude session inherits the venv from its python children
        if venv is None and venv_map:
            for c in children:
                if venv_map.get(c.pid):
                    venv = venv_map[c.pid]
                    break
        sessions.append(SessionInfo(leader=leader, children=children,
                                    venv=venv, category=cat))
    # Sort: highest priority first, pid tiebreak
    sessions.sort(key=lambda s: (-s.category.priority, s.leader.pid))
    return sessions


# --- glue summarization ------------------------------------------------------


def summarize_glue(procs, categories, session_pids):
    """Produce summary lines for infrastructure/system-service procs not in
    sessions. Returns a list of formatted summary strings."""
    buckets = {}  # (badge, comm) -> [Proc]
    for p in procs.values():
        if p.pid in session_pids:
            continue
        cat = categories.get(p.pid, CAT_BY_NAME["misc"])
        if cat.name in ("infrastructure", "system-service"):
            buckets.setdefault((cat.badge, p.comm), []).append(p)
    if not buckets:
        return []
    lines = []
    for (badge, comm), members in sorted(buckets.items()):
        lines.append("%s:%s×%d" % (badge, comm, len(members)))
    return ["  ".join(lines)]


# --- new-process clusters ----------------------------------------------------


def find_new_clusters(cur, prev, categories):
    """Find bursts of new processes with the same comm. Returns [str] summary
    lines for clusters of >= CLUSTER_MIN same-comm procs that appeared.
    cur and prev are {pid: Proc} dicts."""
    if prev is None:
        return []
    cur_keys = {(p.pid, p.starttime) for p in cur.values()}
    prev_keys = {(p.pid, p.starttime) for p in prev.values()}
    born_keys = cur_keys - prev_keys
    if not born_keys:
        return []
    # Build lookup from (pid, starttime) to Proc
    cur_by_key = {(p.pid, p.starttime): p for p in cur.values()}
    born = [cur_by_key[k] for k in born_keys if k in cur_by_key]
    # Group by comm
    comm_groups = {}
    for p in born:
        comm_groups.setdefault(p.comm, []).append(p)
    lines = []
    for comm, members in sorted(comm_groups.items(), key=lambda x: -len(x[1])):
        if len(members) >= CLUSTER_MIN:
            cat = categories.get(members[0].pid, CAT_BY_NAME["misc"])
            lines.append("+%d new: %s [%s]" % (len(members), comm, cat.badge))
    return lines


# --- render -----------------------------------------------------------------


def psf_header(sysinfo, procs, categories):
    """Two-line header: system summary + task breakdown."""
    loadavg = read_loadavg()
    n_run, n_sleep, n_zombie = count_states(procs)
    # meminfo
    try:
        mem = parse_meminfo(_read_text("/proc/meminfo"))
    except Exception:
        mem = {}
    mem_total = mem.get("mem_total")
    mem_used = None
    if mem_total is not None:
        mem_used = mem_total - (mem.get("free") or 0) - (mem.get("buff") or 0) \
                   - (mem.get("cache") or 0)
    swap_total = mem.get("swap_total", 0)
    swap_free = mem.get("swap_free")

    parts = ["psf — %d cores" % sysinfo.cores]
    if loadavg and loadavg[0] is not None:
        parts.append("load %.2f/%.2f/%.2f" % (loadavg[0], loadavg[1], loadavg[2]))
    if mem_total is not None:
        parts.append("Mem: %s/%s" % (fmt_bytes(mem_used), fmt_bytes(mem_total)))
    if swap_total and swap_total > 0:
        sw_used = swap_total - (swap_free or 0)
        parts.append("Swap: %s/%s" % (fmt_bytes(sw_used), fmt_bytes(swap_total)))
    line1 = "  ".join(parts)
    task_parts = ["%d procs" % len(procs)]
    if n_run:
        task_parts.append("%d run" % n_run)
    if n_sleep:
        task_parts.append("%d sleep" % n_sleep)
    if n_zombie:
        task_parts.append("%d zombie" % n_zombie)
    line2 = "(%s)" % ", ".join(task_parts)
    return line1 + "\n" + line2


def _proc_detail(proc, sysinfo, venv=None, show_venv=False):
    """One-line detail for a process: cpu rss up [venv]."""
    bits = []
    life = lifetime_secs(proc.starttime, sysinfo.uptime, sysinfo.clk_tck)
    avg = cpu_fraction(proc.utime + proc.stime, life, sysinfo.clk_tck)
    bits.append("cpu %s" % (fmt_pct(avg) or "—"))
    rss = proc.rss_pages * sysinfo.page_size
    if rss > 0:
        bits.append("rss %s" % fmt_bytes(rss))
    bits.append("up %s" % fmt_duration(life))
    if proc.num_threads > 1:
        bits.append("%d threads" % proc.num_threads)
    if (show_venv or venv) and venv:
        bits.append("venv:%s" % compress_path(venv))
    return "  ".join(bits)


def _group_detail(members, sysinfo, cat):
    """Aggregated detail for a category group of children."""
    bits = ["×%d" % len(members)]
    # CPU range
    avgs = []
    for m in members:
        life = lifetime_secs(m.starttime, sysinfo.uptime, sysinfo.clk_tck)
        a = cpu_fraction(m.utime + m.stime, life, sysinfo.clk_tck)
        if a is not None:
            avgs.append(a)
    if avgs:
        lo, hi = min(avgs), max(avgs)
        cpu = fmt_pct(lo) if lo == hi else "%s–%s" % (fmt_pct(lo), fmt_pct(hi))
        bits.append("cpu %s" % cpu)
    # RSS range
    rss_vals = [m.rss_pages * sysinfo.page_size for m in members if m.rss_pages > 0]
    if rss_vals:
        if min(rss_vals) == max(rss_vals):
            bits.append("rss %s" % fmt_bytes(min(rss_vals)))
        else:
            bits.append("rss %s–%s" % (fmt_bytes(min(rss_vals)),
                                         fmt_bytes(max(rss_vals))))
    # Pids
    pids = sorted(m.pid for m in members)
    if len(pids) <= 4:
        bits.append("pids " + " ".join(str(x) for x in pids))
    else:
        bits.append("pids %s +%d" % (" ".join(str(x) for x in pids[:4]),
                                       len(pids) - 4))
    return "  ".join(bits)


def _children_by_category(children, categories):
    """Group children by category, return [(Category, [Proc])] sorted by
    priority desc."""
    buckets = {}
    for c in children:
        cat = categories.get(c.pid, CAT_BY_NAME["misc"])
        buckets.setdefault(cat, []).append(c)
    return sorted(buckets.items(), key=lambda kv: (-kv[0].priority, kv[0].name))


def render_session(session, sysinfo, args, categories, venv_map):
    """Render one session as lines: leader + categorized children."""
    leader = session.leader
    cat = session.category
    venv = session.venv or (venv_map.get(leader.pid) if venv_map else None)
    lines = []
    # Leader line: [badge] pid comm  detail
    leader_line = "[%s] %d %s" % (
        cat.badge, leader.pid,
        compress_cmdline(leader.cmdline, args.width))
    detail = _proc_detail(leader, sysinfo, venv=venv, show_venv=args.venv)
    if detail:
        leader_line += "  " + detail
    lines.append(leader_line)
    # Children, grouped by category
    by_cat = _children_by_category(session.children, categories)
    for child_cat, members in by_cat:
        if child_cat.name in ("infrastructure", "system-service") and not args.show_all:
            # Summarize glue within a session as a count
            lines.append("  ├─ [%s] %s×%d (summarized)" % (
                child_cat.badge,
                Counter(m.comm for m in members).most_common(1)[0][0],
                len(members)))
            continue
        # Group by comm within the category
        comm_groups = {}
        for m in members:
            comm_groups.setdefault(m.comm, []).append(m)
        for comm, comm_members in sorted(comm_groups.items()):
            if len(comm_members) >= 3 and not args.show_all:
                # Collapsed group
                detail = _group_detail(comm_members, sysinfo, child_cat)
                lines.append("  ├─ [%s] %s %s" % (
                    child_cat.badge, comm, detail))
            else:
                for m in comm_members:
                    m_venv = venv_map.get(m.pid) if venv_map else None
                    m_line = "  ├─ [%s] %d %s" % (
                        child_cat.badge, m.pid,
                        compress_cmdline(m.cmdline, args.width))
                    m_detail = _proc_detail(m, sysinfo, venv=m_venv,
                                            show_venv=args.venv)
                    if m_detail:
                        m_line += "  " + m_detail
                    lines.append(m_line)
    return lines


def render_psf(procs, sysinfo, args, prev=None):
    """Build the output lines for psf: header + sessions + glue + clusters."""
    # Deep-probe exe for all processes (psf needs exe for venv detection
    # and classification, not just for printed ones like topf)
    venv_map = {}
    for p in procs.values():
        p.cwd, p.exe = read_links(p.pid)
        if p.comm.startswith("python"):
            venv_map[p.pid] = detect_venv(p)

    # Classify all processes
    venv_resolver = detect_venv if not args.show_all else detect_venv
    categories = {}
    for p in procs.values():
        categories[p.pid] = classify(p, venv_resolver=venv_resolver)

    # Find sessions
    sessions = find_sessions(procs, categories, venv_map)
    session_pids = set()
    for s in sessions:
        session_pids.add(s.leader.pid)
        session_pids.update(d.pid for d in s.children)

    # Build header
    lines = [psf_header(sysinfo, procs, categories), ""]

    # Render each session
    for session in sessions:
        lines += render_session(session, sysinfo, args, categories, venv_map)
        lines.append("")

    # Glue summaries
    glue = summarize_glue(procs, categories, session_pids)
    if glue:
        lines += glue
        lines.append("")

    # New-process clusters
    clusters = find_new_clusters(procs, prev, categories)
    if clusters:
        lines += clusters
        lines.append("")

    # Strip trailing blank lines
    while lines and lines[-1] == "":
        lines.pop()
    return lines


# --- CLI --------------------------------------------------------------------


def _parse_args(argv):
    ap = argparse.ArgumentParser(prog="psf",
                                 description="Process session finder.")
    ap.add_argument("-w", "--width", type=int, default=CMD_WIDTH,
                    help="cmdline chars per process (default %d)" % CMD_WIDTH)
    ap.add_argument("--once", action="store_true",
                    help="single snapshot and exit (default when piped)")
    ap.add_argument("--watch", action="store_true",
                    help="continuous refresh mode (simple reprint)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="refresh interval in seconds (default 2.0)")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--show-all", action="store_true",
                    help="show every process, not just sessions + glue")
    ap.add_argument("--venv", action="store_true",
                    help="always show venv for python processes")
    return ap.parse_args(argv)


def render_once_psf(args):
    """Take a single snapshot and return lines."""
    procs = scan()
    sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE,
                      uptime=read_uptime(), cores=cores_count())
    return render_psf(procs, sysinfo, args)


def main(argv=None):
    args = _parse_args(argv)
    use_once = args.once or not sys.stdout.isatty()
    if use_once:
        lines = render_once_psf(args)
        print("\n".join(lines))
        return
    # --watch mode: simple reprint loop
    color = not args.no_color
    prev = None
    try:
        while True:
            lines = render_once_psf(args)
            if color and sys.stdout.isatty():
                sys.stdout.write("\x1b[H\x1b[J")
            print("\n".join(lines))
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
