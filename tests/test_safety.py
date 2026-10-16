import ctypes
import os
import re
import stat
import threading
import unittest
from unittest import mock

from dm import config, safety, winapi

from .common import FakeRecycle, SandboxCase, set_age, write


class CanonTests(unittest.TestCase):
    def test_rejects_bad_input(self):
        bad = [
            "", None, 42, "relative\\path", "C:relative", "\\\\server\\share\\x", "\\\\?\\C:\\Windows",
            "\\\\.\\PhysicalDrive0", "C:\\a\\*.txt", "C:\\a\\b?", 'C:\\a"b', "C:\\a<b", "C:\\a|b",
            "C:\\x:stream", "C:\\x.txt:Zone.Identifier", "C:\\Windows.\\System32", "C:\\foo \\bar",
            "C:\\a\\b.", "C:\\a\\..\\Windows.", "C:\\a\x00b", "C:\\" + "a" * 40_000,
        ]
        for p in bad:
            with self.subTest(path=p if not isinstance(p, str) else p[:40]):
                with self.assertRaises(safety.Refused):
                    safety.canon(p)

    def test_normalizes(self):
        self.assertEqual(safety.canon("C:\\a\\b\\..\\c"), "C:\\a\\c")
        self.assertEqual(safety.canon("C:/a//b/./c"), "C:\\a\\b\\c")
        self.assertEqual(safety.canon("C:\\a\\.."), "C:\\")
        self.assertEqual(safety.canon("C:\\..\\.."), "C:\\")
        self.assertEqual(safety.canon("c:\\Users\\X"), "c:\\Users\\X")


class CheckRecyclableTests(SandboxCase):
    def refused(self, path, **kw):
        with self.assertRaises(safety.Refused, msg=path):
            safety.check_recyclable(path, **kw)

    def test_accepts_normal_sandbox_file(self):
        f = write(self.path("ok", "file.txt"), 10)
        self.assertEqual(safety.check_recyclable(f), f)
        self.assertEqual(safety.check_recyclable(f, scope_root=self.sb), f)
        self.assertEqual(safety.check_recyclable(os.path.dirname(f)), os.path.dirname(f))

    def test_refuses_drive_root(self):
        self.refused("C:\\")
        self.refused(config.SYSTEM_DRIVE)
        self.refused("C:")

    def test_refuses_system_locations(self):
        self.refused(os.path.join(config.SYSTEM_ROOT))
        self.refused(os.path.join(config.SYSTEM_ROOT, "System32"))
        self.refused(os.path.join(config.SYSTEM_ROOT, "System32", "notepad.exe"))
        self.refused(config.PROGRAM_FILES)
        self.refused(os.path.join(config.PROGRAM_FILES, "anything"))
        self.refused(config.PROGRAM_FILES_X86)
        self.refused(config.PROGRAMDATA)
        self.refused(os.path.join(config.PROGRAMDATA, "Microsoft"))

    def test_refuses_protected_root_files(self):
        for n in ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "PAGEFILE.SYS"):
            self.refused(os.path.join(config.SYSTEM_DRIVE, n))

    def test_refuses_profile_and_personal_roots(self):
        self.refused(config.USERPROFILE)
        self.refused(os.path.join(config.USERPROFILE, "Desktop"))
        self.refused(os.path.join(config.USERPROFILE, "Documents"))
        self.refused(os.path.join(config.USERPROFILE, "Downloads"))
        self.refused(os.path.join(config.USERPROFILE, "AppData"))
        self.refused(config.LOCALAPPDATA)
        self.refused(config.APPDATA)
        self.refused(config.TEMP)

    def test_refuses_profile_hives(self):
        for n in ("NTUSER.DAT", "ntuser.dat.LOG1", "ntuser.ini", "UsrClass.dat"):
            self.refused(os.path.join(config.USERPROFILE, n))

    def test_refuses_packages(self):
        self.refused(os.path.join(config.LOCALAPPDATA, "Packages"))
        self.refused(os.path.join(config.LOCALAPPDATA, "Packages", "Some.Package_x"))

    def test_refuses_app_own_folder(self):
        self.refused(str(config.ROOT_DIR))
        self.refused(os.path.join(str(config.ROOT_DIR), "dm", "safety.py"))
        self.refused(os.path.join(str(config.ROOT_DIR), "data"))

    def test_refuses_8dot3_short_path(self):
        buf = ctypes.create_unicode_buffer(520)
        n = ctypes.windll.kernel32.GetShortPathNameW(config.PROGRAM_FILES, buf, 520)
        if not n or buf.value.lower() == config.PROGRAM_FILES.lower():
            self.skipTest("8.3 short names unavailable")
        self.refused(buf.value)

    def test_refuses_path_through_junction(self):
        real = self.path("real")
        write(os.path.join(real, "inner.txt"), 5)
        link = self.path("link")
        self.junction(link, real)
        self.refused(link)
        self.refused(os.path.join(link, "inner.txt"))
        # the real path is fine
        safety.check_recyclable(os.path.join(real, "inner.txt"))

    def test_refuses_outside_scope_root(self):
        a = write(self.path("a", "f.txt"), 1)
        b = write(self.path("b", "f.txt"), 1)
        self.assertEqual(safety.check_recyclable(a, scope_root=self.path("a")), a)
        self.refused(b, scope_root=self.path("a"))
        # prefix lookalike must not count as inside
        c = write(self.path("a2", "f.txt"), 1)
        self.refused(c, scope_root=self.path("a"))

    def test_refuses_missing_and_bad_paths(self):
        self.refused(self.path("nope.txt"))
        self.refused("relative.txt")
        self.refused("\\\\server\\share\\x")

    def test_refuses_system_attribute(self):
        f = write(self.path("sys.bin"), 1)
        os.system(f'attrib +S "{f}" >nul')
        try:
            if not os.lstat(f).st_file_attributes & winapi.FILE_ATTRIBUTE_SYSTEM:
                self.skipTest("cannot set system attribute")
            self.refused(f)
        finally:
            os.system(f'attrib -S "{f}" >nul')


class CheckPurgeableTests(SandboxCase):
    def refused(self, path):
        with self.assertRaises(safety.Refused, msg=path):
            safety.check_purgeable(path)

    def test_refuses_windows_and_drive_roots(self):
        self.refused(config.SYSTEM_ROOT)
        self.refused(os.path.join(config.SYSTEM_ROOT, "System32"))
        self.refused("C:\\")
        self.refused(config.PROGRAM_FILES)
        self.refused(config.PROGRAMDATA)

    def test_refuses_user_roots(self):
        self.refused(config.LOCALAPPDATA)
        self.refused(config.APPDATA)
        self.refused(config.USERPROFILE)
        self.refused(os.path.join(config.USERPROFILE, "AppData"))

    def test_refuses_personal_folders_and_content(self):
        for d in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
            self.refused(os.path.join(config.USERPROFILE, d))
            self.refused(os.path.join(config.USERPROFILE, d, "sub", "x.txt"))

    def test_personal_folder_content_refused_but_node_modules_allowed(self):
        personal = self.path("personal")
        os.makedirs(os.path.join(personal, "proj", "node_modules"))
        os.makedirs(os.path.join(personal, "proj", "src"))
        write(os.path.join(personal, "proj", "src", "a.js"), 1)
        write(os.path.join(personal, "proj", "node_modules", "m.js"), 1)
        with mock.patch.object(safety, "PERSONAL_DIRS", [personal]):
            self.refused(os.path.join(personal, "proj", "src"))
            self.refused(os.path.join(personal, "proj"))
            self.refused(personal)
            nm = os.path.join(personal, "proj", "node_modules")
            self.assertEqual(safety.check_purgeable(nm), nm)
            # a *file* named node_modules is not a regenerable folder
            write(os.path.join(personal, "node_modules"), 1)
            self.refused(os.path.join(personal, "node_modules"))

    def test_refuses_outside_allowed_roots(self):
        self.refused(os.path.join(config.SYSTEM_DRIVE, "SomeFolder"))
        self.refused(str(config.ROOT_DIR))
        self.refused(os.path.join(str(config.ROOT_DIR), "data"))

    def test_refuses_junction(self):
        real = self.path("real")
        write(os.path.join(real, "f.bin"), 3)
        link = self.path("link")
        self.junction(link, real)
        self.refused(link)
        self.refused(os.path.join(link, "f.bin"))

    def test_accepts_sandbox_and_temp(self):
        sub = self.path("cache")
        os.makedirs(sub)
        self.assertEqual(safety.check_purgeable(sub), sub)
        self.assertEqual(safety.check_purgeable(config.TEMP), config.TEMP)


class PurgeTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.cache = self.path("cache")
        os.makedirs(self.cache)

    def test_deletes_files_keeps_root_removes_empty_subfolders(self):
        write(os.path.join(self.cache, "a.bin"), 100)
        write(os.path.join(self.cache, "s1", "b.bin"), 200)
        write(os.path.join(self.cache, "s1", "s2", "c.bin"), 300)
        os.makedirs(os.path.join(self.cache, "empty"))
        res = safety.purge(self.cache)
        self.assertEqual((res.bytes, res.files), (600, 3))
        self.assertTrue(os.path.isdir(self.cache))
        self.assertEqual(os.listdir(self.cache), [])
        self.assertEqual(res.dirs, 3)  # s1, s2, empty
        self.assertEqual(res.errors, [])

    def test_keep_root_false_removes_root(self):
        write(os.path.join(self.cache, "a.bin"), 10)
        safety.purge(self.cache, keep_root=False)
        self.assertFalse(os.path.exists(self.cache))

    def test_junction_removed_as_link_target_intact(self):
        other = self.path("other")
        keep = write(os.path.join(other, "precious.txt"), 50)
        write(os.path.join(other, "deep", "x.txt"), 7)
        write(os.path.join(self.cache, "a.bin"), 10)
        self.junction(os.path.join(self.cache, "lnk"), other)
        res = safety.purge(self.cache)
        self.assertEqual((res.bytes, res.files), (10, 1))
        self.assertEqual(os.listdir(self.cache), [])
        self.assertTrue(os.path.isfile(keep))
        self.assertTrue(os.path.isfile(os.path.join(other, "deep", "x.txt")))

    def test_min_age_keeps_recent_files(self):
        old = write(os.path.join(self.cache, "old.bin"), 100)
        new = write(os.path.join(self.cache, "new.bin"), 40)
        mid = write(os.path.join(self.cache, "sub", "old2.bin"), 60)
        set_age(old, 3 * 86400)
        set_age(mid, 2 * 86400)
        res = safety.purge(self.cache, min_age_s=86400)
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(mid))
        self.assertTrue(os.path.exists(new))
        self.assertEqual((res.bytes, res.files, res.skipped_recent), (160, 2, 1))

    def test_name_glob_only_matching(self):
        write(os.path.join(self.cache, "thumbcache_32.db"), 10)
        write(os.path.join(self.cache, "iconcache_1.db"), 20)
        write(os.path.join(self.cache, "keep.txt"), 30)
        glob = re.compile(r"^(thumbcache|iconcache)_.*\.db$", re.IGNORECASE)
        res = safety.purge(self.cache, name_glob=glob)
        self.assertEqual(sorted(os.listdir(self.cache)), ["keep.txt"])
        self.assertEqual((res.bytes, res.files), (30, 2))

    def test_read_only_files_deleted(self):
        f = write(os.path.join(self.cache, "ro.bin"), 15)
        os.chmod(f, stat.S_IREAD)
        res = safety.purge(self.cache)
        self.assertFalse(os.path.exists(f))
        self.assertEqual((res.bytes, res.files), (15, 1))

    def test_dry_run_deletes_nothing_but_counts(self):
        write(os.path.join(self.cache, "a.bin"), 100)
        write(os.path.join(self.cache, "s", "b.bin"), 200)
        with mock.patch.object(safety, "DRY_RUN", True):
            res = safety.purge(self.cache)
            self.assertTrue(res.as_dict()["dry_run"])
        self.assertEqual((res.bytes, res.files), (300, 2))
        self.assertTrue(os.path.isfile(os.path.join(self.cache, "a.bin")))
        self.assertTrue(os.path.isfile(os.path.join(self.cache, "s", "b.bin")))

    def test_single_file_target(self):
        f = write(self.path("single.bin"), 77)
        res = safety.purge(f)
        self.assertFalse(os.path.exists(f))
        self.assertEqual((res.bytes, res.files), (77, 1))

    def test_refused_target_raises_and_deletes_nothing(self):
        with self.assertRaises(safety.Refused):
            safety.purge(config.SYSTEM_ROOT)

    def test_cancel_event(self):
        write(os.path.join(self.cache, "a.bin"), 5)
        ev = threading.Event()
        ev.set()
        res = safety.purge(self.cache, cancel=ev)
        self.assertEqual(res.files, 0)
        self.assertTrue(os.path.exists(os.path.join(self.cache, "a.bin")))

    def test_purge_many_journals_and_reports_refused(self):
        write(os.path.join(self.cache, "a.bin"), 12)
        res = safety.purge_many([{"path": self.cache}, {"path": config.SYSTEM_ROOT},
                                 {"path": self.path("missing")}], rule_id="t")
        self.assertEqual((res.bytes, res.files), (12, 1))
        self.assertEqual(len(res.refused), 2)  # system root + missing path
        self.assertTrue(any(r["path"] == config.SYSTEM_ROOT for r in res.refused))
        lines = self.journal_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn('"purge"', lines[0])


class MeasureTests(SandboxCase):
    def test_measure_equals_purge(self):
        cache = self.path("cache")
        write(os.path.join(cache, "a.bin"), 100)
        old = write(os.path.join(cache, "s", "b.bin"), 200)
        set_age(old, 5 * 86400)
        write(os.path.join(cache, "s", "t", "c.bin"), 300)
        other = self.path("other")
        write(os.path.join(other, "z.bin"), 999)
        self.junction(os.path.join(cache, "lnk"), other)
        for kwargs in ({}, {"min_age_s": 86400}):
            with self.subTest(kwargs=kwargs):
                m = safety.measure(cache, **kwargs)
                with mock.patch.object(safety, "DRY_RUN", True):
                    dry = safety.purge(cache, **kwargs)
                self.assertEqual(m, (dry.bytes, dry.files))
        m_all = safety.measure(cache)
        self.assertEqual(m_all, (600, 3))
        res = safety.purge(cache)
        self.assertEqual((res.bytes, res.files), m_all)
        self.assertTrue(os.path.isfile(os.path.join(other, "z.bin")))

    def test_measure_with_glob_and_refused(self):
        cache = self.path("cache")
        write(os.path.join(cache, "x.db"), 10)
        write(os.path.join(cache, "y.txt"), 20)
        self.assertEqual(safety.measure(cache, name_glob=re.compile(r".*\.db$")), (10, 1))
        self.assertEqual(safety.measure(config.SYSTEM_ROOT), (0, 0))


class RecycleTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeRecycle(self.sb)
        p = mock.patch.object(winapi, "send_to_recycle_bin", self.fake)
        p.start()
        self.addCleanup(p.stop)

    def test_valid_and_refused_reported(self):
        f = write(self.path("sel", "a.txt"), 123)
        res = safety.recycle([f, config.SYSTEM_ROOT, "C:\\", self.path("sel", "ghost.txt"), "relative"],
                             scope_root=self.sb, source="explorer")
        self.assertEqual(res.done, [f])
        self.assertEqual((res.bytes, res.files), (123, 1))
        self.assertFalse(os.path.exists(f))
        self.assertEqual(len(res.refused), 4)
        self.assertTrue(all(r["reason"] for r in res.refused))
        refused_paths = {r["path"] for r in res.refused}
        self.assertIn(config.SYSTEM_ROOT, refused_paths)
        self.assertEqual(res.errors, [])

    def test_nested_selection_collapsed(self):
        d = self.path("folder")
        f = write(os.path.join(d, "inner", "a.txt"), 10)
        g = write(os.path.join(d, "b.txt"), 20)
        res = safety.recycle([f, d, g, d], scope_root=self.sb, source="explorer")
        self.assertEqual(self.fake.calls, [[d]])
        self.assertEqual(res.done, [d])
        self.assertEqual((res.bytes, res.files), (30, 1))

    def test_out_of_scope_refused(self):
        f = write(self.path("x", "a.txt"), 1)
        res = safety.recycle([f], scope_root=self.path("y"), source="t")
        self.assertEqual(res.done, [])
        self.assertEqual(len(res.refused), 1)
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(os.path.exists(f))

    def test_journal_entry_appended(self):
        f = write(self.path("a.txt"), 5)
        safety.recycle([f], scope_root=self.sb, source="largest")
        lines = self.journal_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn('"recycle"', lines[0])
        self.assertIn('"largest"', lines[0])

    def test_dry_run_does_not_call_bin(self):
        f = write(self.path("a.txt"), 5)
        with mock.patch.object(safety, "DRY_RUN", True):
            res = safety.recycle([f], scope_root=self.sb, source="t")
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(os.path.exists(f))
        self.assertEqual(res.done, [f])

    def test_not_deleted_reported_as_error(self):
        f = write(self.path("a.txt"), 5)
        with mock.patch.object(winapi, "send_to_recycle_bin", lambda paths: (1, True)):
            res = safety.recycle([f], scope_root=self.sb, source="t")
        self.assertEqual(res.done, [])
        self.assertEqual(len(res.errors), 1)

    def test_junction_inside_folder_not_followed_in_size(self):
        d = self.path("d")
        write(os.path.join(d, "a.txt"), 10)
        other = self.path("other")
        write(os.path.join(other, "big.bin"), 5000)
        self.junction(os.path.join(d, "lnk"), other)
        res = safety.recycle([d], scope_root=self.sb, source="t")
        self.assertEqual(res.bytes, 10)

    def test_empty_recycle_bin_patched(self):
        with mock.patch.object(winapi, "empty_recycle_bin", return_value=True) as e, \
                mock.patch.object(winapi, "recycle_bin_info", return_value={"size": 9, "count": 2, "ok": True}):
            res = safety.empty_recycle_bin()
        e.assert_called_once()
        self.assertEqual((res.bytes, res.files), (9, 2))


if __name__ == "__main__":
    unittest.main()
