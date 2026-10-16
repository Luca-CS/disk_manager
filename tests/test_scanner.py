import os
import threading
import time
import unittest

from dm import config, scanner

from .common import SandboxCase, write

MB = 1024 * 1024
BIG = config.KEEP_FILE_MIN + 10  # above the keep threshold
SMALL = 1000                     # below


class ScannerCase(SandboxCase):
    """Tree:
    root/
      big1.bin      BIG
      small1.txt    SMALL
      a/            big_a.mp4 (2*BIG), s_a1.txt, s_a2.txt (SMALL each)
        deep/       big_d.zip (BIG), tiny.log (SMALL)
      empty/
      lnk -> ../outside (junction; outside holds 5 MB that must not be counted)
    """

    def setUp(self):
        super().setUp()
        self.root = self.path("root")
        self.outside = self.path("outside")
        write(os.path.join(self.root, "big1.bin"), BIG)
        write(os.path.join(self.root, "small1.txt"), SMALL)
        write(os.path.join(self.root, "a", "big_a.mp4"), 2 * BIG)
        write(os.path.join(self.root, "a", "s_a1.txt"), SMALL)
        write(os.path.join(self.root, "a", "s_a2.txt"), SMALL)
        write(os.path.join(self.root, "a", "deep", "big_d.zip"), BIG)
        write(os.path.join(self.root, "a", "deep", "tiny.log"), SMALL)
        os.makedirs(os.path.join(self.root, "empty"))
        write(os.path.join(self.outside, "huge.bin"), 5 * MB)
        self.has_junction = False
        from .common import mkjunction
        if mkjunction(os.path.join(self.root, "lnk"), self.outside):
            self.has_junction = True
        self.total_size = 4 * BIG + 4 * SMALL
        self.scan = self.run_scan()

    def run_scan(self, cancel=None):
        s = scanner.Scan(self.root)
        s.run(cancel=cancel, workers=4)
        return s


class AggregationTests(ScannerCase):
    def test_totals(self):
        r = self.scan.root
        self.assertEqual(r.size, self.total_size)
        self.assertEqual(r.file_count, 7)
        self.assertEqual(r.dir_count, 3)  # a, deep, empty
        self.assertFalse(self.scan.cancelled)
        s = self.scan.summary()
        self.assertEqual((s["size"], s["files"], s["dirs"]), (self.total_size, 7, 3))

    def test_junction_not_followed(self):
        if not self.has_junction:
            self.skipTest("cannot create junction")
        self.assertLess(self.scan.root.size, 5 * MB)
        self.assertEqual(self.scan.totals["links"], 1)
        self.assertIsNone(self.scan.find(os.path.join(self.root, "lnk")))
        names = [i["name"] for i in self.scan.children(self.root)["items"]]
        self.assertNotIn("lnk", names)

    def test_subfolder_sizes(self):
        a = self.scan.find(os.path.join(self.root, "a"))
        self.assertEqual(a.size, 3 * BIG + 3 * SMALL)
        self.assertEqual(a.file_count, 5)
        self.assertEqual(a.dir_count, 1)
        deep = self.scan.find(os.path.join(self.root, "a", "deep"))
        self.assertEqual(deep.size, BIG + SMALL)
        self.assertEqual((deep.small_count, deep.small_size), (1, SMALL))
        empty = self.scan.find(os.path.join(self.root, "empty"))
        self.assertEqual((empty.size, empty.file_count), (0, 0))

    def test_ext_and_types(self):
        t = self.scan.types()
        self.assertEqual(t["total"], self.total_size)
        exts = {e["ext"]: e for e in t["extensions"]}
        self.assertEqual(exts["txt"]["count"], 3)
        self.assertEqual(exts["txt"]["size"], 3 * SMALL)
        self.assertEqual(exts["mp4"]["size"], 2 * BIG)
        cats = {c["key"]: c for c in t["categories"]}
        self.assertEqual(sum(c["count"] for c in cats.values()), 7)
        self.assertEqual(sum(c["size"] for c in cats.values()), self.total_size)
        sizes = [c["size"] for c in t["categories"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_age_buckets_cover_all_files(self):
        age = self.scan.age()
        self.assertEqual(sum(b["count"] for b in age), 7)
        self.assertEqual(age[0]["count"], 7)  # all files are brand new


class LookupTests(ScannerCase):
    def test_children(self):
        out = self.scan.children(self.root)
        self.assertEqual(out["node"]["size"], self.total_size)
        self.assertIsNone(out["node"]["parent"])
        by_name = {i["name"]: i for i in out["items"]}
        self.assertEqual(by_name["a"]["type"], "dir")
        self.assertEqual(by_name["a"]["path"], os.path.join(self.root, "a"))
        self.assertTrue(by_name["empty"]["empty"])
        self.assertFalse(by_name["a"]["empty"])
        self.assertEqual(by_name["big1.bin"]["type"], "file")
        small = [i for i in out["items"] if i["type"] == "small"]
        self.assertEqual(len(small), 1)
        self.assertEqual((small[0]["count"], small[0]["size"]), (1, SMALL))
        sizes = [i["size"] for i in out["items"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        sub = self.scan.children(os.path.join(self.root, "a"))
        self.assertEqual(sub["node"]["parent"], self.root)
        self.assertIsNone(self.scan.children(os.path.join(self.root, "nope")))

    def test_tree_structure(self):
        t = self.scan.tree(self.root, depth=3, min_ratio=0)
        self.assertEqual(t["s"], self.total_size)
        self.assertEqual(t["t"], "d")
        self.assertEqual(t["fc"], 7)
        self.assertEqual(t["path"], self.root)

        def walk(n):
            yield n
            for c in n.get("ch", []):
                yield from walk(c)
        nodes = list(walk(t))
        for n in nodes:
            self.assertTrue({"n", "s", "t"} <= set(n))
        smalls = [n for n in nodes if n["t"] == "s"]
        # small-file aggregates: root (1 file), a (2), deep (1)
        self.assertEqual(sorted(n["s"] for n in smalls), sorted([SMALL, 2 * SMALL, SMALL]))
        self.assertEqual(sorted(n["k"] for n in smalls), [1, 1, 2])
        files = [n for n in nodes if n["t"] == "f"]
        self.assertEqual(sorted(n["n"] for n in files), ["big1.bin", "big_a.mp4", "big_d.zip"])
        for f in files:
            self.assertIn("cat", f)
        for n in nodes:
            if "ch" in n:
                self.assertEqual(sum(c["s"] for c in n["ch"]), n["s"])

    def test_tree_prunes_to_other(self):
        t = self.scan.tree(self.root, depth=3, min_ratio=0.5)
        others = [c for c in t["ch"] if c["t"] == "o"]
        self.assertEqual(len(others), 1)
        self.assertGreater(others[0]["k"], 0)

    def test_tree_depth_limit_marks_more(self):
        t = self.scan.tree(self.root, depth=1, min_ratio=0)
        a = next(c for c in t["ch"] if c["n"] == "a")
        self.assertNotIn("ch", a)
        self.assertEqual(a.get("more"), 1)
        self.assertIsNone(self.scan.tree(os.path.join(self.root, "zzz")))

    def test_largest(self):
        out = self.scan.largest(limit=10)
        self.assertEqual([f["name"] for f in out][0], "big_a.mp4")
        self.assertEqual(len(out), 3)
        sizes = [f["size"] for f in out]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(out[0]["path"], os.path.join(self.root, "a", "big_a.mp4"))
        self.assertEqual(len(self.scan.largest(limit=2)), 2)
        under = self.scan.largest(under=os.path.join(self.root, "a", "deep"))
        self.assertEqual([f["name"] for f in under], ["big_d.zip"])
        self.assertEqual(self.scan.largest(under=os.path.join(self.root, "zzz")), [])
        self.assertEqual(self.scan.largest(min_age_days=30), [])  # nothing is 30 days old
        for cat in ("video", "archives"):
            r = self.scan.largest(category=cat)
            for f in r:
                self.assertEqual(f["category"], cat)

    def test_find_case_insensitive(self):
        n = self.scan.find(self.root.upper() + "\\A\\DEEP")
        self.assertIsNotNone(n)
        self.assertEqual(n.name, "deep")
        self.assertIs(self.scan.find(self.root + "\\"), self.scan.root)
        self.assertIsNone(self.scan.find(self.path("outside")))
        self.assertIsNone(self.scan.find(self.root + "2"))
        self.assertEqual(n.path(), os.path.join(self.root, "a", "deep"))

    def test_contains(self):
        self.assertTrue(self.scan.contains(self.root))
        self.assertTrue(self.scan.contains(os.path.join(self.root, "a", "x.txt")))
        self.assertTrue(self.scan.contains(self.root.upper()))
        self.assertFalse(self.scan.contains(self.root + "2"))
        self.assertFalse(self.scan.contains(self.outside))


class MutationTests(ScannerCase):
    def test_remove_path_file_updates_ancestors(self):
        self.scan.remove_path(os.path.join(self.root, "a", "deep", "big_d.zip"))
        deep = self.scan.find(os.path.join(self.root, "a", "deep"))
        a = self.scan.find(os.path.join(self.root, "a"))
        self.assertEqual((deep.size, deep.file_count), (SMALL, 1))
        self.assertEqual((a.size, a.file_count), (2 * BIG + 3 * SMALL, 4))
        self.assertEqual((self.scan.root.size, self.scan.root.file_count), (3 * BIG + 4 * SMALL, 6))

    def test_remove_path_folder(self):
        self.scan.remove_path(os.path.join(self.root, "a"))
        self.assertIsNone(self.scan.find(os.path.join(self.root, "a")))
        r = self.scan.root
        self.assertEqual((r.size, r.file_count, r.dir_count), (BIG + SMALL, 2, 1))

    def test_remove_small_file_with_size_hint(self):
        self.scan.remove_path(os.path.join(self.root, "small1.txt"), size_hint=SMALL)
        r = self.scan.root
        self.assertEqual((r.size, r.file_count), (self.total_size - SMALL, 6))
        self.assertEqual(r.small_count, 0)

    def test_remove_unknown_is_noop(self):
        self.scan.remove_path(os.path.join(self.root, "a", "ghost.bin"))
        self.scan.remove_path(os.path.join(self.outside, "huge.bin"))
        self.assertEqual(self.scan.root.size, self.total_size)

    def test_refresh_after_deleting_file(self):
        os.remove(os.path.join(self.root, "a", "deep", "big_d.zip"))
        os.remove(os.path.join(self.root, "a", "s_a1.txt"))
        write(os.path.join(self.root, "a", "new.bin"), 3 * BIG)
        self.assertTrue(self.scan.refresh(os.path.join(self.root, "a")))
        a = self.scan.find(os.path.join(self.root, "a"))
        self.assertEqual(a.size, 2 * BIG + 3 * BIG + SMALL + SMALL)  # big_a, new, s_a2, tiny
        self.assertEqual(a.file_count, 4)
        r = self.scan.root
        self.assertEqual(r.size, BIG + SMALL + a.size)
        self.assertEqual(r.file_count, 2 + a.file_count)
        # fresh scan agrees
        fresh = self.run_scan()
        self.assertEqual((fresh.root.size, fresh.root.file_count, fresh.root.dir_count),
                         (r.size, r.file_count, r.dir_count))
        self.assertIs(self.scan.find(os.path.join(self.root, "a", "deep")).parent, a)

    def test_refresh_missing_folder_removes_it(self):
        import shutil
        shutil.rmtree(os.path.join(self.root, "a"))
        self.assertTrue(self.scan.refresh(os.path.join(self.root, "a")))
        self.assertIsNone(self.scan.find(os.path.join(self.root, "a")))
        self.assertEqual(self.scan.root.file_count, 2)
        self.assertFalse(self.scan.refresh(os.path.join(self.root, "unknown")))


class PersistenceTests(ScannerCase):
    def assert_same(self, a, b):
        self.assertEqual((a.root.size, a.root.file_count, a.root.dir_count),
                         (b.root.size, b.root.file_count, b.root.dir_count))
        na = {n.path(): (n.size, n.file_count, n.dir_count, n.small_size, n.small_count, n.files)
              for n in a.iter_nodes()}
        nb = {n.path(): (n.size, n.file_count, n.dir_count, n.small_size, n.small_count, n.files)
              for n in b.iter_nodes()}
        self.assertEqual(na, nb)

    def test_save_load_roundtrip(self):
        path = self.scan.save()
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(path.startswith(str(config.SCANS_DIR)))
        loaded = scanner.Scan.load(self.root)
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.from_cache)
        self.assert_same(self.scan, loaded)
        self.assertEqual(loaded.ext_stats, self.scan.ext_stats)
        self.assertEqual(loaded.totals, self.scan.totals)
        self.assertEqual([f["path"] for f in loaded.largest()], [f["path"] for f in self.scan.largest()])

    def test_load_missing_or_corrupt(self):
        self.assertIsNone(scanner.Scan.load(self.path("nothing")))
        p = scanner.cache_path(self.root)
        with open(p, "wb") as f:
            f.write(b"not gzip")
        self.assertIsNone(scanner.Scan.load(self.root))

    def test_roundtrip_with_lone_surrogate_filename(self):
        name = "sur_\udc80_rogate.bin"
        d = self.path("surr")
        os.makedirs(d)
        try:
            write(os.path.join(d, name), BIG)
        except (OSError, UnicodeError):
            self.skipTest("filesystem refuses lone surrogate names")
        s = scanner.Scan(d)
        s.run(workers=2)
        names = [f["name"] for f in s.largest()]
        if name not in names:
            self.skipTest("filesystem normalizes lone surrogate names")
        s.save()
        loaded = scanner.Scan.load(d)
        self.assertIsNotNone(loaded)
        self.assertEqual([f["name"] for f in loaded.largest()], names)


class CancelTests(ScannerCase):
    def test_pre_set_cancel_returns_quickly(self):
        ev = threading.Event()
        ev.set()
        t0 = time.monotonic()
        s = self.run_scan(cancel=ev)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(s.cancelled)
        self.assertTrue(s.summary()["cancelled"])
        self.assertEqual(s.root.file_count, 0)

    def test_uncancelled_event_is_fine(self):
        s = self.run_scan(cancel=threading.Event())
        self.assertFalse(s.cancelled)
        self.assertEqual(s.root.size, self.total_size)


if __name__ == "__main__":
    unittest.main()
