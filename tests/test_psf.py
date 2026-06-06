import os
import tempfile
import unittest

import psf


# --- helpers ----------------------------------------------------------------


def _p(pid, ppid, comm="x", **kw):
    kw.setdefault("cmdline", comm)
    kw.setdefault("state", "S")
    kw.setdefault("num_threads", 1)
    kw.setdefault("starttime", pid * 10)
    kw.setdefault("uid", 0)
    return psf.Proc(pid=pid, ppid=ppid, comm=comm, **kw)


# --- Task 1: stat parsing ---------------------------------------------------


class TestParseStat(unittest.TestCase):
    def test_parse_stat_basic(self):
        # fields: 1 pid, 2 (comm), 3 state, 4 ppid, 14 utime, 15 stime,
        # 20 num_threads, 22 starttime, 24 rss (pages)
        line = ("4242 (test app) S 1 4242 4242 0 -1 4194304 100 0 0 0 "
                "10 5 0 0 20 0 7 0 99999 0 0")
        st = psf.parse_stat(line)
        self.assertEqual(st.comm, "test app")
        self.assertEqual(st.state, "S")
        self.assertEqual(st.ppid, 1)
        self.assertEqual(st.num_threads, 7)
        self.assertEqual(st.starttime, 99999)
        self.assertEqual(st.utime, 10)
        self.assertEqual(st.stime, 5)
        self.assertEqual(st.rss_pages, 0)

    def test_parse_stat_comm_with_parens(self):
        line = ("99 (weird )(name) R 2 0 0 0 -1 0 0 0 0 0 "
                "0 0 0 0 20 0 1 0 555 0 0")
        st = psf.parse_stat(line)
        self.assertEqual(st.comm, "weird )(name")
        self.assertEqual(st.ppid, 2)
        self.assertEqual(st.starttime, 555)

    def test_parse_stat_cpu_and_rss(self):
        # utime=200 stime=50 num_threads=4 starttime=1234 rss=4096 pages
        line = ("100 (proc) R 1 100 100 0 -1 0 0 0 0 0 "
                "200 50 0 0 20 0 4 0 1234 999 4096")
        st = psf.parse_stat(line)
        self.assertEqual(st.utime, 200)
        self.assertEqual(st.stime, 50)
        self.assertEqual(st.num_threads, 4)
        self.assertEqual(st.starttime, 1234)
        self.assertEqual(st.rss_pages, 4096)


# --- Task 2: cmdline + tree -------------------------------------------------


class TestCleanCmdline(unittest.TestCase):
    def test_nul_separated(self):
        self.assertEqual(psf.clean_cmdline("bazel\0build\0//foo\0"),
                         "bazel build //foo")

    def test_empty_falls_back_to_comm(self):
        self.assertEqual(psf.clean_cmdline("", comm="kworker"), "[kworker]")


class TestBuildTree(unittest.TestCase):
    def test_children_and_roots(self):
        procs = {1: _p(1, 0), 10: _p(10, 1), 11: _p(11, 1), 100: _p(100, 10)}
        roots = psf.build_tree(procs)
        self.assertEqual([r.pid for r in roots], [1])
        self.assertEqual(sorted(c.pid for c in procs[1].children), [10, 11])
        self.assertEqual([c.pid for c in procs[10].children], [100])

    def test_orphan_is_root(self):
        # ppid points outside the set (parent already reaped) -> treated as root
        procs = {500: _p(500, 999), 501: _p(501, 500)}
        roots = psf.build_tree(procs)
        self.assertEqual([r.pid for r in roots], [500])


# --- Task 3: selection ------------------------------------------------------


class TestSelection(unittest.TestCase):
    def test_is_interesting_matches_comm_and_cmdline(self):
        m = psf.DEFAULT_MATCHERS
        self.assertTrue(psf.is_interesting(_p(1, 0, comm="tmux: server"), m))
        self.assertTrue(psf.is_interesting(
            _p(2, 0, comm="java", cmdline="bazel(myworkspace)"), m))
        self.assertFalse(psf.is_interesting(_p(3, 0, comm="systemd-journald"), m))

    def test_keeps_interesting_descendants_and_ancestors(self):
        # init -> sshd -> bash -> claude ; plus an unrelated daemon
        procs = {
            1: _p(1, 0, comm="systemd"),
            5: _p(5, 1, comm="sshd", cmdline="sshd: shemer@pts/0"),
            6: _p(6, 5, comm="bash"),
            7: _p(7, 6, comm="claude"),
            9: _p(9, 1, comm="crond"),
        }
        psf.build_tree(procs)
        psf.select(procs, psf.DEFAULT_MATCHERS)
        kept = {pid for pid, p in procs.items() if p.kept}
        self.assertEqual(kept, {1, 5, 6, 7})   # ancestor 1, sshd subtree; not crond
        self.assertTrue(procs[7].interesting)  # claude
        self.assertFalse(procs[6].interesting)  # bash kept only as path member

    def test_kernel_threads_excluded(self):
        procs = {1: _p(1, 0, comm="systemd"),
                 2: _p(2, 0, comm="kthreadd"),
                 3: _p(3, 2, comm="ksoftirqd/0")}
        psf.build_tree(procs)
        psf.select(procs, psf.DEFAULT_MATCHERS)
        self.assertFalse(any(p.kept for p in procs.values()))


# --- Task 4: collapse -------------------------------------------------------


class TestCollapse(unittest.TestCase):
    def test_big_subtree_collapses_with_histogram(self):
        procs = {1: _p(1, 0, comm="bazel", cmdline="bazel build //...")}
        procs[1].interesting = True
        pid = 100
        for comm, n in (("cc1plus", 5), ("ld", 3)):
            for _ in range(n):
                procs[pid] = _p(pid, 1, comm=comm)
                pid += 1
        psf.build_tree(procs)
        for p in procs.values():
            p.kept = True
        suppressed = psf.collapse(procs, threshold=4)
        root = procs[1]
        self.assertTrue(root.collapsed)
        self.assertIn("+8", root.collapse_note)       # 8 descendants summarized
        self.assertIn("cc1plus", root.collapse_note)
        self.assertEqual(len(suppressed), 8)

    def test_small_subtree_not_collapsed(self):
        procs = {1: _p(1, 0, comm="tmux"), 2: _p(2, 1, comm="bash")}
        procs[1].interesting = True
        psf.build_tree(procs)
        for p in procs.values():
            p.kept = True
        suppressed = psf.collapse(procs, threshold=4)
        self.assertFalse(procs[1].collapsed)
        self.assertEqual(suppressed, set())

    def test_interesting_descendant_is_never_suppressed(self):
        procs = {1: _p(1, 0, comm="tmux")}
        procs[1].interesting = True
        for i in range(10):
            procs[200 + i] = _p(200 + i, 1, comm="bash")
        procs[300] = _p(300, 1, comm="claude")
        procs[300].interesting = True
        psf.build_tree(procs)
        for p in procs.values():
            p.kept = True
        suppressed = psf.collapse(procs, threshold=4)
        self.assertTrue(procs[1].collapsed)
        self.assertNotIn(300, suppressed)             # claude stays visible


# --- Task 5: socket parsing -------------------------------------------------


class TestSocketParsing(unittest.TestCase):
    # Real /proc/net/tcp layout: inode is field index 9. tx:rx and tr:tm->when
    # are each a single colon-joined field.
    TCP = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode\n"
        "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 "
        "00000000   100        0 1111 1 0000000000000000 100 0 0 10 0\n"
        "   1: 0100007F:8A9C 0100007F:1F90 01 00000000:00000000 00:00000000 "
        "00000000   100        0 2222 1 0000000000000000 100 0 0 10 0\n"
    )
    UNIX = (
        "Num       RefCount Protocol Flags    Type St Inode Path\n"
        "0000: 00000002 00000000 00010000 0001 01 3333 /tmp/tmux-1000/default\n"
        "0000: 00000002 00000000 00010000 0001 01 4444\n"
    )

    def test_parse_tcp_listen_and_established(self):
        m = psf.parse_net_tcp(self.TCP, ipv6=False)
        self.assertEqual(m[1111], ("tcp", "LISTEN", 8080))
        self.assertEqual(m[2222][1], "ESTAB")

    def test_parse_unix_named_only(self):
        m = psf.parse_net_unix(self.UNIX)
        self.assertEqual(m[3333], ("unix", "/tmp/tmux-1000/default"))
        self.assertNotIn(4444, m)            # unnamed unix socket skipped

    def test_format_sockets_summary(self):
        netmap = {
            1111: ("tcp", "LISTEN", 8080),
            2222: ("tcp", "ESTAB", 0),
            3333: ("unix", "/tmp/tmux-1000/default"),
        }
        out = psf.format_sockets({1111, 2222, 3333, 9999}, netmap)
        self.assertEqual(out, "LISTEN :8080  +1 est  unix:/tmp/tmux-1000/default")

    def test_format_sockets_empty(self):
        self.assertEqual(psf.format_sockets(set(), {}), "")


# --- Task 6: cache ----------------------------------------------------------


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "cache.json")

    def test_roundtrip_and_boot_id(self):
        c = psf.Cache(self.path, boot_id="boot-A", now=1000.0)
        c.put(42, 99999, fdcount=7, sockets="LISTEN :22")
        c.save(live_keys={(42, 99999)})

        c2 = psf.Cache(self.path, boot_id="boot-A", now=1005.0)
        self.assertEqual(c2.get(42, 99999, fdcount=7), "LISTEN :22")

        # different boot id -> whole cache invalid
        c3 = psf.Cache(self.path, boot_id="boot-B", now=1005.0)
        self.assertIsNone(c3.get(42, 99999, fdcount=7))

    def test_fdcount_mismatch_is_miss(self):
        c = psf.Cache(self.path, boot_id="b", now=1000.0)
        c.put(42, 99999, fdcount=7, sockets="x")
        c.save(live_keys={(42, 99999)})
        c2 = psf.Cache(self.path, boot_id="b", now=1001.0)
        self.assertIsNone(c2.get(42, 99999, fdcount=8))   # fd count changed

    def test_ttl_expiry_is_miss(self):
        c = psf.Cache(self.path, boot_id="b", now=1000.0)
        c.put(42, 99999, fdcount=7, sockets="x")
        c.save(live_keys={(42, 99999)})
        c2 = psf.Cache(self.path, boot_id="b", now=1000.0 + psf.CACHE_TTL + 1)
        self.assertIsNone(c2.get(42, 99999, fdcount=7))   # too old

    def test_save_drops_dead_entries(self):
        c = psf.Cache(self.path, boot_id="b", now=1000.0)
        c.put(1, 10, fdcount=1, sockets="a")
        c.put(2, 20, fdcount=1, sockets="b")
        c.save(live_keys={(1, 10)})                       # 2 is dead
        c2 = psf.Cache(self.path, boot_id="b", now=1001.0)
        self.assertEqual(c2.get(1, 10, fdcount=1), "a")
        self.assertIsNone(c2.get(2, 20, fdcount=1))

    def test_missing_file_is_empty(self):
        c = psf.Cache(self.path, boot_id="b", now=1.0)
        self.assertIsNone(c.get(1, 1, fdcount=1))


# --- Task 7: live /proc readers ---------------------------------------------


class TestProcIO(unittest.TestCase):
    def test_scan_includes_self(self):
        procs = psf.scan()
        me = procs[os.getpid()]
        self.assertEqual(me.ppid, os.getppid())
        self.assertGreaterEqual(me.num_threads, 1)
        self.assertEqual(me.uid, os.getuid())

    def test_fd_inodes_and_count_for_self(self):
        import socket
        s = socket.socket()
        try:
            count, inodes = psf.fd_socket_inodes(os.getpid())
            self.assertGreater(count, 0)
            self.assertIsInstance(inodes, set)
            self.assertIn(os.fstat(s.fileno()).st_ino, inodes)
        finally:
            s.close()

    def test_links_for_self(self):
        cwd, exe = psf.read_links(os.getpid())
        self.assertEqual(cwd, os.getcwd())
        self.assertTrue(exe.endswith("python3") or "python" in exe)

    def test_boot_id_nonempty(self):
        self.assertTrue(psf.read_boot_id())

    def test_netns_of_self(self):
        self.assertTrue(psf.netns_of(os.getpid()).startswith("net:["))


# --- Task 8: probe orchestration --------------------------------------------


class TestProbeOrchestration(unittest.TestCase):
    def test_probe_uses_cache_on_fdcount_match(self):
        # Build one printed proc; pre-seed cache; probe must NOT call the
        # (exploding) socket resolver when fd count matches.
        p = _p(os.getpid(), os.getppid(), comm="python", cmdline="python psf")
        p.kept = True
        tmp = tempfile.mkdtemp()
        cpath = os.path.join(tmp, "c.json")

        calls = []

        def fake_resolver(pids_by_ns):
            calls.append(pids_by_ns)
            return {}

        cache = psf.Cache(cpath, boot_id="b", now=1000.0)
        count, _ = psf.fd_socket_inodes(p.pid)
        cache.put(p.pid, p.starttime, fdcount=count, sockets="LISTEN :1234")
        cache.save(live_keys={(p.pid, p.starttime)})

        cache2 = psf.Cache(cpath, boot_id="b", now=1001.0)
        psf.probe([p], cache2, resolver=fake_resolver)
        self.assertEqual(p.sockets_str, "LISTEN :1234")
        self.assertEqual(calls, [])        # resolver skipped entirely

    def test_probe_resolves_on_miss(self):
        p = _p(os.getpid(), os.getppid(), comm="python")
        p.kept = True
        tmp = tempfile.mkdtemp()
        cache = psf.Cache(os.path.join(tmp, "c.json"), boot_id="b", now=1.0)
        psf.probe([p], cache)              # real resolver, empty cache
        self.assertIsNotNone(p.cwd)
        self.assertIsNotNone(p.exe)
        self.assertIsInstance(p.sockets_str, str)


# --- Task 9: render ---------------------------------------------------------


class TestRender(unittest.TestCase):
    def test_render_tree_lines(self):
        procs = {
            1: _p(1, 0, comm="sshd", cmdline="sshd: shemer@pts/0"),
            2: _p(2, 1, comm="bash", cmdline="-bash"),
            3: _p(3, 2, comm="claude",
                  cmdline="claude --resume some/long/args/here/that/exceeds/fifty/chars"),
        }
        psf.build_tree(procs)
        for p in procs.values():
            p.kept = True
        procs[3].cwd = "/home/shemer/proj"
        procs[3].exe = "/usr/bin/node"
        procs[3].sockets_str = "+2 est"
        procs[3].num_threads = 11
        lines = psf.render(psf.build_tree(procs), suppressed=set(),
                           width=50, color=False)
        text = "\n".join(lines)
        self.assertIn("sshd: shemer@pts/0", text)
        self.assertIn("claude --resume", text)
        self.assertIn("cwd:~/proj", text)           # cwd shown, HOME summarized
        self.assertIn("node", text)                 # exe shown
        self.assertIn("+2 est", text)               # sockets shown
        self.assertIn("11 threads", text)           # thread count
        # cmdline truncated to width
        claude_line = [ln for ln in lines if "claude --resume" in ln][0]
        self.assertLessEqual(len(claude_line.split("claude")[1]), 60)

    def test_render_collapse_note(self):
        root = _p(1, 0, comm="bazel", cmdline="bazel build //...")
        root.kept = True
        root.collapsed = True
        root.collapse_note = "… (+50 descendants: cc1plus×50)"
        lines = psf.render([root], suppressed=set(), width=50, color=False)
        self.assertTrue(any("+50 descendants" in ln for ln in lines))

    def test_render_hides_non_kept_children(self):
        # PID 1 kept only as an ancestor; its non-kept child (journald) must
        # not leak into the tree, but the kept path to claude must show.
        procs = {
            1: _p(1, 0, comm="systemd", cmdline="/sbin/init"),
            2: _p(2, 1, comm="journald", cmdline="systemd-journald"),
            3: _p(3, 1, comm="tmux", cmdline="tmux"),
            4: _p(4, 3, comm="claude", cmdline="claude"),
        }
        psf.build_tree(procs)
        psf.select(procs, psf.DEFAULT_MATCHERS)
        roots = [r for r in psf.build_tree(procs) if r.kept]
        lines = psf.render(roots, suppressed=set(), width=50, color=False)
        text = "\n".join(lines)
        self.assertIn("tmux", text)
        self.assertIn("claude", text)
        self.assertNotIn("journald", text)        # boring child hidden

    def test_render_skips_suppressed(self):
        procs = {1: _p(1, 0, comm="bazel"), 2: _p(2, 1, comm="cc1plus")}
        psf.build_tree(procs)
        for p in procs.values():
            p.kept = True
        lines = psf.render([procs[1]], suppressed={2}, width=50, color=False)
        self.assertFalse(any("cc1plus" in ln for ln in lines))


# --- resource stats: cpu rate, elapsed, rss ---------------------------------


class TestCpuMath(unittest.TestCase):
    def test_lifetime_secs(self):
        # process started at tick 99999, 100 ticks/sec -> 999.99s after boot;
        # system up 1100s -> ~100.01s alive.
        self.assertAlmostEqual(psf.lifetime_secs(99999, 1100.0, 100), 100.01,
                               places=4)

    def test_cpu_fraction_lifetime_average(self):
        # 250 jiffies @100Hz = 2.5 cpu-seconds over a 100s life -> 0.025.
        self.assertAlmostEqual(psf.cpu_fraction(250, 100.0, 100), 0.025)

    def test_cpu_fraction_zero_wall_is_none(self):
        self.assertIsNone(psf.cpu_fraction(10, 0.0, 100))
        self.assertIsNone(psf.cpu_fraction(10, -1.0, 100))

    def test_cpu_fraction_busy_core(self):
        # 20 jiffies of cpu in a 0.2s window @100Hz = 0.2 cpu-s / 0.2s = 1.0.
        self.assertAlmostEqual(psf.cpu_fraction(20, 0.2, 100), 1.0)


class TestFormatters(unittest.TestCase):
    def test_fmt_pct_scales_precision(self):
        self.assertEqual(psf.fmt_pct(0.034), "3.4%")
        self.assertEqual(psf.fmt_pct(0.50), "50%")
        self.assertEqual(psf.fmt_pct(0.12), "12%")
        self.assertEqual(psf.fmt_pct(1.5), "150%")        # >100% on many cores

    def test_fmt_pct_tiny_keeps_two_sig_figs(self):
        self.assertEqual(psf.fmt_pct(0.0002), "0.02%")    # the asked-for case
        self.assertEqual(psf.fmt_pct(0.000034), "0.0034%")

    def test_fmt_pct_never_scientific_notation(self):
        # a near-idle daemon over a long life -> extremely small average; must
        # stay plain decimal, not '1.7e-05%'.
        out = psf.fmt_pct(1.7e-07)
        self.assertNotIn("e", out)
        self.assertEqual(out, "0.000017%")

    def test_fmt_pct_zero_and_none(self):
        self.assertEqual(psf.fmt_pct(0.0), "0%")
        self.assertIsNone(psf.fmt_pct(None))

    def test_fmt_bytes(self):
        self.assertEqual(psf.fmt_bytes(0), "0")
        self.assertEqual(psf.fmt_bytes(512), "512B")
        self.assertEqual(psf.fmt_bytes(1536), "1.5K")
        self.assertEqual(psf.fmt_bytes(2 * 1024 * 1024), "2.0M")
        self.assertEqual(psf.fmt_bytes(1310720000), "1.2G")

    def test_fmt_duration(self):
        self.assertEqual(psf.fmt_duration(5), "5s")
        self.assertEqual(psf.fmt_duration(65), "1m5s")
        self.assertEqual(psf.fmt_duration(3725), "1h2m")
        self.assertEqual(psf.fmt_duration(90000), "1d1h")


class TestResourceDetail(unittest.TestCase):
    def test_detail_shows_cpu_rss_and_elapsed(self):
        # uptime 1100s, started @99999 ticks -> ~100s alive; 250 jiffies cpu
        # -> 2.5% lifetime average; rss 4096 pages * 4096 B = 16M.
        p = _p(7, 1, comm="claude", cmdline="claude",
               starttime=99999, utime=200, stime=50, rss_pages=4096)
        p.kept = True
        p.cpu_current = 0.034            # set by the sampler
        sysinfo = psf.SysInfo(clk_tck=100, page_size=4096, uptime=1100.0)
        lines = psf.render([p], suppressed=set(), width=50, color=False,
                           sysinfo=sysinfo)
        text = "\n".join(lines)
        self.assertIn("cpu 3.4% (2.5% avg)", text)   # current + lifetime avg
        self.assertIn("rss:16.0M", text)
        self.assertIn("up:1m40s", text)              # ~100s alive

    def test_detail_without_current_sample(self):
        p = _p(7, 1, comm="claude", cmdline="claude",
               starttime=99999, utime=200, stime=50, rss_pages=0)
        p.kept = True
        p.cpu_current = None             # sampling disabled / unreadable
        sysinfo = psf.SysInfo(clk_tck=100, page_size=4096, uptime=1100.0)
        text = "\n".join(psf.render([p], suppressed=set(), width=50,
                                    color=False, sysinfo=sysinfo))
        self.assertIn("cpu 2.5% avg", text)          # avg only, no current
        self.assertNotIn("rss:", text)               # rss 0 -> omitted


class TestGlossary(unittest.TestCase):
    def test_glossary_explains_est_and_stats(self):
        text = "\n".join(psf.glossary(color=False))
        self.assertIn("est", text)                   # explains "+N est"
        self.assertIn("established", text)
        self.assertIn("rss", text)
        self.assertIn("cpu", text)


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


if __name__ == "__main__":
    unittest.main()
