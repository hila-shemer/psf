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
