"""Elevated mode: nothing a non-elevated process of the user can change may steer the administrator.

* data files (log, journal, scan cache, history) live in the admin-only folder, never in data/;
* system locations come from the kernel / HKLM / the process token, never from the environment
  (an elevated process inherits the user's, who can set any variable in HKCU\\Environment);
* child processes (PowerShell, browser) get a trusted environment.

Non-elevated tests cannot create Administrators-owned folders, so the private folder checks run
with the current user's SID in place of Administrators. Everything happens inside a dm_test_*
sandbox under %TEMP%; the "poisoned" environment variables point into that sandbox.
"""

import functools
import importlib
import logging
import logging.handlers
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from dm import config, journal, safety, system_info, window, winapi

from .common import SandboxCase, mkjunction, write

SY = winapi.SID_SYSTEM
REAL_ENSURE_DATA_DIRS = config.ensure_data_dirs  # SandboxCase replaces it with a plain mkdir
_LOCATION_VARS = ("SystemDrive", "SystemRoot", "windir", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
                  "APPDATA", "ProgramData", "ALLUSERSPROFILE", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
                  "TEMP", "TMP")


def nc(p) -> str:
    return os.path.normcase(str(p)).rstrip("\\")


class PoisonedEnvCase(SandboxCase):
    """Every location variable points to an existing folder of the sandbox."""

    def setUp(self):
        super().setUp()
        self.clean, ok = config._trusted_locations()
        self.assertTrue(ok)
        self.original_env = dict(os.environ)
        self.evil = self.path("evil")
        for sub in ("ProgramData", "Program Files", "Windows", r"AppData\Local\Temp", r"AppData\Roaming"):
            os.makedirs(os.path.join(self.evil, sub))
        e = self.evil
        poison = {v: os.path.join(e, v) for v in _LOCATION_VARS}
        poison.update({"SystemDrive": e, "SystemRoot": os.path.join(e, "Windows"), "windir": os.path.join(e, "Windows"),
                       "USERPROFILE": e, "LOCALAPPDATA": os.path.join(e, "AppData", "Local"),
                       "APPDATA": os.path.join(e, "AppData", "Roaming"), "ProgramData": os.path.join(e, "ProgramData"),
                       "ALLUSERSPROFILE": os.path.join(e, "ProgramData"),
                       "ProgramFiles": os.path.join(e, "Program Files"),
                       "TEMP": os.path.join(e, "AppData", "Local", "Temp"),
                       "TMP": os.path.join(e, "AppData", "Local", "Temp")})
        p = mock.patch.dict(os.environ, poison)
        p.start()
        self.addCleanup(p.stop)

    def assert_not_evil(self, path):
        self.assertFalse(nc(path).startswith(nc(self.evil)), path)


class TrustedLocationsTests(PoisonedEnvCase):
    def test_poison_reaches_the_environment_based_values(self):
        env = config._env_locations()
        for k in ("SYSTEM_ROOT", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAM_FILES", "TEMP"):
            self.assertTrue(nc(env[k]).startswith(nc(self.evil)), (k, env[k]))

    def test_trusted_values_ignore_the_environment(self):
        trusted, ok = config._trusted_locations()
        self.assertTrue(ok)
        self.assertEqual({k: nc(v) for k, v in trusted.items()}, {k: nc(v) for k, v in self.clean.items()})
        for k, v in trusted.items():
            self.assert_not_evil(v)
        self.assertEqual(nc(trusted["LOCALAPPDATA"]), nc(os.path.join(trusted["USERPROFILE"], "AppData", "Local")))
        self.assertEqual(nc(trusted["TEMP"]), nc(os.path.join(trusted["LOCALAPPDATA"], "Temp")))
        self.assertEqual(nc(trusted["SYSTEM_DRIVE"]), nc(trusted["SYSTEM_ROOT"][:3]))

    def test_known_folder_program_data_does_follow_the_environment(self):
        # Why config does not use FOLDERID_ProgramData: it is expanded from %SystemDrive% / %ProgramData%.
        # (Program Files is a plain HKLM value.) Run in a fresh process: the shell caches known folders.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from dm import winapi; "
                "print(winapi.known_folder(winapi.FOLDERID_PROGRAM_DATA)); "
                "print(winapi.known_folder(winapi.FOLDERID_PROGRAM_FILES))")
        out = subprocess.run([sys.executable, "-c", code, str(config.ROOT_DIR)], capture_output=True, text=True,
                             timeout=60, env=dict(os.environ)).stdout.splitlines()
        self.assertEqual(nc(out[0]), nc(os.path.join(self.evil, "ProgramData")))
        self.assertEqual(nc(out[1]), nc(self.clean["PROGRAM_FILES"]))

    def test_admin_data_dir_ignores_the_environment(self):
        self.assertEqual(nc(config.admin_data_dir()), nc(os.path.join(self.clean["PROGRAMDATA"], config.APP_NAME)))
        self.assert_not_evil(config.admin_data_dir())

    def test_elevated_import_uses_trusted_locations_and_admin_folder(self):
        try:
            with mock.patch.object(winapi, "is_admin", return_value=True):
                importlib.reload(config)
            self.assertTrue(config.ELEVATED)
            for name in ("SYSTEM_ROOT", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAM_FILES",
                         "PROGRAM_FILES_X86", "TEMP", "SYSTEM_DRIVE"):
                self.assertEqual(nc(getattr(config, name)), nc(self.clean[name]), name)
            admin = nc(os.path.join(self.clean["PROGRAMDATA"], config.APP_NAME))
            self.assertEqual(nc(config.DATA_DIR), admin)
            for p in (config.SCANS_DIR, config.JOURNAL_PATH, config.LOG_PATH, config.EDGE_PROFILE_DIR,
                      config.INSTANCE_PATH):
                self.assertTrue(nc(p).startswith(admin + "\\"), p)
                self.assertFalse(nc(p).startswith(nc(config.ROOT_DIR)), p)
        finally:
            with mock.patch.dict(os.environ, self.original_env, clear=True):
                importlib.reload(config)
        self.assertFalse(config.ELEVATED)
        self.assertEqual(nc(config.DATA_DIR), nc(config.ROOT_DIR / "data"))

    def test_fallbacks_are_flagged(self):
        with mock.patch.object(winapi, "user_profile_dir", return_value=None):
            loc, ok = config._trusted_locations()
        self.assertFalse(ok)
        self.assertTrue(safety.is_within(loc["USERPROFILE"], os.path.join(loc["SYSTEM_DRIVE"], "Users", "Default")))
        self.assert_not_evil(loc["USERPROFILE"])
        with mock.patch.object(winapi, "system_windows_dir", return_value=None):
            loc, ok = config._trusted_locations()
        self.assertFalse(ok)
        self.assert_not_evil(loc["SYSTEM_ROOT"])

    def test_expand_system_vars(self):
        root = r"C:\Windows"
        self.assertEqual(config.expand_system_vars(r"%SystemDrive%\Users\x", root), r"C:\Users\x")
        self.assertEqual(config.expand_system_vars(r"%SYSTEMROOT%\Temp", root), r"C:\Windows\Temp")
        self.assertEqual(config.expand_system_vars(r"%windir%\ServiceProfiles\..\Temp", root), r"C:\Windows\Temp")
        self.assertEqual(config.expand_system_vars(r"D:\Profiles\x", root), r"D:\Profiles\x")
        for bad in (r"%USERPROFILE%\x", r"%SystemDrive%\%USERNAME%", r"Users\x", r"\\server\share", None, 3):
            self.assertIsNone(config.expand_system_vars(bad, root), bad)

    def test_profile_list_expansion_ignores_the_environment(self):
        p = safety._expand_profile_path(r"%SystemDrive%\Users\someone")
        self.assertEqual(nc(p), nc(os.path.join(config.SYSTEM_DRIVE, "Users", "someone")))
        self.assert_not_evil(p)
        self.assertIsNone(safety._expand_profile_path(r"%USERPROFILE%\..\someone"))

    def test_long_path_does_not_resolve_links(self):
        target, link = self.path("target"), self.path("link")
        os.makedirs(os.path.join(target, "inner"))
        self.assertTrue(mkjunction(link, target))
        self.assertEqual(nc(winapi.long_path(os.path.join(link, "inner"))), nc(os.path.join(link, "inner")))
        self.assertEqual(nc(os.path.realpath(os.path.join(link, "inner"))), nc(os.path.join(target, "inner")))

    def test_volume_system_flag_ignores_the_environment(self):
        vols = {v["root"].lower(): v["is_system"] for v in winapi.list_volumes()}
        self.assertTrue(vols.get(self.clean["SYSTEM_DRIVE"].lower()))


class ChildEnvTests(PoisonedEnvCase):
    def setUp(self):
        super().setUp()
        self.mods = self.path("mods")
        # A user module that would replace Get-Disk in an elevated PowerShell.
        write(os.path.join(self.mods, "Storage", "Storage.psd1"),
              data=b"@{ ModuleVersion = '99.0'; RootModule = 'Storage.psm1'; FunctionsToExport = @('Get-Disk') }\n")
        write(os.path.join(self.mods, "Storage", "Storage.psm1"), data=b"function Get-Disk { 'planted' }\n")
        sys32 = os.path.join(self.clean["SYSTEM_ROOT"], "System32")
        p = mock.patch.dict(os.environ, {
            "PSModulePath": self.mods + ";" + os.path.join(sys32, "WindowsPowerShell", "v1.0", "Modules"),
            "PYTHONPATH": self.mods, "Path": self.mods + ";" + os.environ.get("Path", "")})
        p.start()
        self.addCleanup(p.stop)
        self.ps = os.path.join(sys32, "WindowsPowerShell", "v1.0", "powershell.exe")

    def elevated_env(self) -> dict:
        trusted, _ = config._trusted_locations()
        with mock.patch.object(config, "ELEVATED", True), \
                mock.patch.multiple(config, **{k: v for k, v in trusted.items()}):
            return config.child_env()

    def test_not_elevated_inherits(self):
        self.assertIsNone(config.child_env())

    def test_elevated_env_has_only_trusted_values(self):
        env = self.elevated_env()
        for k, v in env.items():
            self.assertNotIn(nc(self.mods), nc(v), k)
            self.assert_not_evil(v)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(nc(env["PSModulePath"]),
                         nc(os.path.join(self.clean["SYSTEM_ROOT"], "System32", "WindowsPowerShell", "v1.0", "Modules")))
        self.assertEqual(nc(env["SystemRoot"]), nc(self.clean["SYSTEM_ROOT"]))

    def test_powershell_does_not_load_the_users_module(self):
        # Bypass in both runs: the execution policy is no defence (the user sets their own CurrentUser policy,
        # and binary modules ignore it); only the module search path differs.
        cmd = [self.ps, "-NoProfile", "-NonInteractive", "-NoLogo", "-ExecutionPolicy", "Bypass", "-Command",
               "(Get-Command Get-Disk).Module.ModuleBase"]

        def run(env):
            r = subprocess.run(cmd, capture_output=True, timeout=60, env=env,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            return nc(r.stdout.decode("utf-8", "replace").strip())

        attacked = dict(self.original_env, PSModulePath=os.environ["PSModulePath"])
        self.assertEqual(run(attacked), nc(os.path.join(self.mods, "Storage")))  # the attack works...
        self.assertTrue(run(self.elevated_env()).startswith(nc(self.clean["SYSTEM_ROOT"])))  # ...not elevated

    def test_subprocesses_receive_the_child_env(self):
        marker = {"SystemRoot": r"C:\Windows"}
        with mock.patch.object(config, "child_env", return_value=marker), \
                mock.patch.object(system_info.subprocess, "run", side_effect=RuntimeError("stop")) as run:
            with self.assertRaises(RuntimeError):
                system_info._collect()
        self.assertIs(run.call_args.kwargs["env"], marker)
        with mock.patch.object(config, "child_env", return_value=marker), \
                mock.patch.object(window, "find_browser", return_value=r"C:\PF\msedge.exe"), \
                mock.patch.object(window, "admin_profile_dir", return_value=r"C:\PD\Disk Manager\edge-profile"), \
                mock.patch.object(window.subprocess, "Popen") as popen:
            window.open_window("http://127.0.0.1:1/#c=x", elevated=True)
        self.assertIs(popen.call_args.kwargs["env"], marker)


class AdminDataDirCase(SandboxCase):
    """Elevated, with the admin data folder (%ProgramData%\\Disk Manager) redirected to the sandbox."""

    def setUp(self):
        super().setUp()
        self.me = winapi.security_info(self.sb)["owner"]
        self.pd = self.path("ProgramData")
        os.mkdir(self.pd)
        self.root = os.path.join(self.pd, config.APP_NAME)
        self.victim = write(self.path("victim.txt"), data=b"keep")
        root = Path(self.root)
        allowed = {SY, self.me}
        sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{self.me})"
        for p in (
            mock.patch.object(config, "ensure_data_dirs", REAL_ENSURE_DATA_DIRS),
            mock.patch.object(config, "ELEVATED", True),
            mock.patch.object(config, "DATA_DIR", root),
            mock.patch.object(config, "SCANS_DIR", root / "scans"),
            mock.patch.object(config, "JOURNAL_PATH", root / "journal.jsonl"),
            mock.patch.object(config, "LOG_PATH", root / "app.log"),
            mock.patch.object(config, "SYSTEM_OWNERS", frozenset({SY, self.me})),
            mock.patch.object(winapi, "ensure_private_dir",
                              functools.partial(winapi.ensure_private_dir, allowed_sids=allowed, sddl=sddl)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def assert_victim_intact(self):
        with open(self.victim, "rb") as f:
            self.assertEqual(f.read(), b"keep")


class AdminDataDirTests(AdminDataDirCase):

    def test_creates_private_folder_and_writes_there(self):
        config.ensure_data_dirs()
        self.assertTrue(winapi.is_private_dir(self.root, {SY, self.me}))
        journal.append("purge", path=r"C:\x", bytes=1)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "journal.jsonl")))
        self.assertEqual({sid for _, sid in winapi.security_info(os.path.join(self.root, "scans"))["aces"]},
                         {SY, self.me})
        config.ensure_data_dirs()  # existing private folder accepted again

    def test_refuses_folder_planted_with_a_hardlink(self):
        os.mkdir(self.root)
        os.link(self.victim, os.path.join(self.root, "journal.jsonl"))
        with self.assertRaises(PermissionError):
            config.ensure_data_dirs()
        with self.assertRaises(PermissionError):
            journal.append("purge", path=r"C:\x", bytes=1)
        self.assert_victim_intact()

    def test_refuses_folder_replaced_by_a_junction(self):
        elsewhere = self.path("elsewhere")
        self.assertTrue(winapi.ensure_private_dir(elsewhere))  # private, but not at that path
        self.assertTrue(mkjunction(self.root, elsewhere))
        with self.assertRaises(PermissionError):
            config.ensure_data_dirs()
        self.assertEqual(os.listdir(elsewhere), [])

    def test_refuses_untrusted_parent(self):
        with mock.patch.object(config, "SYSTEM_OWNERS", frozenset({SY})):
            with self.assertRaises(PermissionError):
                config.ensure_data_dirs()
        self.assertFalse(os.path.exists(self.root))

    def test_not_elevated_uses_data_folder(self):
        data = Path(self.path("data"))
        with mock.patch.object(config, "ELEVATED", False), mock.patch.object(config, "DATA_DIR", data), \
                mock.patch.object(config, "SCANS_DIR", data / "scans"):
            config.ensure_data_dirs()
        self.assertTrue(os.path.isdir(data / "scans"))
        self.assertFalse(os.path.exists(self.root))


class ElevatedStartupTests(AdminDataDirCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(sys, "argv", ["run.py"]):
            import run
        cls.app = run

    def setUp(self):
        super().setUp()
        root_logger = logging.getLogger()
        before = list(root_logger.handlers)

        def restore():
            for h in list(root_logger.handlers):
                if h not in before:
                    root_logger.removeHandler(h)
                    h.close()
        self.addCleanup(restore)

    def test_log_goes_to_the_private_folder(self):
        self.assertTrue(self.app._start_logging())
        handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertIn(nc(os.path.join(self.root, "app.log")), [nc(h.baseFilename) for h in handlers])
        self.assertTrue(winapi.is_private_dir(self.root, {SY, self.me}))

    def test_planted_log_folder_stops_the_elevated_start(self):
        os.mkdir(self.root)
        os.link(self.victim, os.path.join(self.root, "app.log"))
        before = list(logging.getLogger().handlers)
        self.assertFalse(self.app._start_logging())
        self.assertEqual(logging.getLogger().handlers, before)
        self.assert_victim_intact()

    def test_untrusted_locations_stop_the_elevated_start(self):
        with mock.patch.object(config, "TRUSTED_LOCATIONS_OK", False), \
                mock.patch.object(config, "ensure_data_dirs", side_effect=AssertionError("wrote something")):
            self.assertFalse(self.app._start_logging())

    def test_main_shows_an_error_and_exits(self):
        with mock.patch.object(self.app, "_start_logging", return_value=False), \
                mock.patch.object(winapi, "show_error") as err, \
                mock.patch.object(self.app, "AppState", side_effect=AssertionError("started")):
            self.assertEqual(self.app.main(), 1)
        self.assertIn(self.root, err.call_args.args[1])

    def test_elevated_relaunch_ignores_python_environment(self):
        params = self.app._elevate_params(r"C:\app\run.py")
        self.assertTrue(params.startswith('-E -s -B "C:\\app\\run.py" --after-elevate'), params)


if __name__ == "__main__":
    unittest.main()
