"""模型命令（agent_models.py）对核心的约定：退出码分清原因、key 不出现在输出里、登录会话号拆回上游的服务商和会话。

命令按核心的方式跑（新进程，标准输入一行 JSON）；控制台换成回环上的假服务，服务商目录用镜像里真实的上游模块。
最后一项对着上游控制台的路由表，确认命令调的接口都还在：升级 Hermes 时这里先红。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "agent_models.py"
TOKEN = "test-dashboard-token"
KEY = "sk-or-v1-secret-test-value-0000"
OAUTH = {"providers": [{"id": "nous", "name": "Nous Portal", "flow": "device_code", "status": {"logged_in": False}}]}


class FakeDashboard:
    """按 (方法, 路径) 回预设的 (状态码, 内容)；没口令的请求回 401，和上游一样。"""

    def __init__(self):
        self.routes, self.seen = {}, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.seen.append((self.command, self.path, body))
                if self.headers.get("X-Hermes-Session-Token") != TOKEN:
                    status, payload = 401, {"detail": "Unauthorized"}
                else:
                    status, payload = fake.routes.get((self.command, self.path), (404, {"detail": "Not Found"}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_PUT = do_POST = do_DELETE = answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class AgentModelsCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        token = Path(self.home.name) / "token"
        token.write_text(TOKEN)
        self.dashboard = FakeDashboard()
        self.addCleanup(self.dashboard.close)
        self.dashboard.routes[("GET", "/api/providers/oauth")] = (200, OAUTH)
        self.env = {**os.environ, "HERMES_HOME": self.home.name, "HERMES_DASHBOARD_HOST": "127.0.0.1",
                    "HERMES_DASHBOARD_PORT": str(self.dashboard.port),
                    "HERMES_DASHBOARD_SESSION_TOKEN": f"file:{token}"}

    def run_op(self, request, env=None):
        line = request if isinstance(request, str) else json.dumps(request)
        done = subprocess.run([sys.executable, str(SCRIPT)], input=line + "\n", capture_output=True, text=True,
                              env=env or self.env, timeout=60)
        stdout = json.loads(done.stdout) if done.stdout.strip() else None
        return done.returncode, stdout, done.stdout + done.stderr

    def test_the_key_never_reaches_the_output(self):
        # 上游把 key 原样带回来（探针的说明、报错的正文）时，命令写出去之前也要抹掉。
        validate = ("POST", "/api/providers/validate")
        self.dashboard.routes[validate] = (200, {"ok": False, "reachable": True, "message": f"bad key {KEY}"})
        code, stdout, raw = self.run_op({"op": "save_key", "provider": "openrouter", "key": KEY})
        self.assertEqual((code, stdout), (0, {"check": "rejected"}))
        self.assertNotIn(KEY, raw)
        self.assertNotIn(("PUT", "/api/env"), [(method, path) for method, path, _ in self.dashboard.seen])

        self.dashboard.routes[validate] = (500, {"detail": f"probe crashed on {KEY}"})
        code, stdout, raw = self.run_op({"op": "save_key", "provider": "openrouter", "key": KEY})
        self.assertEqual((code, stdout), (1, None))
        self.assertNotIn(KEY, raw)

    def test_a_login_session_finds_its_provider_again(self):
        self.dashboard.routes[("POST", "/api/providers/oauth/nous/start")] = (200, {
            "session_id": "s1", "user_code": "ABCD-EFGH", "verification_url": "https://portal.example.com/device",
            "expires_in": 600, "poll_interval": 5})
        code, started, _ = self.run_op({"op": "login_start", "provider": "nous"})
        self.assertEqual(code, 0)
        self.assertEqual((started["url"], started["code"]), ("https://portal.example.com/device", "ABCD-EFGH"))

        self.dashboard.routes[("GET", "/api/providers/oauth/nous/poll/s1")] = (200, {"status": "approved"})
        code, polled, _ = self.run_op({"op": "login_poll", "session": started["session"]})
        self.assertEqual((code, polled["state"]), (0, "approved"))

        self.dashboard.routes[("DELETE", "/api/providers/oauth/sessions/s1")] = (200, {"ok": True})
        self.assertEqual(self.run_op({"op": "login_cancel", "session": started["session"]})[0], 0)

        # 控制台重启过、会话没了：找不到，不是上游出错。
        del self.dashboard.routes[("GET", "/api/providers/oauth/nous/poll/s1")]
        self.assertEqual(self.run_op({"op": "login_poll", "session": started["session"]})[0], 3)

    def test_exit_codes_tell_the_causes_apart(self):
        self.assertEqual(self.run_op("not json")[0], 2)
        self.assertEqual(self.run_op({"op": "format_disk"})[0], 2)
        code, stdout, _ = self.run_op({"op": "save_key", "provider": "openrouter", "key": "two words"})
        self.assertEqual((code, stdout["field"]), (2, "key"))
        self.assertEqual(self.run_op({"op": "save_key", "provider": "no-such-provider", "key": KEY})[0], 3)

        self.dashboard.close()
        self.assertEqual(self.run_op({"op": "catalog"})[0], 4)
        self.assertEqual(self.run_op({"op": "catalog"}, {**self.env, "HERMES_DASHBOARD_SESSION_TOKEN": ""})[0], 4)

    def test_the_dashboard_still_has_the_routes_we_call(self):
        from hermes_cli.web_server import app

        routes = {(method, route.path) for route in app.routes for method in getattr(route, "methods", None) or ()}
        called = {
            ("GET", "/api/env"), ("PUT", "/api/env"), ("DELETE", "/api/env"), ("POST", "/api/providers/validate"),
            ("GET", "/api/providers/oauth"), ("POST", "/api/providers/oauth/{provider_id}/start"),
            ("GET", "/api/providers/oauth/{provider_id}/poll/{session_id}"),
            ("DELETE", "/api/providers/oauth/sessions/{session_id}"), ("DELETE", "/api/providers/oauth/{provider_id}"),
            ("GET", "/api/providers/custom-endpoints"), ("POST", "/api/providers/custom-endpoints"),
            ("POST", "/api/providers/custom-endpoints/validate"),
            ("DELETE", "/api/providers/custom-endpoints/{endpoint_id}"),
            ("GET", "/api/model/options"), ("GET", "/api/model/recommended-default"), ("POST", "/api/model/set"),
        }
        self.assertEqual(called - routes, set())


if __name__ == "__main__":
    unittest.main()
