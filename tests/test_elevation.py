"""Elevated mode: the admin window must not read anything a non-elevated process can write.

Non-elevated tests cannot create Administrators-owned folders, so the private
folder checks run with the current user's SID in place of Administrators.
Everything happens inside a dm_test_* sandbox under %TEMP%.
"""

import functools
import os
import sys
import unittest
from unittest import mock

from dm import config, window, winapi

from .common import SandboxCase, write

SY = winapi.SID_SYSTEM


class PrivateDirTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.me = winapi.security_info(self.sb)["owner"]
        self.allowed = {SY, self.me}
        self.sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{self.me})"

    def test_creates_protected_dir(self):
        p = self.path("priv")
        self.assertTrue(winapi.ensure_private_dir(p, self.allowed, self.sddl))
        info = winapi.security_info(p)
        self.assertEqual(info["owner"], self.me)
        self.assertTrue(info["protected"])
        self.assertEqual(info["aces"], [(winapi.ACCESS_ALLOWED_ACE_TYPE, SY), (winapi.ACCESS_ALLOWED_ACE_TYPE, self.me)])
        self.assertTrue(winapi.ensure_private_dir(p, self.allowed, self.sddl))  # existing and still private

    def test_children_inherit_the_protection(self):
        p = self.path("priv")
        self.assertTrue(winapi.ensure_private_dir(p, self.allowed, self.sddl))
        os.mkdir(os.path.join(p, "child"))
        aces = winapi.security_info(os.path.join(p, "child"))["aces"]
        self.assertEqual({sid for _, sid in aces}, self.allowed)

    def test_refuses_existing_dir_with_inherited_dacl(self):
        p = self.path("planted")
        os.mkdir(p)
        write(os.path.join(p, "Preferences"), data=b"{}")
        self.assertFalse(winapi.is_private_dir(p, self.allowed))
        self.assertFalse(winapi.ensure_private_dir(p, self.allowed, self.sddl))
        self.assertTrue(os.path.isfile(os.path.join(p, "Preferences")))  # refused, never "fixed" or emptied

    def test_refuses_extra_grantee(self):
        for extra in ("(A;OICI;FA;;;WD)", "(A;OICI;FR;;;BU)", "(A;OICI;FA;;;S-1-3-0)"):
            p = self.path("extra" + str(abs(hash(extra))))
            self.assertFalse(winapi.ensure_private_dir(p, self.allowed, self.sddl + extra), extra)
            self.assertTrue(os.path.isdir(p))

    def test_deny_aces_are_accepted(self):
        p = self.path("deny")
        self.assertTrue(winapi.ensure_private_dir(p, self.allowed, self.sddl + "(D;OICI;FA;;;AN)"))

    def test_refuses_null_dacl(self):
        p = self.path("nulldacl")
        self.assertFalse(winapi.ensure_private_dir(p, self.allowed, "D:NO_ACCESS_CONTROL"))

    def test_refuses_owner_not_allowed(self):
        # Every ACE names an allowed SID (OW = OWNER RIGHTS keeps the folder removable), but the owner is the user.
        p = self.path("owner")
        self.assertFalse(winapi.ensure_private_dir(p, {SY, "S-1-3-4"}, "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;OW)"))
        self.assertTrue(winapi.ensure_private_dir(p, {SY, "S-1-3-4", self.me}, "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;OW)"))

    def test_refuses_junction_to_private_dir(self):
        target, link = self.path("priv"), self.path("link")
        self.assertTrue(winapi.ensure_private_dir(target, self.allowed, self.sddl))
        self.junction(link, target)
        self.assertIsNone(winapi.security_info(link))
        self.assertFalse(winapi.is_private_dir(link, self.allowed))
        self.assertFalse(winapi.ensure_private_dir(link, self.allowed, self.sddl))

    def test_refuses_file_and_missing(self):
        f = write(self.path("file"), data=b"x")
        self.assertFalse(winapi.is_private_dir(f, self.allowed))
        self.assertFalse(winapi.ensure_private_dir(f, self.allowed, self.sddl))
        self.assertFalse(winapi.is_private_dir(self.path("missing"), self.allowed))

    def test_admin_descriptor_cannot_be_forged_without_elevation(self):
        if winapi.is_admin():
            self.skipTest("running elevated")
        p = self.path("admin")
        self.assertFalse(winapi.ensure_private_dir(p))  # O:BA refused for a non-elevated token
        self.assertFalse(os.path.exists(p))


class AdminProfileTests(SandboxCase):
    """window.admin_profile_dir with the admin data folder redirected to the sandbox."""

    def setUp(self):
        super().setUp()
        self.me = winapi.security_info(self.sb)["owner"]
        self.pd = self.path("ProgramData")
        os.mkdir(self.pd)
        self.root = os.path.join(self.pd, config.APP_NAME)
        allowed = {SY, self.me}
        sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{self.me})"
        for p in (
            mock.patch.object(config, "admin_data_dir", lambda: self.root),
            mock.patch.object(winapi, "ensure_private_dir",
                              functools.partial(winapi.ensure_private_dir, allowed_sids=allowed, sddl=sddl)),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.trust_parent = mock.patch.object(config, "SYSTEM_OWNERS", frozenset({self.me}))

    def assert_refused(self):
        with self.assertLogs("dm.window", "ERROR"):
            self.assertIsNone(window.admin_profile_dir())

    def test_creates_protected_root(self):
        with self.trust_parent:
            profile = window.admin_profile_dir()
        self.assertEqual(profile, os.path.join(self.root, "edge-profile"))
        self.assertTrue(winapi.is_private_dir(self.root, {SY, self.me}))
        self.assertNotEqual(os.path.normcase(profile), os.path.normcase(str(config.EDGE_PROFILE_DIR)))
        with self.trust_parent:
            self.assertEqual(window.admin_profile_dir(), profile)  # reused on the next launch

    def test_refuses_root_planted_by_the_user(self):
        write(os.path.join(self.root, "edge-profile", "Local State"), data=b"{}")
        with self.trust_parent:
            self.assert_refused()
        self.assertTrue(os.path.isfile(os.path.join(self.root, "edge-profile", "Local State")))

    def test_refuses_root_replaced_by_a_link(self):
        elsewhere = self.path("elsewhere")
        self.assertTrue(winapi.ensure_private_dir(elsewhere))  # patched: a private folder, but not at that path
        self.junction(self.root, elsewhere)
        with self.trust_parent:
            self.assert_refused()

    def test_refuses_profile_link_inside_root(self):
        with self.trust_parent:
            profile = window.admin_profile_dir()
        os.mkdir(self.path("user_profile"))
        self.junction(profile, self.path("user_profile"))
        with self.trust_parent:
            self.assert_refused()

    def test_refuses_untrusted_parent(self):
        self.assert_refused()  # sandbox parent is owned by the user
        self.assertFalse(os.path.exists(self.root))

    def test_profile_is_in_the_admin_data_folder(self):
        self.assertEqual(window.admin_profile_root(), self.root)


class OpenWindowTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(window.subprocess, "Popen"), mock.patch.object(window.webbrowser, "open")):
            p.start()
            self.addCleanup(p.stop)

    def test_elevated_window_uses_protected_profile(self):
        with mock.patch.object(window, "find_browser", return_value=r"C:\PF\msedge.exe") as fb, \
                mock.patch.object(window, "admin_profile_dir", return_value=r"C:\PD\Disk Manager\edge-profile"):
            self.assertIsNotNone(window.open_window("http://127.0.0.1:1/#c=x", elevated=True))
        fb.assert_called_once_with(True)
        args = window.subprocess.Popen.call_args.args[0]
        self.assertIn(r"--user-data-dir=C:\PD\Disk Manager\edge-profile", args)
        self.assertFalse(any(str(config.EDGE_PROFILE_DIR) in a for a in args))

    def test_elevated_window_never_falls_back(self):
        cases = [(None, r"C:\PD\p"), (r"C:\PF\msedge.exe", None)]
        for exe, profile in cases:
            with mock.patch.object(window, "find_browser", return_value=exe), \
                    mock.patch.object(window, "admin_profile_dir", return_value=profile):
                self.assertIsNone(window.open_window("http://127.0.0.1:1/#c=x", elevated=True))
        window.subprocess.Popen.assert_not_called()
        window.webbrowser.open.assert_not_called()

    def test_normal_window_keeps_data_profile(self):
        data_profile = mock.MagicMock(__str__=lambda _: r"C:\app\data\edge-profile")
        with mock.patch.object(window, "find_browser", return_value=r"C:\PF\msedge.exe"), \
                mock.patch.object(window, "admin_profile_dir", side_effect=AssertionError), \
                mock.patch.object(config, "EDGE_PROFILE_DIR", data_profile):
            window.open_window("http://127.0.0.1:1/#t=x")
        self.assertIn(r"--user-data-dir=C:\app\data\edge-profile", window.subprocess.Popen.call_args.args[0])

    def test_elevated_browsers_come_from_program_files_only(self):
        folders = {winapi.FOLDERID_PROGRAM_FILES: r"C:\PF", winapi.FOLDERID_PROGRAM_FILES_X86: r"C:\PF86"}
        with mock.patch.object(winapi, "known_folder", folders.get), \
                mock.patch.dict(os.environ, {"ProgramFiles": r"C:\Users\x\fake"}):
            bases = {base for base, _ in window._admin_candidates()}
            self.assertEqual(bases, {r"C:\PF", r"C:\PF86"})
            seen = []
            with mock.patch.object(window.os.path, "isfile", lambda p: seen.append(p) or False):
                self.assertIsNone(window.find_browser(elevated=True))
        self.assertTrue(seen)
        self.assertTrue(all(p.startswith(("C:\\PF\\", "C:\\PF86\\")) for p in seen), seen)


class HandOffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(sys, "argv", ["run.py"]):
            import run
        cls.app = run

    def test_elevated_launch_ignores_instance_file(self):
        with mock.patch.object(self.app, "_existing_instance", side_effect=AssertionError("instance.json read")), \
                mock.patch.object(self.app.window, "open_window") as ow:
            self.assertFalse(self.app._hand_off_to_running_instance(elevated=True))
        ow.assert_not_called()

    def test_normal_launch_hands_off(self):
        with mock.patch.object(self.app, "_existing_instance", return_value={"port": 1234, "token": "abc"}), \
                mock.patch.object(self.app.window, "open_window") as ow:
            self.assertTrue(self.app._hand_off_to_running_instance(elevated=False))
        ow.assert_called_once_with("http://127.0.0.1:1234/#t=abc")

    def test_normal_launch_without_instance(self):
        with mock.patch.object(self.app, "_existing_instance", return_value=None), \
                mock.patch.object(self.app.window, "open_window") as ow:
            self.assertFalse(self.app._hand_off_to_running_instance(elevated=False))
        ow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
