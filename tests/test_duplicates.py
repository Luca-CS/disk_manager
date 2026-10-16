import os
import threading

from dm import config, duplicates, scanner

from .common import SandboxCase, write

SIZE = 1024 * 1024 + 4096  # > KEEP_FILE_MIN and > 2 * partial-hash window
CONTENT = bytes((i * 7 + 3) % 251 for i in range(SIZE))


def variant(offset: int) -> bytes:
    b = bytearray(CONTENT)
    b[offset] ^= 0xFF
    return bytes(b)


class DuplicateTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.root = self.path("dupes")
        self.a1 = write(os.path.join(self.root, "a1.bin"), data=CONTENT)
        self.a2 = write(os.path.join(self.root, "sub", "a2.bin"), data=CONTENT)
        self.a3 = write(os.path.join(self.root, "sub", "deeper", "a3.dat"), data=CONTENT)
        # same size, different content: first byte / middle byte (invisible to the partial hash)
        write(os.path.join(self.root, "d_head.bin"), data=variant(0))
        write(os.path.join(self.root, "d_mid.bin"), data=variant(SIZE // 2))
        self.link = os.path.join(self.root, "hardlink_of_a1.bin")
        try:
            os.link(self.a1, self.link)
        except OSError:
            self.skipTest("hard links unavailable")
        # identical small files are below the keep threshold and never candidates
        write(os.path.join(self.root, "s1.txt"), 5000)
        write(os.path.join(self.root, "s2.txt"), 5000)
        self.scan = scanner.Scan(self.root)
        self.scan.run(workers=4)

    def test_one_group_hardlink_collapsed(self):
        res = duplicates.find_duplicates(self.scan, min_size=config.KEEP_FILE_MIN, include_system=True)
        self.assertFalse(res["cancelled"])
        self.assertEqual(len(res["groups"]), 1)
        g = res["groups"][0]
        paths = {os.path.normcase(f["path"]) for f in g["files"]}
        self.assertEqual(g["count"], 3)
        self.assertEqual(len(paths), 3)
        self.assertIn(os.path.normcase(self.a2), paths)
        self.assertIn(os.path.normcase(self.a3), paths)
        # exactly one of the two hard-linked names
        self.assertEqual(len(paths & {os.path.normcase(self.a1), os.path.normcase(self.link)}), 1)
        self.assertEqual(g["size"], SIZE)
        self.assertEqual(g["wasted"], SIZE * 2)
        self.assertEqual(res["total_wasted"], SIZE * 2)
        self.assertTrue(res["include_system"])
        for f in g["files"]:
            self.assertEqual(f["name"], os.path.basename(f["path"]))
            self.assertIsInstance(f["mtime"], int)
        # d_mid shares the partial hash (head/tail identical) so it is fully hashed too; d_head is not
        self.assertEqual(res["files_hashed"], 4)
        self.assertEqual(res["bytes_hashed"], 4 * SIZE)

    def test_include_system_false_excludes_localappdata(self):
        # the sandbox lives under %LOCALAPPDATA%\Temp, which is excluded by default
        res = duplicates.find_duplicates(self.scan, min_size=config.KEEP_FILE_MIN, include_system=False)
        self.assertEqual(res["groups"], [])
        self.assertEqual(res["total_wasted"], 0)
        self.assertFalse(res["include_system"])

    def test_cancelled(self):
        ev = threading.Event()
        ev.set()
        res = duplicates.find_duplicates(self.scan, min_size=config.KEEP_FILE_MIN, include_system=True, cancel=ev)
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["groups"], [])

    def test_min_size_above_file_size_finds_nothing(self):
        res = duplicates.find_duplicates(self.scan, min_size=SIZE + 1, include_system=True)
        self.assertEqual(res["groups"], [])

    def test_progress_dict_updated(self):
        prog = {}
        duplicates.find_duplicates(self.scan, min_size=config.KEEP_FILE_MIN, include_system=True, progress=prog)
        self.assertEqual(prog["phase"], "full")
        self.assertEqual(prog["groups"], 1)
        self.assertEqual(prog["done_files"], prog["total_files"])

    def test_deleted_file_is_skipped(self):
        os.remove(self.a3)
        res = duplicates.find_duplicates(self.scan, min_size=config.KEEP_FILE_MIN, include_system=True)
        self.assertEqual(len(res["groups"]), 1)
        self.assertEqual(res["groups"][0]["count"], 2)
        self.assertEqual(res["total_wasted"], SIZE)

    def test_junction_target_not_double_counted(self):
        other = self.path("other")
        write(os.path.join(other, "copy.bin"), data=CONTENT)
        self.junction(os.path.join(self.root, "lnk"), other)
        scan = scanner.Scan(self.root)
        scan.run(workers=4)
        res = duplicates.find_duplicates(scan, min_size=config.KEEP_FILE_MIN, include_system=True)
        self.assertEqual(res["groups"][0]["count"], 3)


class NoDuplicatesTests(SandboxCase):
    def test_all_unique(self):
        root = self.path("u")
        write(os.path.join(root, "a.bin"), data=CONTENT)
        write(os.path.join(root, "b.bin"), data=variant(10))
        write(os.path.join(root, "c.bin"), data=variant(SIZE - 1))
        scan = scanner.Scan(root)
        scan.run(workers=2)
        res = duplicates.find_duplicates(scan, min_size=0, include_system=True)
        self.assertEqual(res["groups"], [])
        self.assertEqual(res["total_wasted"], 0)
