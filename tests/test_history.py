import json

from dm import history

from .common import SandboxCase


def snap(id_, total, folders, ts=0):
    return {"id": id_, "ts": ts, "root": "C:\\", "total": total, "files": 1, "volume": None, "folders": folders}


class CompareTests(SandboxCase):
    def write_snaps(self, *snaps):
        with open(history.SNAPSHOTS, "w", encoding="utf-8") as f:
            for s in snaps:
                f.write(json.dumps(s) + "\n")

    def test_compare_deltas(self):
        a = snap("1", 1000, {"C:\\": 1000, "C:\\gone": 300, "C:\\same": 200, "C:\\grow": 100}, ts=10)
        b = snap("2", 1500, {"C:\\": 1500, "C:\\new": 400, "C:\\same": 200, "C:\\grow": 150}, ts=20)
        self.write_snaps(a, b)
        res = history.compare("1", "2")
        self.assertEqual(res["delta"], 500)
        self.assertEqual(res["a"], {"id": "1", "ts": 10, "root": "C:\\", "total": 1000})
        self.assertEqual(res["b"], {"id": "2", "ts": 20, "root": "C:\\", "total": 1500})
        by = {c["path"]: c for c in res["changes"]}
        self.assertNotIn("C:\\same", by)
        self.assertEqual(by["C:\\gone"], {"path": "C:\\gone", "before": 300, "after": None, "delta": -300})
        self.assertEqual(by["C:\\new"], {"path": "C:\\new", "before": None, "after": 400, "delta": 400})
        self.assertEqual(by["C:\\grow"]["delta"], 50)
        self.assertEqual(by["C:\\"]["delta"], 500)
        deltas = [abs(c["delta"]) for c in res["changes"]]
        self.assertEqual(deltas, sorted(deltas, reverse=True))

    def test_compare_reverse_negates(self):
        self.write_snaps(snap("1", 100, {"C:\\x": 100}), snap("2", 40, {"C:\\x": 40}))
        self.assertEqual(history.compare("1", "2")["delta"], -60)
        self.assertEqual(history.compare("1", "2")["changes"][0]["delta"], -60)
        self.assertEqual(history.compare("2", "1")["changes"][0]["delta"], 60)

    def test_compare_limit(self):
        folders_b = {f"C:\\f{i}": i + 1 for i in range(10)}
        self.write_snaps(snap("1", 0, {}), snap("2", 55, folders_b))
        res = history.compare("1", "2", limit=3)
        self.assertEqual([c["delta"] for c in res["changes"]], [10, 9, 8])

    def test_compare_unknown_ids(self):
        self.assertIsNone(history.compare("1", "2"))  # no file at all
        self.write_snaps(snap("1", 1, {}))
        self.assertIsNone(history.compare("1", "2"))
        self.assertIsNone(history.compare("9", "1"))
        self.assertIsNone(history.compare("", ""))

    def test_list_snapshots_hides_folders_most_recent_first(self):
        self.write_snaps(snap("1", 1, {"C:\\a": 1}), snap("2", 2, {"C:\\a": 2}))
        out = history.list_snapshots()
        self.assertEqual([s["id"] for s in out], ["2", "1"])
        self.assertTrue(all("folders" not in s for s in out))

    def test_corrupt_lines_ignored(self):
        with open(history.SNAPSHOTS, "w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write(json.dumps(snap("1", 1, {"C:\\a": 1})) + "\n")
            f.write("{broken\n")
            f.write(json.dumps(snap("2", 3, {"C:\\a": 3})) + "\n")
        self.assertEqual(history.compare("1", "2")["delta"], 2)


class UsageTests(SandboxCase):
    def test_usage_series_filters_root(self):
        history._append(history.USAGE, {"ts": 1, "root": "C:\\", "total": 10, "used": 5})
        history._append(history.USAGE, {"ts": 2, "root": "D:\\", "total": 10, "used": 6})
        history._append(history.USAGE, {"ts": 3, "root": "c:\\", "total": 10, "used": 7})
        rows = history.usage_series("C:\\")
        self.assertEqual([r["used"] for r in rows], [5, 7])
        self.assertEqual(len(history.usage_series(None)), 3)
        self.assertEqual(len(history.usage_series("C:\\", limit=1)), 1)
