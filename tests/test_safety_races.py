"""Regression tests for the security review findings (TOCTOU, containment, locks)."""

import os
import unittest
from unittest import mock

from dm import config, safety, winapi

from .common import FakeRecycle, SandboxCase, write


class HandleDeletionTests(SandboxCase):
    def test_swapped_parent_cannot_redirect_deletion(self):
        """A folder replaced by a junction after validation must not lead outside the target."""
        cache = self.path("cache")
        sub = os.path.join(cache, "sub")
        victim = self.path("victim")
        write(os.path.join(sub, "f.bin"), 10)
        keep = write(os.path.join(victim, "f.bin"), 10)
        # Simulate the race: right after the walk lists `sub`, it becomes a junction to `victim`.
        real_scandir = os.scandir

        def racing_scandir(path):
            entries = list(real_scandir(path))
            if os.path.normcase(path) == os.path.normcase(sub):
                # `sub` passed the lstat re-check and was listed: now swap it.
                os.rename(sub, sub + "_old")
                self.junction(sub, victim)
            return iter(entries)

        with mock.patch("dm.safety.os.scandir", side_effect=racing_scandir):
            res = safety.purge(cache)
        self.assertTrue(os.path.exists(keep), "file outside the purge target was deleted")
        self.assertEqual(res.files, 0)
        self.assertTrue(res.refused, "the redirected file should be reported as refused")

    def test_file_deleted_through_handle(self):
        cache = self.path("cache2")
        f = write(os.path.join(cache, "a.bin"), 100)
        os.chmod(f, 0o444)  # read-only must not block the deletion
        res = safety.purge(cache)
        self.assertFalse(os.path.exists(f))
        self.assertEqual((res.files, res.bytes), (1, 100))

    def test_delete_via_handle_refuses_outside_root(self):
        inside = self.path("root")
        outside = write(self.path("elsewhere", "x.bin"), 1)
        os.makedirs(inside, exist_ok=True)
        res = safety.OpResult()
        self.assertFalse(safety._delete_via_handle(outside, inside, res))
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(len(res.refused), 1)


class ContainmentTests(SandboxCase):
    def test_parent_of_protected_location_refused(self):
        for p in (os.path.join(config.APPDATA, "Microsoft"), os.path.join(config.LOCALAPPDATA, "Microsoft")):
            with self.assertRaises(safety.Refused):
                safety.check_recyclable(p)
            with self.assertRaises(safety.Refused):
                safety.check_purgeable(p)

    def test_folder_holding_the_interpreter_refused(self):
        import sys
        parent = os.path.dirname(sys.prefix)
        if safety.is_within(parent, config.LOCALAPPDATA) and safety.key(parent) != safety.key(config.LOCALAPPDATA):
            with self.assertRaises(safety.Refused):
                safety.check_recyclable(parent)

    def test_profile_hive_refused_for_purge(self):
        with self.assertRaises(safety.Refused):
            safety.check_purgeable(os.path.join(config.USERPROFILE, "NTUSER.DAT"))

    def test_purge_whitelist_excludes_profile_root_content(self):
        with self.assertRaises(safety.Refused):
            safety.check_purgeable(os.path.join(config.USERPROFILE, ".ssh"))

    def test_every_profile_root_is_exact_protected(self):
        users = os.path.join(config.SYSTEM_DRIVE, "Users")
        for e in os.scandir(users):
            if e.is_dir(follow_symlinks=False):
                self.assertTrue(safety._exact_any(e.path, safety.PROTECTED_EXACT), e.path)


class AncestorLockTests(SandboxCase):
    def test_locked_ancestor_cannot_be_renamed(self):
        f = write(self.path("a", "b", "c.txt"), 1)
        held = {}
        try:
            failed = safety._lock_ancestors([f], held)
            self.assertEqual(failed, set())
            with self.assertRaises(OSError):
                os.rename(self.path("a", "b"), self.path("a", "b2"))
        finally:
            for h in held.values():
                h.close()
        os.rename(self.path("a", "b"), self.path("a", "b2"))  # free again once released

    def test_recycle_refuses_when_parent_is_a_junction(self):
        real = self.path("real")
        write(os.path.join(real, "x.txt"), 1)
        self.junction(self.path("link"), real)
        target = os.path.join(self.path("link"), "x.txt")
        with mock.patch.object(winapi, "send_to_recycle_bin", FakeRecycle(self.sb)):
            res = safety.recycle([target])
        self.assertEqual(res.files, 0)
        self.assertTrue(os.path.exists(os.path.join(real, "x.txt")))


if __name__ == "__main__":
    unittest.main()
