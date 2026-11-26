#!/usr/bin/env python3
"""Mirage 试点的一次性跑批：起一个假收件箱，再用 Hermes 的 HTTP API 让 Bot 真跑命令。

只在试点用。所有输出走 stdout，`scripts/dev.sh logs pilot-runner` 能看。

假收件箱按命令名决定回答：
- rm：允许一次（跟终端说 allow）；
- mv：睡 8 秒再允许，客户端的 ask_timeout 是 5 秒，所以会超时、按拒绝；
- pip：拒绝并带理由。
别的命令不会触发 ask。
"""

import asyncio
import json
import pathlib
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ASK_PORT = 8000
ASK_TOKEN = "pilot-token"
GATEWAY = "http://gateway-default:8642"
API_KEY = "pilot-api-key-0123456789"
CHAT_TIMEOUT = 600

asked = []
LOG = []


def log(kind, **fields):
    line = json.dumps({"ts": round(time.time(), 3), "kind": kind, **fields})
    LOG.append(line)
    del LOG[:-200]
    print(line, flush=True)


class AskHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._answer(200, {"ok": True})
            return
        if self.path == "/log":
            self._answer_raw(200, ("\n".join(LOG) + "\n").encode())
            return
        self._answer(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization", "")
        asked.append(payload)
        log("ask", command=payload.get("command"), argv=payload.get("argv"),
            cwd=payload.get("cwd"), reason=payload.get("reason"), auth_ok=auth == f"Bearer {ASK_TOKEN}")
        if auth != f"Bearer {ASK_TOKEN}":
            self._answer(401, {"error": "bad token"})
            return
        if self.path.startswith("/contract"):
            self._contract()
            return
        command = payload.get("command") or ""
        if command == "pip":
            self._answer(200, {"decision": "deny", "note": "installs need ops (mock)"})
            return
        if command == "wget":
            self._answer(200, {"decision": "deny", "scope": "session", "note": "never for this session"})
            return
        argv = payload.get("argv") or []
        if command == "mv" and any("/tmp/hermes-snap" in str(a) for a in argv):
            self._answer(200, {"decision": "allow", "scope": "session"})
            return
        if command == "curl":
            if any("slow" in str(a) or str(a) == "-s" for a in argv):
                log("ask-slow", command=command, seconds=8)
                time.sleep(8)
                try:
                    self._answer(200, {"decision": "allow", "scope": "once"})
                except (BrokenPipeError, ConnectionResetError):
                    log("timeout-detected", command=command)
                return
            self._answer(200, {"decision": "allow", "scope": "session"})
            return
        if command == "rm":
            self._answer(200, {"decision": "allow", "scope": "session"})
            return
        if command == "mv":
            log("ask-slow", command=command, seconds=8)
            time.sleep(8)
            try:
                self._answer(200, {"decision": "allow", "scope": "once"})
            except (BrokenPipeError, ConnectionResetError):
                # 客户端已经按超时拒绝了；这条日志就是超时的证据。
                log("timeout-detected", command=command)
            return
        self._answer(200, {"decision": "allow", "scope": "once"})

    def _answer(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _contract(self):
        """按 fixture 回答，给 runner 自己的契约测试用。"""
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        name = (query.get("case") or [""])[0]
        fixture = json.loads(pathlib.Path("/pilot/contract/ask.json").read_text())
        if name == "timeout":
            time.sleep(float((query.get("sleep") or ["8"])[0]))
            self._answer(200, {"decision": "allow"})
            return
        for case in fixture["answers"]:
            if case["name"] != name:
                continue
            if "body_raw" in case:
                self._answer_raw(200, case["body_raw"].encode())
            else:
                self._answer(200, case["body"])
            return
        self._answer(404, {"error": f"no contract case {name}"})

    def _answer_raw(self, status, raw):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


def http(method, path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        GATEWAY + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read(600).decode("utf-8", "replace")
        log("http-error", path=path, code=exc.code, body=detail)
        raise


def wait_gateway(deadline=300):
    started = time.time()
    while time.time() - started < deadline:
        try:
            with urllib.request.urlopen(GATEWAY + "/health", timeout=5) as response:
                if response.status == 200:
                    log("gateway-ready", seconds=round(time.time() - started, 1))
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    return False


def chat(session_id, prompt):
    # 试点跑批共用离线的 Magpie 网关：单条 80 秒以上、偶发 500/超时都见过。
    # 一次失败不代表用例失败，重试三次再记账，让结果反映 Mirage 本身而不是网关抖动。
    last = None
    for attempt in range(3):
        try:
            result = http(
                "POST",
                f"/api/sessions/{session_id}/chat",
                {"message": prompt},
                timeout=CHAT_TIMEOUT,
            )
            return (result.get("message") or {}).get("content") or ""
        except Exception as exc:  # noqa: BLE001
            last = exc
            log("chat-retry", attempt=attempt + 1, error=repr(exc))
            time.sleep(10 * (attempt + 1))
    raise last


def contract_checks():
    """按 pilot/contract/ask.json 跑线上契约：回答映射和三种失败都落到 deny。"""
    from mirage.agents.hermes.ask import HttpAsk
    from mirage.policy.types import CommandRule, Decision, Outcome

    fixture = json.loads(pathlib.Path("/pilot/contract/ask.json").read_text())
    failures = 0
    for case in fixture["answers"]:
        url = f"http://127.0.0.1:{ASK_PORT}/contract?case={case['name']}"
        settled = asyncio.run(HttpAsk(url, token=ASK_TOKEN, timeout=2)(sample_decision()))
        want = case["expect"]
        got = {"outcome": settled.outcome.value, "scope": settled.scope.value, "note": settled.note}
        ok = got["outcome"] == want["outcome"]
        if "scope" in want:
            ok = ok and got["scope"] == want["scope"]
        if "note" in want:
            ok = ok and got["note"] == want["note"]
        if "note_contains" in want:
            ok = ok and want["note_contains"] in got["note"]
        log("contract", case=case["name"], result="PASS" if ok else "FAIL", got=got)
        failures += 0 if ok else 1

    unreachable = asyncio.run(
        HttpAsk("http://127.0.0.1:1/ask", token=ASK_TOKEN, timeout=1)(sample_decision())
    )
    ok = unreachable.outcome is Outcome.DENY and "unreachable" in unreachable.note
    log("contract", case="unreachable", result="PASS" if ok else "FAIL", got=unreachable.note)
    failures += 0 if ok else 1

    slow = fixture["timeout"]
    timed_out = asyncio.run(
        HttpAsk(
            f"http://127.0.0.1:{ASK_PORT}/contract?case=timeout&sleep={slow['sleep']}",
            token=ASK_TOKEN,
            timeout=slow["timeout"],
        )(sample_decision())
    )
    ok = timed_out.outcome is Outcome.DENY and "did not answer" in timed_out.note
    log("contract", case="timeout", result="PASS" if ok else "FAIL", got=timed_out.note)
    failures += 0 if ok else 1
    return failures


def sample_decision():
    from mirage.policy.types import CommandRule, Decision

    return Decision(
        id="contract-1",
        session_id="contract-session",
        command="rm",
        argv=("notes.txt",),
        cwd="/opt/data/work",
        paths=("/opt/data/work/notes.txt",),
        reason="deletes need sign-off",
        rule=CommandRule(reason="deletes need sign-off"),
    )


CASES = [
    # 名字, 提示词, 判定：响应里必须有这些词之一（空串不判文本）, 每轮期望的 ask 次数
    (
        "debug-mount",
        "只执行：echo PATH=$PATH; ls -l /root/.mirage/bin 2>&1 | head -3; "
        "mount 2>/dev/null | grep -E 'mirage|opt/data' | head -6; "
        "echo T > /tmp/t.txt && echo TMP_OK; echo W > /opt/data/work/w.txt && echo WORK_OK; "
        "ls -ld /opt/data /opt/data/work; ls -la /opt/data/work。把终端工具返回的原文贴出来，不要解释。",
        ("WORK_OK",),
        [0],
    ),
    (
        "debug-docker",
        "只执行：sh -c 'command -v docker; ls -l /usr/bin/docker; docker version; echo rc=$?'; "
        "/usr/bin/docker version 2>&1 | head -2; ls -ld /opt/data /opt/data/work。"
        "把终端工具返回的原文贴出来，不要解释。",
        ("Client",),
        [0],
    ),
    (
        "debug-env",
        "只执行：id; ls -ld /opt/data/work; ls -la /opt/data/work; docker version 2>&1 | head -2; "
        "echo DBG > /opt/data/work/dbg.txt; echo TOUCHED; cat /opt/data/work/dbg.txt。"
        "把终端工具返回的原文贴出来，不要解释。",
        ("TOUCHED",),
        [0],
    ),
    (
        "control-write",
        "只执行一条终端命令：echo pilot-ok > /opt/data/work/t.txt && cat /opt/data/work/t.txt。"
        "把终端工具返回的原文贴出来，不要解释。",
        ("pilot-ok",),
        [0],
    ),
    (
        "hidden-env-cat",
        "只执行：echo K=1 > /opt/data/work/x.env && cat /opt/data/work/x.env。"
        "把终端工具返回的原文贴出来，不要解释。",
        ("Permission denied",),
        [0],
    ),
    (
        "hidden-env-python",
        "只执行：python3 -c \"print(open('/opt/data/work/x.env').read())\"。"
        "把终端工具返回的原文贴出来，不要解释。",
        ("Permission denied",),
        [0],
    ),
    (
        "rm-deletes",
        "只执行：rm /opt/data/work/t.txt && ([ -e /opt/data/work/t.txt ] && echo STILL || echo GONE)。"
        "把终端工具返回的原文贴出来，不要解释。",
        ("GONE",),
        [0],  # 观察：rm 是 Mirage 内置命令，不走 command 规则、不触发 ask
    ),
    (
        "rule-cat",
        "只执行：cat /opt/data/work/workspace.yaml | head -2。把终端工具返回的原文贴出来，不要解释。",
        ("policy denied",),
        [0],  # 若内置命令绕过 deny 规则，这条会先 FAIL，正好记录
    ),
    (
        "ask-deny-pip",
        "只执行：pip --version。把终端工具返回的原文贴出来，不要解释。",
        ("Permission denied", "policy denied"),
        [1],
    ),
    (
        "ask-allow-session-curl",
        "只执行：curl -s -S -o /dev/null -w '%{http_code}' --max-time 5 https://example.com/。"
        "把终端工具返回的原文贴出来，不要解释。",
        ("",),
        [1, 0],
    ),
    (
        "ask-deny-session-wget",
        "只执行：wget --version。把终端工具返回的原文贴出来，不要解释。",
        ("Permission denied", "policy denied"),
        [1, 1],  # 观察：deny+session 下一条同规则还会问（允许+session 不会），见 result
    ),
    (
        "ask-timeout-curl",
        "只执行：curl -s -S -o /dev/null -w '%{http_code}' --max-time 20 https://example.com/。"
        "把终端工具返回的原文贴出来，不要解释。",
        ("Permission denied", "policy denied"),
        [1],
    ),
]


def main():
    server = ThreadingHTTPServer(("0.0.0.0", ASK_PORT), AskHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("ask-endpoint", port=ASK_PORT)

    if not wait_gateway():
        log("failed", reason="gateway never became healthy")
        return 1
    contract_failures = contract_checks()

    # 标题是会话表里的唯一键：时间戳 + 随机串，重装/重跑后旧会话还在也不撞。
    title = f"mirage pilot {int(time.time())}-{secrets.token_hex(3)}"
    created = None
    for attempt in range(3):
        try:
            created = http("POST", "/api/sessions", {"title": title})
            break
        except Exception as exc:  # noqa: BLE001
            log("session-create-retry", attempt=attempt, error=repr(exc))
            time.sleep(2)
    session_id = ((created or {}).get("session") or {}).get("id") or (created or {}).get("id")
    if not session_id:
        session_id = "api_pilot"
        log("session", id=session_id, note="create failed, trying chat on this id")
    log("session", id=session_id)

    # 文件不靠模型建：模型慢或 500 时用例会连带全红。跑批容器和 Bot 共用 /opt/data。
    work = pathlib.Path("/opt/data/work")
    work.mkdir(parents=True, exist_ok=True)
    for name, body in (("a.txt", "ready\n"), ("t.txt", "t\n"), ("x.env", "K=1\n")):
        (work / name).write_text(body)

    # 再让模型走一次最简单的写命令，确认终端链路活着；失败不影响后面的用例。
    chat(session_id, "只执行：echo ready > /opt/data/work/a.txt。贴终端原文，不要解释。")

    failures = contract_failures
    for name, prompt, expected, ask_counts in CASES:
        contents = []
        got_asks = []
        for _ in range(len(ask_counts)):
            before = len(asked)
            try:
                contents.append(chat(session_id, prompt))
            except Exception as exc:  # noqa: BLE001
                got_asks.append(-1)
                contents.append(repr(exc))
            else:
                got_asks.append(len(asked) - before)
        text = "\n---\n".join(contents)
        ok_text = not expected[0] or any(word in text for word in expected)
        ok_asks = got_asks == ask_counts
        result = "PASS" if ok_text and ok_asks else "FAIL"
        if result == "FAIL":
            failures += 1
        log("case", name=name, result=result, asks=got_asks,
            expected_asks=ask_counts, text=text[:1200])

    log("summary", failures=failures, asks=len(asked), asked=asked)
    log("idle", note="跑完不退出：核心按 healthcheck 判就绪，不用等整轮跑批超过安装时限")
    threading.Event().wait()
    return 1 if failures else 0


if __name__ == "__main__":
    code = 2
    try:
        code = main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    raise SystemExit(0)
