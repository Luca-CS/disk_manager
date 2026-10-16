import copy
import os
import re
import unittest
from unittest import mock

from dm import cleaners, safety
from dm.safety import OpResult

from .common import SandboxCase, write


def make_analysis(sb: str) -> dict:
    def target(p, **kw):
        return {"path": p, "min_age_s": 0, "keep_root": True, "name_glob": kw.get("name_glob")}

    def item(id_, path, size=100, blocked=False):
        return {"id": id_, "label": id_, "path": path, "size": size, "files": 1, "note": None,
                "requires_admin": blocked, "blocked": blocked, "default_selected": True,
                "_targets": [target(path)]}

    return {
        "rules": [
            {"id": "purge_rule", "title": "Purge", "mode": "purge", "category": "system", "risk": "safe",
             "description": "d", "requires_admin": False, "blocked": False, "running": [], "size": 300, "files": 3,
             "items": [item("a1", os.path.join(sb, "ca")), item("a2", os.path.join(sb, "cb")),
                       item("a3", os.path.join(sb, "cblocked"), blocked=True)]},
            {"id": "recycle_rule", "title": "Recycle", "mode": "recycle", "category": "downloads", "risk": "review",
             "description": "d", "requires_admin": False, "blocked": False, "running": [], "size": 50, "files": 1,
             "items": [item("r1", os.path.join(sb, "old.zip"), size=50)]},
            {"id": "bin_rule", "title": "Bin", "mode": "recycle_bin", "category": "system", "risk": "moderate",
             "description": "d", "requires_admin": False, "blocked": False, "running": [], "size": 9, "files": 1,
             "items": [{"id": "b1", "label": "Corbeille", "path": "Corbeille", "size": 9, "files": 1, "note": None,
                        "requires_admin": False, "blocked": False, "default_selected": False, "_targets": []}]},
        ],
        "total": 359, "analyzed_at": 0.0, "is_admin": False, "dry_run": False,
    }


class PublicViewTests(unittest.TestCase):
    def test_strips_private_keys_deeply_without_mutating(self):
        a = make_analysis("C:\\x")
        a["rules"][0]["_secret"] = {"_nested": 1, "keep": 2, "name_glob": "zz"}
        a["rules"][0]["items"][0]["extra"] = [{"_t": 1, "min_age_s": 5, "ok": 1}]
        before = copy.copy(a["rules"][0]["items"][0]["_targets"])
        v = cleaners.public_view(a)

        def walk(o):
            if isinstance(o, dict):
                for k, val in o.items():
                    self.assertFalse(k.startswith("_"), k)
                    self.assertNotIn(k, ("min_age_s", "keep_root", "name_glob"))
                    walk(val)
            elif isinstance(o, list):
                for val in o:
                    walk(val)
        walk(v)
        self.assertEqual(v["rules"][0]["items"][0]["extra"], [{"ok": 1}])
        self.assertEqual(v["rules"][0]["items"][0]["id"], "a1")
        self.assertEqual(v["total"], 359)
        self.assertIn("_targets", a["rules"][0]["items"][0])
        self.assertEqual(a["rules"][0]["items"][0]["_targets"], before)
        # the view must be JSON serializable (compiled regexes would not be)
        import json
        json.dumps(v)

    def test_handles_empty(self):
        self.assertEqual(cleaners.public_view({"rules": []}), {"rules": []})


class RunTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.analysis = make_analysis(self.sb)
        self.purge_calls = []
        self.recycle_calls = []

        def fake_purge_many(targets, *, rule_id, cancel=None, on_progress=None):
            self.purge_calls.append((rule_id, [t["path"] for t in targets]))
            return OpResult(bytes=100 * len(targets), files=len(targets))

        def fake_recycle(paths, **kw):
            self.recycle_calls.append((list(paths), kw))
            return OpResult(bytes=50, files=len(paths))

        for name, fn in (("purge_many", fake_purge_many), ("recycle", fake_recycle)):
            p = mock.patch.object(safety, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_selects_only_requested_items_from_analysis(self):
        res = cleaners.run(self.analysis, {"purge_rule": ["a1"]})
        self.assertEqual(self.purge_calls, [("purge_rule", [os.path.join(self.sb, "ca")])])
        self.assertEqual(res["freed"], 100)
        self.assertEqual(res["files"], 1)
        self.assertEqual(res["touched_paths"], [os.path.join(self.sb, "ca")])
        self.assertEqual([r["id"] for r in res["rules"]], ["purge_rule"])
        self.assertEqual(self.recycle_calls, [])

    def test_all_skips_blocked_items(self):
        res = cleaners.run(self.analysis, {"purge_rule": "all"})
        self.assertEqual(self.purge_calls[0][1], [os.path.join(self.sb, "ca"), os.path.join(self.sb, "cb")])
        self.assertNotIn(os.path.join(self.sb, "cblocked"), res["touched_paths"])

    def test_blocked_item_selected_explicitly_is_skipped(self):
        res = cleaners.run(self.analysis, {"purge_rule": ["a3"]})
        self.assertEqual(self.purge_calls, [])
        self.assertEqual(res["rules"], [])
        self.assertEqual(res["freed"], 0)

    def test_unknown_ids_ignored(self):
        res = cleaners.run(self.analysis, {"nope": "all", "purge_rule": ["zzz", "C:\\Windows", "", 5, None]})
        self.assertEqual(self.purge_calls, [])
        self.assertEqual(self.recycle_calls, [])
        self.assertEqual(res["rules"], [])

    def test_bad_selection_shapes(self):
        for sel in (None, [], "all", 5, {"purge_rule": 12}, {"purge_rule": "ALL"}, {"purge_rule": {"a1": 1}}):
            with self.subTest(sel=sel):
                res = cleaners.run(self.analysis, sel)
                self.assertEqual(res["rules"], [])
        self.assertEqual(self.purge_calls, [])

    def test_paths_never_come_from_selection(self):
        evil = "C:\\Windows\\System32"
        cleaners.run(self.analysis, {"purge_rule": ["a2", evil, {"path": evil}], "recycle_rule": [evil, "r1"],
                                     evil: "all"})
        allowed = {os.path.join(self.sb, n) for n in ("ca", "cb", "old.zip")}
        for _, paths in self.purge_calls:
            self.assertTrue(set(paths) <= allowed)
        for paths, kw in self.recycle_calls:
            self.assertEqual(paths, [os.path.join(self.sb, "old.zip")])
            self.assertEqual(kw["sizes"], {os.path.join(self.sb, "old.zip"): 50})
            self.assertEqual(kw["source"], "clean:recycle_rule")
        self.assertEqual(self.purge_calls, [("purge_rule", [os.path.join(self.sb, "cb")])])

    def test_recycle_bin_mode(self):
        with mock.patch.object(safety, "empty_recycle_bin", return_value=OpResult(bytes=9, files=1)) as e:
            res = cleaners.run(self.analysis, {"bin_rule": "all"})
        e.assert_called_once()
        self.assertEqual(res["freed"], 9)
        self.assertEqual(self.purge_calls, [])

    def test_exception_in_rule_is_reported_not_raised(self):
        with mock.patch.object(safety, "purge_many", side_effect=RuntimeError("boom")):
            res = cleaners.run(self.analysis, {"purge_rule": "all", "recycle_rule": "all"})
        self.assertEqual(len(res["rules"]), 2)
        self.assertEqual(res["rules"][0]["errors"][0]["reason"], "boom")
        self.assertEqual(res["rules"][1]["id"], "recycle_rule")
        self.assertEqual(res["freed"], 50)

    def test_progress_and_cancel(self):
        import threading
        ev = threading.Event()
        ev.set()
        res = cleaners.run(self.analysis, {"purge_rule": "all"}, cancel=ev, progress={})
        self.assertEqual(self.purge_calls, [])
        self.assertEqual(res["rules"], [])
        prog = {}
        cleaners.run(self.analysis, {"purge_rule": "all"}, progress=prog)
        self.assertEqual(prog["done"], prog["total"])


class RealRunTests(SandboxCase):
    def test_real_purge_in_sandbox_through_run(self):
        """End-to-end with the real safety layer, targets inside the sandbox only."""
        write(os.path.join(self.sb, "ca", "x.bin"), 40)
        write(os.path.join(self.sb, "ca", "sub", "y.bin"), 60)
        write(os.path.join(self.sb, "cb", "keep.bin"), 5)
        analysis = make_analysis(self.sb)
        res = cleaners.run(analysis, {"purge_rule": ["a1"]})
        self.assertEqual((res["freed"], res["files"]), (100, 2))
        self.assertTrue(os.path.isdir(os.path.join(self.sb, "ca")))
        self.assertEqual(os.listdir(os.path.join(self.sb, "ca")), [])
        self.assertTrue(os.path.isfile(os.path.join(self.sb, "cb", "keep.bin")))
        self.assertEqual(len(self.journal_lines()), 1)

    def test_refused_target_reported(self):
        analysis = make_analysis(self.sb)
        analysis["rules"][0]["items"][0]["_targets"][0]["path"] = "C:\\Windows\\System32"
        res = cleaners.run(analysis, {"purge_rule": ["a1"]})
        self.assertEqual(res["freed"], 0)
        self.assertEqual(len(res["rules"][0]["refused"]), 1)


class AnalyzeTests(unittest.TestCase):
    """analyze() is read-only, so it is safe to run it for real."""

    @classmethod
    def setUpClass(cls):
        cls.progress = {}
        cls.analysis = cleaners.analyze(None, is_admin=False, running=set(), progress=cls.progress)

    RULE_KEYS = {"id", "title", "category", "description", "risk", "mode", "requires_admin", "blocked", "running",
                 "size", "files", "items"}
    ITEM_KEYS = {"id", "label", "path", "size", "files", "note", "requires_admin", "blocked", "default_selected",
                 "_targets"}

    def test_top_level_shape(self):
        a = self.analysis
        self.assertTrue({"rules", "total", "analyzed_at", "is_admin", "dry_run"} <= set(a))
        self.assertIs(a["is_admin"], False)
        self.assertEqual(a["dry_run"], safety.DRY_RUN)
        self.assertEqual(a["total"], sum(r["size"] for r in a["rules"]))
        sizes = [r["size"] for r in a["rules"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(self.progress["done"], self.progress["total"])

    def test_rule_and_item_shapes(self):
        ids = set()
        for r in self.analysis["rules"]:
            self.assertEqual(set(r), self.RULE_KEYS, r["id"])
            self.assertNotIn(r["id"], ids)
            ids.add(r["id"])
            self.assertIn(r["mode"], ("purge", "recycle", "recycle_bin"))
            self.assertIn(r["risk"], ("safe", "moderate", "review"))
            self.assertTrue(r["items"])
            for it in r["items"]:
                self.assertEqual(set(it), self.ITEM_KEYS, (r["id"], it["label"]))
                self.assertGreater(it["size"], 0)
                self.assertIsInstance(it["id"], str)
                self.assertIs(type(it["blocked"]), bool)
                if it["blocked"]:
                    self.assertTrue(it["requires_admin"])
                    self.assertFalse(it["default_selected"])
                for t in it["_targets"]:
                    self.assertTrue({"path", "min_age_s", "keep_root", "name_glob"} <= set(t))

    def test_item_ids_unique_within_rule(self):
        for r in self.analysis["rules"]:
            ids = [i["id"] for i in r["items"]]
            self.assertEqual(len(ids), len(set(ids)), r["id"])

    def test_purge_targets_pass_safety_validation(self):
        for r in self.analysis["rules"]:
            if r["mode"] != "purge":
                continue
            for it in r["items"]:
                for t in it["_targets"]:
                    try:
                        safety.check_purgeable(t["path"])
                    except safety.Refused as e:
                        self.fail(f"{r['id']}: {t['path']} would be refused: {e}")

    def test_non_admin_blocks_admin_items(self):
        for r in self.analysis["rules"]:
            if r["requires_admin"]:
                self.assertTrue(r["blocked"])
            for it in r["items"]:
                if it["requires_admin"]:
                    self.assertTrue(it["blocked"])
            ok = [i for i in r["items"] if not i["blocked"]]
            self.assertEqual(r["size"], sum(i["size"] for i in ok))

    def test_public_view_of_real_analysis_is_clean(self):
        import json
        v = cleaners.public_view(self.analysis)
        text = json.dumps(v)
        self.assertNotIn("_targets", text)
        self.assertNotIn("name_glob", text)

    def test_running_processes_consistent_with_default_selection(self):
        for r in self.analysis["rules"]:
            if not r["running"]:
                continue
            for it in r["items"]:
                if it["note"] and "est ouvert" in it["note"]:
                    self.assertFalse(it["default_selected"])

    def test_cancelled_analyze_returns_partial(self):
        import threading
        ev = threading.Event()
        ev.set()
        a = cleaners.analyze(None, is_admin=False, running=set(), cancel=ev)
        self.assertEqual(a["rules"], [])

    def test_advice_cards(self):
        cards = cleaners.advice(None, None)
        self.assertTrue(cards)
        for c in cards:
            self.assertTrue({"id", "title", "body", "size", "severity", "action"} <= set(c))
            self.assertIn(c["severity"], ("info", "tip", "warning"))
            act = c["action"]
            if act and act["type"] == "open":
                from dm.api import SHELL_TARGETS
                self.assertIn(act["target"], SHELL_TARGETS)


if __name__ == "__main__":
    unittest.main()
