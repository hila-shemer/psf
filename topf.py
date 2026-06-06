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
import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, namedtuple
from dataclasses import dataclass, field


# --- config -----------------------------------------------------------------

CMD_WIDTH = 50            # chars of cmdline shown per process
COLLAPSE_THRESHOLD = 20   # kept-descendant count above which a subtree collapses
CACHE_TTL = 30            # seconds before a cached socket entry is re-probed
REPR_COMMS = 4            # distinct comms named in a collapse summary
SAMPLE_INTERVAL = 0.2     # seconds slept to measure current CPU (0 disables)
PATH_HEAD = 2             # leading path components kept when compressing
PATH_MAX_BASENAME = 30    # basename length above which it is itself shortened
PATH_BASENAME_KEEP = 6    # chars kept each side of '...' in a long basename
DEDUP_MIN = 3             # min identical siblings to merge into one ×N group
NEVER_MERGE = frozenset({"claude"})   # comms never grouped, even when identical
GROUP_PIDS = 8            # member pids listed on a group's detail line
LIFECYCLE_MAX = 40        # max born (and max died) comm-groups listed
# Graduated tint for the cpu/rss detail bits. A value's level = how many of its
# (exponential) anchors it clears; the level indexes TINT_SGR. Level 0 is the
# dim baseline shared by the rest of the line; higher levels add a faint warm
# tint, then full color, then bold red so heavy consumers pop. CPU anchors are
# in cores (1.0 == one full core / 100%, independent of how many cores exist).
TINT_SGR = ("2", "2;33", "33", "1;31")          # dim, dim-yellow, yellow, bold-red
RSS_TINT_ANCHORS = (100 * 1024**2, 1024**3, 5 * 1024**3)   # 100M, 1G, 5G
CPU_TINT_ANCHORS = (0.10, 1.0, 4.0)                        # 10%, 100%, 400%

# System constants, read once. CLK_TCK converts stat jiffies -> seconds;
# PAGE_SIZE converts stat rss (in pages) -> bytes.
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# Resolved-once view of system state needed to turn raw counters into rates.
SysInfo = namedtuple("SysInfo", "clk_tck page_size uptime")

# One /proc/PID/stat row, only the fields we use.
Stat = namedtuple("Stat", "comm state ppid num_threads starttime "
                          "utime stime rss_pages")

# A merged set of >= DEDUP_MIN near-identical sibling Procs.
Group = namedtuple("Group", "members")

# Each matcher: (label, target, regex) where target is "comm" or "cmdline".
DEFAULT_MATCHERS = [
    ("bazel", "comm", re.compile(r"^bazel")),
    ("bazel", "cmdline", re.compile(r"\bbazel\(")),
    ("sshd", "cmdline", re.compile(r"^sshd: ")),
    ("tmux", "comm", re.compile(r"^tmux")),
    ("claude", "comm", re.compile(r"claude")),
    ("claude", "cmdline", re.compile(r"\bclaude\b")),
]

# ---------------------------------------------------------------------------


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    cmdline: str
    state: str
    num_threads: int
    starttime: int
    uid: int
    utime: int = 0                  # user jiffies (stat field 14)
    stime: int = 0                  # system jiffies (stat field 15)
    rss_pages: int = 0              # resident pages (stat field 24)
    cpu_current: float = None       # recent CPU fraction from probe sampling
    children: list = field(default_factory=list)   # list[Proc]
    interesting: bool = False
    kept: bool = False
    collapsed: bool = False         # this node's filler descendants are summarized
    collapse_note: str = ""         # histogram summary line for collapsed nodes
    cwd: str = None
    exe: str = None
    sockets_str: str = ""           # rendered socket summary for this process


# --- pure core: parsing -----------------------------------------------------


def parse_stat(content):
    """Parse /proc/PID/stat text into a Stat. comm may contain spaces and
    parens, so split on the LAST ')'."""
    open_paren = content.index("(")
    close_paren = content.rindex(")")
    comm = content[open_paren + 1:close_paren]
    rest = content[close_paren + 2:].split()
    # rest[i] is stat field (i + 3): field3=state, field4=ppid, field14=utime,
    # field15=stime, field20=num_threads, field22=starttime, field24=rss.
    return Stat(
        comm=comm,
        state=rest[0],
        ppid=int(rest[1]),
        num_threads=int(rest[17]),
        starttime=int(rest[19]),
        utime=int(rest[11]),
        stime=int(rest[12]),
        rss_pages=int(rest[21]),
    )


def clean_cmdline(raw, comm=""):
    """Turn raw /proc/PID/cmdline (NUL-separated) into a readable string.
    Kernel threads and zombies have an empty cmdline -> show [comm]."""
    s = raw.replace("\0", " ").strip()
    if s:
        return s
    return "[%s]" % comm if comm else ""


# --- pure core: tree --------------------------------------------------------


def build_tree(procs):
    """Populate .children from .ppid. Return list of root Procs (ppid not in
    the set, or ppid 0) sorted by pid."""
    for p in procs.values():
        p.children = []
    roots = []
    for p in procs.values():
        parent = procs.get(p.ppid)
        if parent is not None and parent is not p:
            parent.children.append(p)
        else:
            roots.append(p)
    for p in procs.values():
        p.children.sort(key=lambda c: c.pid)
    roots.sort(key=lambda r: r.pid)
    return roots


# --- pure core: selection ---------------------------------------------------


def is_interesting(proc, matchers):
    for _label, target, rx in matchers:
        hay = proc.comm if target == "comm" else proc.cmdline
        if rx.search(hay or ""):
            return True
    return False


def _descendants(proc):
    out = []
    stack = list(proc.children)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


def select(procs, matchers):
    """Mark .interesting and .kept. Kept = interesting roots + their
    descendants + their ancestors (so the tree stays rooted). Kernel-thread
    subtrees (under pid 2) are never kept unless explicitly matched."""
    kthreadd = procs.get(2)
    kthread_pids = set()
    if kthreadd is not None:
        kthread_pids = {2} | {d.pid for d in _descendants(kthreadd)}

    for p in procs.values():
        p.interesting = is_interesting(p, matchers) and p.pid not in kthread_pids
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


# --- pure core: collapse ----------------------------------------------------


def collapse(procs, threshold=COLLAPSE_THRESHOLD):
    """For each kept node whose kept-descendant count exceeds threshold, set
    .collapsed and a .collapse_note histogram, and add its *non-interesting*
    kept descendants to the suppressed set (interesting descendants stay
    visible). Returns the set of suppressed pids."""
    suppressed = set()
    for p in procs.values():
        if not p.kept:
            continue
        kept_desc = [d for d in _descendants(p) if d.kept]
        if len(kept_desc) <= threshold:
            continue
        hide = [d for d in kept_desc if not d.interesting and d.pid not in suppressed]
        if len(hide) <= threshold:
            continue
        p.collapsed = True
        suppressed.update(d.pid for d in hide)
        hist = Counter(d.comm for d in hide)
        top = ", ".join("%s×%d" % (c, n)
                        for c, n in hist.most_common(REPR_COMMS))
        extra = len(hist) - REPR_COMMS
        if extra > 0:
            top += ", …"
        p.collapse_note = "… (+%d descendants: %s)" % (len(hide), top)
    return suppressed


# --- pure core: socket parsing ----------------------------------------------


_TCP_STATES = {"0A": "LISTEN", "01": "ESTAB"}


def parse_net_tcp(content, ipv6=False):
    """Parse /proc/net/tcp or tcp6. Return {inode: (proto, state, port)}.
    Addresses are hex; the port is the hex part after ':' in local_address."""
    proto = "tcp6" if ipv6 else "tcp"
    out = {}
    for line in content.splitlines()[1:]:        # skip header
        f = line.split()
        if len(f) < 10:
            continue
        local = f[1]
        st = f[3]
        inode = int(f[9])
        if inode == 0:
            continue
        port = int(local.rsplit(":", 1)[1], 16)
        out[inode] = (proto, _TCP_STATES.get(st, st), port)
    return out


def parse_net_udp(content, ipv6=False):
    """Parse /proc/net/udp(6). UDP has no LISTEN state; report the bound port
    as state 'UDP'."""
    proto = "udp6" if ipv6 else "udp"
    out = {}
    for line in content.splitlines()[1:]:
        f = line.split()
        if len(f) < 10:
            continue
        inode = int(f[9])
        if inode == 0:
            continue
        port = int(f[1].rsplit(":", 1)[1], 16)
        out[inode] = (proto, "UDP", port)
    return out


def parse_net_unix(content):
    """Parse /proc/net/unix. Return {inode: ('unix', path)} for NAMED sockets
    only (unnamed sockets carry no useful info for our summary)."""
    out = {}
    for line in content.splitlines()[1:]:
        f = line.split()
        if len(f) < 8:                # column 8 (path) absent => unnamed
            continue
        inode = int(f[6])
        out[inode] = ("unix", f[7])
    return out


def format_sockets(inodes, netmap):
    """Summarize a process's socket inodes against a merged netmap.
    Listening/UDP ports shown explicitly; established TCP counted; named unix
    paths listed. Returns a single compact string ('' if nothing matched)."""
    listen_ports = set()
    est = 0
    unix_paths = []
    for ino in inodes:
        entry = netmap.get(ino)
        if entry is None:
            continue
        if entry[0] == "unix":
            unix_paths.append(entry[1])
        elif entry[1] == "ESTAB":
            est += 1
        else:                          # LISTEN or UDP
            listen_ports.add(entry[2])
    parts = []
    if listen_ports:
        parts.append("LISTEN " + " ".join(":%d" % p for p in sorted(listen_ports)))
    if est:
        parts.append("+%d est" % est)
    for path in sorted(set(unix_paths)):
        parts.append("unix:" + path)
    return "  ".join(parts)


# --- pure core: resource stats ----------------------------------------------


def lifetime_secs(starttime, uptime, clk_tck):
    """Wall-clock seconds the process has been alive: system uptime minus the
    process's start offset (stat starttime is in clock ticks since boot)."""
    return uptime - starttime / clk_tck


def cpu_fraction(cpu_ticks, wall_secs, clk_tck):
    """CPU busy fraction = cpu-seconds / wall-seconds. 1.0 == one core saturated
    (can exceed 1.0 across cores). None when the window is non-positive."""
    if wall_secs <= 0:
        return None
    return (cpu_ticks / clk_tck) / wall_secs


def fmt_pct(frac):
    """Format a CPU fraction as a percentage with magnitude-scaled precision so
    tiny lifetime averages stay legible (e.g. 0.0002 -> '0.02%') instead of
    rounding to '0.00%'. None -> None; non-positive -> '0%'."""
    if frac is None:
        return None
    pct = frac * 100.0
    if pct <= 0:
        return "0%"
    if pct >= 10:
        return "%.0f%%" % pct
    if pct >= 1:
        return "%.1f%%" % pct
    # sub-1%: two significant figures in plain decimal (never sci notation,
    # which would be unreadable in a process monitor), trailing zeros trimmed.
    decimals = 1 - int(math.floor(math.log10(pct)))
    return ("%.*f" % (decimals, pct)).rstrip("0").rstrip(".") + "%"


def fmt_bytes(n):
    """Human-readable binary size: 0 -> '0', <1KiB -> 'NB', else one decimal
    with a K/M/G/T suffix."""
    if n <= 0:
        return "0"
    if n < 1024:
        return "%dB" % n
    val = float(n)
    for unit in ("K", "M", "G", "T"):
        val /= 1024.0
        if val < 1024 or unit == "T":
            return "%.1f%s" % (val, unit)


def fmt_duration(secs):
    """Compact elapsed time, top two units: '5s', '1m5s', '1h2m', '1d1h'."""
    secs = int(secs) if secs > 0 else 0
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, sec = divmod(rem, 60)
    if days:
        return "%dd%dh" % (days, hours)
    if hours:
        return "%dh%dm" % (hours, mins)
    if mins:
        return "%dm%ds" % (mins, sec)
    return "%ds" % sec


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


def _dominant_parent(members, parents):
    """The most common parent comm among members (None if unknown)."""
    counts = Counter(parents.get(m.ppid) for m in members)
    counts.pop(None, None)
    return counts.most_common(1)[0][0] if counts else None


def _lifecycle_side(procs, sign, parents, sysinfo, is_died):
    """One 'born'/'died' line body: group by comm (×N), largest first, each
    annotated with its dominant parent comm; singletons show pid + comm and,
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


# --- cache ------------------------------------------------------------------


class Cache:
    """Cross-run cache of resolved socket summaries, keyed by (pid, starttime).
    Freshness = fd count matches AND entry age < CACHE_TTL. A changed boot_id
    invalidates the whole file. `now` is injectable for testing."""

    def __init__(self, path, boot_id, now, ttl=CACHE_TTL):
        self.path = path
        self.boot_id = boot_id
        self.now = now
        self.ttl = ttl
        self._loaded = {}      # key "pid:start" -> {fdcount, sockets, ts}
        self._fresh = {}       # entries probed/validated this run, to persist
        self._load()

    def _load(self):
        try:
            with open(self.path) as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return
        if blob.get("boot_id") != self.boot_id:
            return
        self._loaded = blob.get("entries", {})

    @staticmethod
    def _key(pid, starttime):
        return "%d:%d" % (pid, starttime)

    def get(self, pid, starttime, fdcount):
        """Return cached socket summary if fresh, else None."""
        e = self._loaded.get(self._key(pid, starttime))
        if e is None:
            return None
        if e.get("fdcount") != fdcount:
            return None
        if self.now - e.get("ts", 0) >= self.ttl:
            return None
        self._fresh[self._key(pid, starttime)] = e   # carry forward
        return e.get("sockets")

    def put(self, pid, starttime, fdcount, sockets):
        self._fresh[self._key(pid, starttime)] = {
            "fdcount": fdcount, "sockets": sockets, "ts": self.now}

    def save(self, live_keys):
        """Persist only entries whose (pid, starttime) is still live."""
        live = {self._key(p, s) for (p, s) in live_keys}
        entries = {k: v for k, v in self._fresh.items() if k in live}
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump({"boot_id": self.boot_id, "entries": entries}, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass     # cache is best-effort; never fail the run over it


# --- I/O shell: live /proc readers ------------------------------------------

PROC = "/proc"


def _read(path):
    with open(path) as fh:
        return fh.read()


def scan():
    """One cheap pass over /proc. Returns {pid: Proc} with stat/cmdline/uid
    filled; cwd/exe/sockets are deferred to probe()."""
    procs = {}
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        pid = int(name)
        base = "%s/%d" % (PROC, pid)
        try:
            st = parse_stat(_read(base + "/stat"))
            cmdline = clean_cmdline(_read(base + "/cmdline"), st.comm)
            uid = os.stat(base).st_uid
        except (OSError, ValueError, IndexError):
            continue        # process vanished mid-scan, or unreadable
        procs[pid] = Proc(pid=pid, ppid=st.ppid, comm=st.comm, cmdline=cmdline,
                          state=st.state, num_threads=st.num_threads,
                          starttime=st.starttime, uid=uid,
                          utime=st.utime, stime=st.stime,
                          rss_pages=st.rss_pages)
    return procs


def fd_socket_inodes(pid):
    """Return (fd_count, {socket_inodes}) for a pid. fd_count is the cheap
    freshness fingerprint (one readdir, no readlinks beyond socket fds)."""
    fddir = "%s/%d/fd" % (PROC, pid)
    try:
        fds = os.listdir(fddir)
    except OSError:
        return 0, set()
    inodes = set()
    for fd in fds:
        try:
            target = os.readlink("%s/%s" % (fddir, fd))
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(int(target[8:-1]))
    return len(fds), inodes


def read_links(pid):
    """Return (cwd, exe) via readlink, or '?' for whichever is unreadable."""
    base = "%s/%d" % (PROC, pid)

    def link(name):
        try:
            return os.readlink(base + "/" + name)
        except OSError:
            return "?"
    return link("cwd"), link("exe")


def netns_of(pid):
    try:
        return os.readlink("%s/%d/ns/net" % (PROC, pid))
    except OSError:
        return ""


def read_boot_id():
    try:
        return _read("/proc/sys/kernel/random/boot_id").strip()
    except OSError:
        return ""


def read_uptime():
    """System uptime in seconds (first field of /proc/uptime)."""
    try:
        return float(_read("/proc/uptime").split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _parents_map(*snaps):
    """pid -> comm across one or more {pid: Proc} snapshots (for annotating the
    parent of a born/died process)."""
    out = {}
    for snap in snaps:
        for p in snap.values():
            out[p.pid] = p.comm
    return out


# --- probe orchestration ----------------------------------------------------


def resolve_netmaps(pids_by_ns):
    """For each network namespace, read /proc/<rep>/net/* ONCE (any pid in the
    ns sees the same tables) and merge into one inode->desc map per ns. Each
    file is read independently so a missing tcp6/udp6 (IPv6 off) doesn't drop
    the others. Input: {netns_id: [pids]}. Output: {netns_id: {inode: desc}}."""
    out = {}
    for ns, pids in pids_by_ns.items():
        # pick a representative whose net tables are readable
        rep = next((p for p in pids
                    if os.path.exists("%s/%d/net/tcp" % (PROC, p))), pids[0])
        base = "%s/%d/net" % (PROC, rep)
        netmap = {}
        for fn, parser in (
            ("tcp", lambda c: parse_net_tcp(c, False)),
            ("tcp6", lambda c: parse_net_tcp(c, True)),
            ("udp", lambda c: parse_net_udp(c, False)),
            ("udp6", lambda c: parse_net_udp(c, True)),
            ("unix", parse_net_unix),
        ):
            try:
                netmap.update(parser(_read(base + "/" + fn)))
            except OSError:
                pass
        out[ns] = netmap
    return out


def probe(nodes, cache, resolver=resolve_netmaps):
    """Fill cwd/exe/sockets_str for the given printed nodes. Sockets come from
    the cache when fd count matches and the entry is fresh; otherwise the node
    is grouped by netns and resolved in one batch. `resolver` is injectable."""
    # cwd/exe are cheap single readlinks -> always re-read.
    miss = []                       # (node, fdcount, inodes)
    pids_by_ns = {}
    for node in nodes:
        node.cwd, node.exe = read_links(node.pid)
        fdcount, inodes = fd_socket_inodes(node.pid)
        cached = cache.get(node.pid, node.starttime, fdcount)
        if cached is not None:
            node.sockets_str = cached
            continue
        ns = netns_of(node.pid)
        pids_by_ns.setdefault(ns, []).append(node.pid)
        miss.append((node, fdcount, inodes))

    netmaps = resolver(pids_by_ns) if pids_by_ns else {}
    for node, fdcount, inodes in miss:
        ns = netns_of(node.pid)
        summary = format_sockets(inodes, netmaps.get(ns, {}))
        node.sockets_str = summary
        cache.put(node.pid, node.starttime, fdcount, summary)


# --- render -----------------------------------------------------------------

HOME = os.path.expanduser("~")


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


def compress_cmdline(cmdline, width):
    """Fit a process cmdline into `width` while keeping the binary name and at
    least the start of its arguments. argv[0] is often a long absolute path
    (e.g. a bazelisk download dir), so blindly truncating shows only path and
    drops the real command — instead compress argv[0]'s path the way exe/cwd
    are compressed, and if that still leaves no room for an argument, fall back
    to the bare (and, if huge, shortened) basename before truncating."""
    if not cmdline or cmdline.startswith("["):
        return cmdline[:width]
    head, sep, rest = cmdline.partition(" ")
    argv0 = compress_path(head)
    if not sep:
        return argv0[:width]
    line = argv0 + " " + rest
    if len(line) <= width:
        return line
    base = _compress_basename(head.rsplit("/", 1)[-1])
    return (base + " " + rest)[:width]


def _tint_level(value, anchors):
    """Number of anchors `value` meets or exceeds -> a tint level (0..len).
    0 is the dim baseline; higher anchors are exponential, so the tint only
    strengthens by orders of magnitude. None/0 -> baseline."""
    if not value:
        return 0
    return sum(1 for a in anchors if value >= a)


def _cpu_bit(node, sysinfo):
    """('cpu 3.4% (2.5% avg)', level) when a current sample exists, else
    ('cpu 2.5% avg', …). avg = lifetime CPU rate (total cpu-time / time alive).
    The tint level tracks live load (current sample, or avg when none) against
    CPU_TINT_ANCHORS. Returns None when no rate is computable."""
    life = lifetime_secs(node.starttime, sysinfo.uptime, sysinfo.clk_tck)
    avg_frac = cpu_fraction(node.utime + node.stime, life, sysinfo.clk_tck)
    avg = fmt_pct(avg_frac)
    cur = fmt_pct(node.cpu_current)
    if avg is None:
        return None
    live = node.cpu_current if node.cpu_current is not None else avg_frac
    level = _tint_level(live, CPU_TINT_ANCHORS)
    if cur is not None:
        return ("cpu %s (%s avg)" % (cur, avg), level)
    return ("cpu %s avg" % avg, level)


def _compose_dim(bits, color):
    """Join (text, level) bits into one detail line. Without color, plain text.
    With color, each bit is wrapped in its tint level's SGR code (TINT_SGR);
    level 0 is dim, so all-baseline lines look exactly as before while heavy
    cpu/rss bits gain a graduated warm tint."""
    if not color:
        return "  ".join(t for t, _ in bits)
    segs = ["\x1b[%sm%s\x1b[0m" % (TINT_SGR[lvl], t) for t, lvl in bits]
    return "\x1b[2m  \x1b[0m".join(segs)


def _detail(node, color, sysinfo=None):
    bits = []   # (text, tint level)
    if node.cwd and node.cwd not in ("?", ""):
        bits.append(("cwd:" + compress_path(node.cwd), 0))
    if node.exe and node.exe not in ("?", ""):
        bits.append(("exe:" + compress_path(node.exe), 0))
    if node.sockets_str:
        bits.append((node.sockets_str, 0))
    if sysinfo is not None:
        cpu = _cpu_bit(node, sysinfo)
        if cpu is not None:
            bits.append(cpu)
        rss = node.rss_pages * sysinfo.page_size
        if rss > 0:
            bits.append(("rss:" + fmt_bytes(rss),
                         _tint_level(rss, RSS_TINT_ANCHORS)))
        bits.append(("up:" + fmt_duration(
            lifetime_secs(node.starttime, sysinfo.uptime, sysinfo.clk_tck)), 0))
    if node.num_threads > 1:
        bits.append(("%d threads" % node.num_threads, 0))
    if not bits:
        return None
    return _compose_dim(bits, color)


def _visible_children(node, suppressed):
    return [c for c in node.children if c.kept and c.pid not in suppressed]


def _group_label(members, width):
    return brace_summary([m.cmdline for m in members])[:width]


def _group_detail(members, color, sysinfo):
    """Aggregated detail line for a merged group: shared/braced cwd & exe,
    member pids, and cpu/rss/up ranges (when sysinfo is given)."""
    bits = []   # (text, tint level)
    for attr, prefix in (("cwd", "cwd:"), ("exe", "exe:")):
        vals = [getattr(m, attr) for m in members
                if getattr(m, attr) and getattr(m, attr) not in ("?", "")]
        if vals:
            bits.append((prefix + brace_summary([compress_path(v)
                                                 for v in vals]), 0))
    pids = sorted(m.pid for m in members)
    extra = " +%d" % (len(pids) - GROUP_PIDS) if len(pids) > GROUP_PIDS else ""
    bits.append(("pids:" + " ".join(str(x) for x in pids[:GROUP_PIDS]) + extra,
                 0))
    if sysinfo is not None:
        avgs = [a for a in (
            cpu_fraction(m.utime + m.stime,
                         lifetime_secs(m.starttime, sysinfo.uptime,
                                       sysinfo.clk_tck), sysinfo.clk_tck)
            for m in members) if a is not None]
        if avgs:
            curs = [m.cpu_current for m in members if m.cpu_current is not None]
            # Tint tracks the group's combined live load (the whole ×N entry).
            live_total = sum(curs) if curs else sum(avgs)
            level = _tint_level(live_total, CPU_TINT_ANCHORS)
            if curs:
                bits.append(("cpu %s (%s avg)" % (range_str(curs, fmt_pct),
                                                  range_str(avgs, fmt_pct)),
                             level))
            else:
                bits.append(("cpu %s avg" % range_str(avgs, fmt_pct), level))
        rss = [m.rss_pages * sysinfo.page_size for m in members
               if m.rss_pages > 0]
        if rss:
            bits.append(("rss:" + range_str(rss, fmt_bytes),
                         _tint_level(sum(rss), RSS_TINT_ANCHORS)))
        lifes = [lifetime_secs(m.starttime, sysinfo.uptime, sysinfo.clk_tck)
                 for m in members]
        bits.append(("up:" + range_str(lifes, fmt_duration), 0))
    threads = max(m.num_threads for m in members)
    if threads > 1:
        bits.append(("%d threads" % threads, 0))
    return _compose_dim(bits, color)


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
                                      compress_cmdline(item.cmdline, width))
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


def glossary(color):
    """A short legend printed at the head of the output explaining the
    annotations (notably what '+N est' means). Returns a list of lines."""
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
    if color:
        lines = ["\x1b[2m%s\x1b[0m" % ln for ln in lines]
    return lines


# --- CLI --------------------------------------------------------------------


def cache_path():
    """Resolve the cache file. Under sudo, write to the invoking user's cache
    dir (via SUDO_USER) rather than root's."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    sudo_user = os.environ.get("SUDO_USER")
    if xdg:
        base = xdg
    elif sudo_user:
        import pwd
        base = os.path.join(pwd.getpwnam(sudo_user).pw_dir, ".cache")
    else:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "psf", "cache.json")


def collect_printed(roots, suppressed):
    """The nodes render() will actually print -> the only ones to deep-probe."""
    out = []

    def walk(node):
        if not node.kept or node.pid in suppressed:
            return
        out.append(node)
        for c in node.children:
            walk(c)
    for r in roots:
        walk(r)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Focused process snapshot.")
    ap.add_argument("-w", "--width", type=int, default=CMD_WIDTH,
                    help="cmdline chars per process (default %d)" % CMD_WIDTH)
    ap.add_argument("-t", "--threshold", type=int, default=COLLAPSE_THRESHOLD,
                    help="collapse subtrees with more kept descendants")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore and do not write the socket cache")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--no-glossary", action="store_true",
                    help="suppress the legend printed at the head of output")
    ap.add_argument("-s", "--sample-interval", type=float,
                    default=SAMPLE_INTERVAL,
                    help="seconds slept to measure current CPU (default %.2g; "
                         "0 disables the current-CPU sample)" % SAMPLE_INTERVAL)
    ap.add_argument("--no-dedup", action="store_true",
                    help="do not merge near-identical sibling subtrees")
    ap.add_argument("--dedup-min", type=int, default=DEDUP_MIN,
                    help="min identical siblings to merge (default %d)"
                         % DEDUP_MIN)
    ap.add_argument("--no-lifecycle", action="store_true",
                    help="suppress the born/died section")
    args = ap.parse_args(argv)

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


if __name__ == "__main__":
    main()
