"""Regression tests for the security review findings (TOCTOU, containment, locks,
cache allow-list for permanent deletion, profile roots from the registry)."""

import os
import unittest
from unittest import mock

from dm import cleaners, config, safety, winapi

from .common import FakeRecycle, SandboxCase, write

LOCAL, ROAMING, HOME = config.LOCALAPPDATA, config.APPDATA, config.USERPROFILE


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
                self.assertTrue(safety._exact_any(e.path, safety._protected_exact()), e.path)


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


def _location_ok(path: str) -> bool:
    p = safety.canon(path)
    return safety._within_any(p, safety.PURGE_SYSTEM_ROOTS) or safety.is_cache_location(p)


class PurgeAllowListTests(SandboxCase):
    """Permanent deletion only inside folders that are caches by construction."""

    NOT_CACHES = [
        os.path.join(LOCAL, "Programs"),
        os.path.join(LOCAL, "Programs", "Microsoft VS Code"),
        os.path.join(LOCAL, "Programs", "SomeApp", "resources", "Cache"),  # cache name, but inside an install
        os.path.join(LOCAL, "Microsoft", "Edge"),
        os.path.join(LOCAL, "Microsoft", "Edge", "User Data"),
        os.path.join(LOCAL, "Microsoft", "Edge", "User Data", "Default"),
        os.path.join(LOCAL, "Microsoft", "Edge", "User Data", "Default", "Login Data"),
        os.path.join(LOCAL, "Google", "Chrome", "User Data", "Default", "Network", "Cookies"),
        os.path.join(LOCAL, "Package Cache"),  # installer bundles needed to repair / uninstall
        os.path.join(LOCAL, "npm-cache"),
        os.path.join(LOCAL, "Discord"),
        os.path.join(LOCAL, "dm_test_no_such_app", "app-1.0.0"),  # app-* without Update.exe
        os.path.join(ROAMING, "Code"),
        os.path.join(ROAMING, "Code", "User", "settings.json"),
        os.path.join(ROAMING, "Mozilla", "Firefox", "Profiles", "x.default", "logins.json"),
        os.path.join(HOME, "anaconda3"),
        os.path.join(HOME, "anaconda3", "envs", "py311"),
        os.path.join(HOME, ".cargo"),
        os.path.join(HOME, ".cargo", "bin"),
        os.path.join(HOME, ".cache"),
        os.path.join(HOME, "go", "bin"),
        os.path.join(HOME, ".vscode", "extensions"),
    ]
    CACHES = [
        os.path.join(LOCAL, "Google", "Chrome", "User Data", "Default", "Cache", "Cache_Data", "f_000001"),
        os.path.join(LOCAL, "Microsoft", "Edge", "User Data", "Default", "Service Worker", "CacheStorage"),
        os.path.join(LOCAL, "Microsoft", "Edge", "User Data", "GraphiteDawnCache"),
        os.path.join(ROAMING, "discord", "Code Cache", "js"),
        os.path.join(ROAMING, "Code", "CachedData", "0123abcd"),
        os.path.join(LOCAL, "Mozilla", "Firefox", "Profiles", "x.default", "cache2", "entries"),
        os.path.join(LOCAL, "NVIDIA", "DXCache"),
        os.path.join(LOCAL, "pip", "cache", "http"),
        os.path.join(HOME, ".cargo", "registry", "cache"),
        os.path.join(HOME, "go", "pkg", "mod"),
        os.path.join(config.TEMP, "anything"),
    ]

    def test_non_cache_locations_refused(self):
        for p in self.NOT_CACHES:
            with self.subTest(p=p):
                self.assertFalse(safety.is_cache_location(safety.canon(p)))
                if os.path.lexists(p):  # read-only: validation never touches the file system
                    with self.assertRaisesRegex(safety.Refused, "Hors des emplacements"):
                        safety.check_purgeable(p)

    def test_cache_locations_accepted(self):
        for p in self.CACHES:
            with self.subTest(p=p):
                self.assertTrue(safety.is_cache_location(safety.canon(p)))

    def test_every_rule_target_is_a_cache_location(self):
        """Every target a purge rule can produce (all folders faked as present) passes the allow-list."""
        names = ["Default", "Profile 1", "Guest Profile", "abcd.default-release", "app-1.0.1", "app-1.2.0",
                 "SomeApp", "some-app-updater", "0123abcd"]
        patterns = [(root, rx, check and (lambda folder: True)) for root, rx, check in safety.PURGE_CACHE_PATTERNS]
        ctx = cleaners._Ctx(None, is_admin=True, running=set(), cancel=None)
        seen = set()
        with mock.patch.object(cleaners, "_is_dir", lambda p: True), \
                mock.patch.object(cleaners, "_is_file", lambda p: True), \
                mock.patch.object(cleaners, "_subdirs", lambda p: [(n, os.path.join(p, n)) for n in names]), \
                mock.patch.object(safety, "PURGE_CACHE_PATTERNS", patterns):
            for r in cleaners._RULES:
                if r["mode"] != "purge":
                    continue
                for raw in r["fn"](ctx):
                    for t in raw["targets"]:
                        seen.add(r["id"])
                        self.assertTrue(_location_ok(t["path"]), f"{r['id']}: {t['path']}")
        needs_scan = {"inactive_node_modules"}
        self.assertEqual(seen, {r["id"] for r in cleaners._RULES if r["mode"] == "purge"} - needs_scan)

    def test_real_rule_targets_are_cache_locations(self):
        """Same check on the targets discovered on this PC (read-only: rules only list folders)."""
        ctx = cleaners._Ctx(None, is_admin=True, running=set(), cancel=None)
        for r in cleaners._RULES:
            if r["mode"] == "purge":
                for raw in r["fn"](ctx):
                    for t in raw["targets"]:
                        self.assertTrue(_location_ok(t["path"]), f"{r['id']}: {t['path']}")

    def test_squirrel_version_needs_update_exe(self):
        version = self.path("App", "app-1.0.0")
        os.makedirs(version)
        self.assertFalse(safety._is_squirrel_app(version))
        os.makedirs(self.path("App", "Update.exe"))
        self.assertFalse(safety._is_squirrel_app(version))  # a folder named Update.exe does not count
        os.rmdir(self.path("App", "Update.exe"))
        write(self.path("App", "Update.exe"), 1)
        self.assertTrue(safety._is_squirrel_app(version))

    def test_node_modules_of_installed_tools_refused(self):
        for p in (os.path.join(LOCAL, "Programs", "Microsoft VS Code", "resources", "app", "node_modules"),
                  os.path.join(ROAMING, "npm", "node_modules"),
                  os.path.join(HOME, ".vscode", "extensions", "ms-python.python", "node_modules")):
            with self.subTest(p=p):
                self.assertFalse(safety._is_regenerable_dir(p))
                self.assertFalse(safety.is_cache_location(safety.canon(p)))
        nm = self.path("proj", "node_modules")  # %TEMP% sandbox: still allowed
        os.makedirs(nm)
        self.assertTrue(safety._is_regenerable_dir(nm))


class ProfileRootTests(SandboxCase):
    """Profile roots come from C:\\Users AND the registry's ProfileList, re-read at check time."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, safety, "_profiles_cache", (float("-inf"), ()))
        p = mock.patch.object(safety, "_PROFILES_TTL_S", 0)
        p.start()
        self.addCleanup(p.stop)
        self.users = self.path("D_Users")
        self.bob = os.path.join(self.users, "bob")
        self.doc = write(os.path.join(self.bob, "Documents", "a.txt"), 5)

    def registry(self, paths, profiles_dir=None):
        return mock.patch.object(safety, "_read_profile_list", return_value=(list(paths), profiles_dir))

    def test_real_registry_profiles_are_protected(self):
        listed, _ = safety._read_profile_list()
        self.assertIn(safety.key(config.USERPROFILE), {safety.key(p) for p in listed})
        for p in listed:
            self.assertTrue(safety._exact_any(p, safety._protected_exact()), p)

    def test_profile_outside_users_folder_protected(self):
        fake = FakeRecycle(self.sb)
        with self.registry([self.bob]), mock.patch.object(winapi, "send_to_recycle_bin", fake):
            with self.assertRaisesRegex(safety.Refused, "Dossier essentiel"):
                safety.check_recyclable(self.bob)
            with self.assertRaisesRegex(safety.Refused, "contient"):
                safety.check_recyclable(self.users)
            with self.assertRaises(safety.Refused):
                safety.check_purgeable(self.bob)
            with self.assertRaises(safety.Refused):
                safety.purge(self.users)
            self.assertEqual(safety.check_recyclable(self.doc), self.doc)  # content stays deletable
            res = safety.recycle([self.bob, self.users])
        self.assertEqual((res.files, len(res.refused), fake.calls), (0, 2, []))
        self.assertTrue(os.path.isfile(self.doc))

    def test_profile_created_after_startup_is_protected(self):
        self.assertEqual(safety.check_recyclable(self.bob), self.bob)  # validation only, nothing deleted
        with self.registry([self.bob]):  # account created while the app runs
            with self.assertRaises(safety.Refused):
                safety.check_recyclable(self.bob)

    def test_cached_list_refreshed_after_ttl(self):
        with mock.patch.object(safety, "_PROFILES_TTL_S", 3600):
            safety.profile_roots()
            with self.registry([self.bob]):
                self.assertFalse(safety._exact_any(self.bob, safety.profile_roots()))  # still cached
                safety._profiles_cache = (float("-inf"), safety._profiles_cache[1])  # TTL elapsed
                self.assertTrue(safety._exact_any(self.bob, safety.profile_roots()))

    def test_profiles_directory_children_protected(self):
        alice = self.path("Profiles", "alice")
        os.makedirs(alice)
        with self.registry([], self.path("Profiles")):
            roots = safety.profile_roots()
            self.assertTrue(safety._exact_any(alice, roots))
            self.assertTrue(safety._exact_any(self.path("Profiles"), roots))


if __name__ == "__main__":
    unittest.main()
