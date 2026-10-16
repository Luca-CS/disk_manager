import http.client
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from dm import config
from dm.api import AppState, build_router
from dm.server import AppServer, serve_forever_in_thread

from .common import SandboxCase, write


class ServerCase(SandboxCase):
    def setUp(self):
        super().setUp()
        self.state = AppState()
        self.server = AppServer(build_router(self.state))
        self.server.handle_error = lambda *a: None  # abrupt client disconnects are expected in some tests
        self.thread = serve_forever_in_thread(self.server)
        self.addCleanup(self._shutdown)
        self.port = self.server.port
        self.token = self.server.token
        self.host = f"127.0.0.1:{self.port}"

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def req(self, method, path, *, headers=None, body=None, token=True, host=True, json_body=None):
        """One request per connection. Returns (status, headers, parsed body or raw bytes)."""
        h = {}
        if host is True:
            h["Host"] = self.host
        elif host:
            h["Host"] = host
        if token is True:
            h["X-DM-Token"] = self.token
        elif token:
            h["X-DM-Token"] = token
        if json_body is not None:
            body = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            raw = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            if hdrs.get("content-type", "").startswith("application/json"):
                try:
                    return r.status, hdrs, json.loads(raw)
                except ValueError:
                    pass
            return r.status, hdrs, raw
        finally:
            c.close()

    def assertError(self, resp, status, code=None):
        self.assertEqual(resp[0], status, resp[2])
        if code is not None:
            self.assertEqual(resp[2]["error"]["code"], code)


class ApiAuthTests(ServerCase):
    def test_ok_with_token(self):
        st, hdrs, body = self.req("GET", "/api/meta")
        self.assertEqual(st, 200)
        self.assertIn("version", body)
        self.assertIn("categories", body)

    def test_localhost_host_accepted(self):
        st, _, _ = self.req("GET", "/api/meta", host=f"localhost:{self.port}")
        self.assertEqual(st, 200)

    def test_missing_token_401(self):
        self.assertError(self.req("GET", "/api/meta", token=False), 401, "bad_token")

    def test_bad_token_401(self):
        self.assertError(self.req("GET", "/api/meta", token="wrong"), 401)
        self.assertError(self.req("GET", "/api/meta", token=self.token[:-1]), 401)
        self.assertError(self.req("GET", "/api/meta", token=self.token + "x"), 401)

    def test_post_needs_token(self):
        self.assertError(self.req("POST", "/api/ping", token=False, json_body={}), 401)

    def test_bad_host_403_api_and_static(self):
        for h in ("evil.example.com", f"evil.example.com:{self.port}", "127.0.0.1", "127.0.0.1:1",
                  f"127.0.0.1.evil.com:{self.port}"):
            with self.subTest(host=h):
                self.assertError(self.req("GET", "/api/meta", host=h), 403, "bad_host")
                self.assertError(self.req("GET", "/", host=h, token=False), 403, "bad_host")
                self.assertError(self.req("GET", "/index.html", host=h, token=False), 403, "bad_host")

    def test_missing_host_403(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest("GET", "/api/meta", skip_host=True, skip_accept_encoding=True)
            c.putheader("X-DM-Token", self.token)
            c.endheaders()
            self.assertEqual(c.getresponse().status, 403)
        finally:
            c.close()

    def test_foreign_origin_403(self):
        for origin in ("http://evil.example.com", "https://evil.example.com", "null",
                       f"http://127.0.0.1:{self.port + 1}", f"https://127.0.0.1:{self.port}"):
            with self.subTest(origin=origin):
                self.assertError(self.req("GET", "/api/meta", headers={"Origin": origin}), 403, "bad_origin")

    def test_same_origin_accepted(self):
        st, _, _ = self.req("GET", "/api/meta", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 200)
        st, _, _ = self.req("GET", "/api/meta", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(st, 200)
        st, _, _ = self.req("GET", "/api/meta", headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(st, 200)

    def test_cross_site_fetch_403(self):
        for v in ("cross-site", "same-site"):
            with self.subTest(v=v):
                self.assertError(self.req("GET", "/api/meta", headers={"Sec-Fetch-Site": v}), 403, "bad_origin")

    def test_non_json_post_415(self):
        self.assertError(self.req("POST", "/api/ping", body=b"a=1",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}), 415)
        self.assertError(self.req("POST", "/api/ping", body=b"hello", headers={"Content-Type": "text/plain"}), 415)
        self.assertError(self.req("POST", "/api/ping", body=b"{}"), 415)  # no content-type

    def test_bad_json_400(self):
        self.assertError(self.req("POST", "/api/ping", body=b"{nope",
                                  headers={"Content-Type": "application/json"}), 400)
        self.assertError(self.req("POST", "/api/ping", body=b"[1,2]",
                                  headers={"Content-Type": "application/json"}), 400)

    def test_oversized_body_413(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest("POST", "/api/ping", skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", self.host)
            c.putheader("X-DM-Token", self.token)
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", str(config.MAX_BODY_BYTES + 1))
            c.endheaders()  # body never sent: the server must refuse from the header alone
            r = c.getresponse()
            self.assertEqual(r.status, 413)
            r.read()
        finally:
            c.close()

    def test_body_at_limit_is_read(self):
        pad = "a" * 1000
        st, _, body = self.req("POST", "/api/ping", json_body={"pad": pad})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])

    def test_unsupported_methods_405(self):
        for m in ("OPTIONS", "PUT", "DELETE", "PATCH"):
            with self.subTest(m=m):
                self.assertError(self.req(m, "/api/meta"), 405)
                self.assertEqual(self.req(m, "/index.html")[0], 405)

    def test_no_cors_headers_ever(self):
        _, hdrs, _ = self.req("GET", "/api/meta", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertFalse(any(k.startswith("access-control-") for k in hdrs))
        _, hdrs, _ = self.req("OPTIONS", "/api/meta", headers={"Origin": "http://evil.example.com"})
        self.assertFalse(any(k.startswith("access-control-") for k in hdrs))

    def test_wrong_method_and_unknown_route(self):
        self.assertError(self.req("POST", "/api/meta", json_body={}), 405)
        self.assertError(self.req("GET", "/api/ping"), 405)
        self.assertError(self.req("GET", "/api/nothing-here"), 404, "not_found")

    def test_post_static_405(self):
        self.assertEqual(self.req("POST", "/index.html", json_body={})[0], 405)


class SecurityHeaderTests(ServerCase):
    def check_headers(self, hdrs):
        csp = hdrs["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(hdrs["x-content-type-options"], "nosniff")
        self.assertEqual(hdrs["cache-control"], "no-store")
        self.assertEqual(hdrs["x-frame-options"], "DENY")
        self.assertEqual(hdrs["referrer-policy"], "no-referrer")

    def test_api_response_headers(self):
        self.check_headers(self.req("GET", "/api/meta")[1])

    def test_api_error_headers(self):
        self.check_headers(self.req("GET", "/api/meta", token=False)[1])
        self.check_headers(self.req("GET", "/api/zzz")[1])

    def test_static_headers(self):
        st, hdrs, body = self.req("GET", "/", token=False)
        self.assertEqual(st, 200)
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        self.assertIn(b"<", body)
        self.check_headers(hdrs)
        st, hdrs, _ = self.req("GET", "/js/app.js", token=False)
        self.assertEqual(st, 200)
        self.assertTrue(hdrs["content-type"].startswith("text/javascript"))
        self.check_headers(hdrs)

    def test_static_needs_no_token(self):
        self.assertEqual(self.req("GET", "/index.html", token=False)[0], 200)

    def test_server_does_not_leak_python_version(self):
        _, hdrs, _ = self.req("GET", "/api/meta")
        self.assertNotIn("Python", hdrs.get("server", ""))


class StaticTests(ServerCase):
    def test_path_traversal_404(self):
        paths = [
            "/../dm/safety.py", "/%2e%2e/run.py", "/web/../run.py", "/..%2fdm/safety.py", "/%2e%2e%2frun.py",
            "/css/../../run.py", "/css/..%5c..%5crun.py", "/..\\run.py", "/%2e%2e/%2e%2e/windows/win.ini",
            "/....//run.py", "//run.py", "/js//app.js", "/C:/Windows/win.ini", "/css/%2e%2e/%2e%2e/run.py",
            "/index.html%00.png", "/index.html::$DATA", "/%252e%252e/run.py",
        ]
        for p in paths:
            with self.subTest(path=p):
                st, _, body = self.req("GET", p, token=False)
                self.assertEqual(st, 404, p)
                self.assertNotIn(b"import ", body)
                self.assertNotIn(b"[fonts]", body)

    def test_non_web_files_not_served(self):
        for p in ("/dm/safety.py", "/run.py", "/data/journal.jsonl", "/CLAUDE.md", "/docs/API.md"):
            with self.subTest(path=p):
                self.assertEqual(self.req("GET", p, token=False)[0], 404)

    def test_extension_whitelist_and_symlink_escape(self):
        web = Path(self.path("web"))
        write(str(web / "index.html"), data=b"<html></html>")
        write(str(web / "ok.js"), data=b"1")
        write(str(web / "notes.txt"), data=b"secret")
        write(str(web / "evil.py"), data=b"import os")
        write(str(web / "run.bat"), data=b"@echo off")
        write(str(web / "noext"), data=b"x")
        write(self.path("outside.js"), data=b"outside")
        with mock.patch.object(config, "WEB_DIR", web):
            self.assertEqual(self.req("GET", "/", token=False)[0], 200)
            self.assertEqual(self.req("GET", "/ok.js", token=False)[0], 200)
            for p in ("/notes.txt", "/evil.py", "/run.bat", "/noext", "/missing.js", "/../outside.js",
                      "/%2e%2e/outside.js"):
                with self.subTest(path=p):
                    self.assertEqual(self.req("GET", p, token=False)[0], 404)

    def test_junction_escape_from_web_dir_404(self):
        web = Path(self.path("web"))
        write(str(web / "index.html"), data=b"x")
        write(self.path("outside", "secret.js"), data=b"top secret")
        self.junction(str(web / "esc"), self.path("outside"))
        with mock.patch.object(config, "WEB_DIR", web):
            st, _, body = self.req("GET", "/esc/secret.js", token=False)
        self.assertEqual(st, 404)
        self.assertNotIn(b"top secret", body)

    def test_head_has_no_body(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("HEAD", "/index.html", headers={"Host": self.host})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"")
        finally:
            c.close()


class ApiRouteTests(ServerCase):
    def test_shell_open_unknown_target_400(self):
        for t in ("calc.exe", "cmd", "", None, 5, "C:\\Windows\\System32\\cmd.exe", "../x"):
            with self.subTest(target=t):
                self.assertError(self.req("POST", "/api/shell/open", json_body={"target": t}), 400)
        self.assertError(self.req("POST", "/api/shell/open", json_body={}), 400)

    def test_recycle_without_scan_409(self):
        resp = self.req("POST", "/api/files/recycle", json_body={"paths": [self.path("a.txt")], "source": "explorer"})
        self.assertError(resp, 409, "no_scan")

    def test_scan_unc_path_400(self):
        for p in ("\\\\server\\share", "\\\\?\\C:\\Windows", "//server/share/x", "relative", "C:\\a:stream",
                  "C:\\Windows.\\", None, 12, ""):
            with self.subTest(path=p):
                self.assertError(self.req("POST", "/api/scan", json_body={"path": p}), 400)
        self.assertIsNone(self.state.scan)

    def test_scan_missing_folder_400(self):
        self.assertError(self.req("POST", "/api/scan", json_body={"path": self.path("nope")}), 400)

    def test_scan_junction_root_400(self):
        real = self.path("real")
        os.makedirs(real)
        self.junction(self.path("lnk"), real)
        self.assertError(self.req("POST", "/api/scan", json_body={"path": self.path("lnk")}), 400)

    def test_scan_queries_without_scan_409(self):
        for p in ("/api/scan/tree", "/api/scan/children", "/api/scan/largest", "/api/scan/types", "/api/scan/age"):
            with self.subTest(path=p):
                self.assertError(self.req("GET", p), 409, "no_scan")

    def test_clean_run_without_analysis_409(self):
        self.assertError(self.req("POST", "/api/clean/run", json_body={"selection": {"x": "all"}}), 409)

    def test_unknown_task_404(self):
        self.assertError(self.req("GET", "/api/tasks/deadbeef"), 404)
        self.assertError(self.req("POST", "/api/tasks/deadbeef/cancel", json_body={}), 404)

    def test_scan_end_to_end_and_recycle_with_fake_bin(self):
        """Real scan of a sandbox over HTTP, then recycle through the API with a fake bin."""
        import time
        from dm import winapi
        from .common import FakeRecycle
        root = self.path("proj")
        big = write(os.path.join(root, "big.bin"), config.KEEP_FILE_MIN + 5)
        write(os.path.join(root, "x", "small.txt"), 10)
        fake = FakeRecycle(self.sb)

        def wait(task_id):
            for _ in range(200):
                st, _, t = self.req("GET", f"/api/tasks/{task_id}")
                if t["status"] != "running":
                    return t
                time.sleep(0.05)
            self.fail("task did not finish")

        with mock.patch.object(winapi, "send_to_recycle_bin", fake):
            st, _, task = self.req("POST", "/api/scan", json_body={"path": root})
            self.assertEqual(st, 200, task)
            t = wait(task["id"])
            self.assertEqual(t["status"], "done", t)
            self.assertEqual(t["result"]["files"], 2)
            # let the background _persist_scan thread (writes into the sandbox) finish
            from dm import history
            for _ in range(100):
                if os.path.exists(history.SNAPSHOTS):
                    break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(history.SNAPSHOTS))
            st, _, kids = self.req("GET", "/api/scan/children?path=" + root.replace("\\", "%5C"))
            self.assertEqual(st, 200)
            self.assertTrue(any(i["name"] == "big.bin" for i in kids["items"]))
            # a path outside the scan is refused, the in-scan one is recycled
            st, _, task = self.req("POST", "/api/files/recycle",
                                   json_body={"paths": [big, self.path("elsewhere.txt")], "source": "explorer"})
            self.assertEqual(st, 200, task)
            t = wait(task["id"])
            self.assertEqual(t["status"], "done", t)
            self.assertEqual(t["result"]["done"], [big])
            self.assertEqual(len(t["result"]["refused"]), 1)
            self.assertFalse(os.path.exists(big))
            self.assertEqual(self.state.scan.root.file_count, 1)
            self.assertEqual(fake.calls, [[big]])


if __name__ == "__main__":
    unittest.main()
