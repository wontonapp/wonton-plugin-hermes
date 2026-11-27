"""清单 agent.models 的命令：App 经核心在这个 Bot 的 gateway 容器里执行，标准输入一行 JSON，标准输出一行 JSON
（插件规范“模型接入”）。

每一步都交给 Hermes 自己：改东西、登录走同一容器里控制台的 HTTP 接口（127.0.0.1，口令和控制台读同一个文件，
HERMES_DASHBOARD_SESSION_TOKEN=file:…）；服务商目录和测试用上游的 Python 模块，只读不改。不另存 key、不另记服务商名单：
上游加了服务商，catalog 跟着带出来。设备码登录的会话在控制台进程里，由它的后台线程轮询服务商、保存凭据；命令每次都是
新进程，不留状态。每个 Bot 一组容器、一份数据目录，所以不传 ?profile=，用控制台的默认 profile。

key 只从标准输入进来：不写日志，不出现在输出和标准错误里（出错时也先抹掉再写）。
退出码：0 做完了（拒了 key、登录没成、测试不通也是 0，结果在字段里）；2 输入不合法；3 不认识的服务商或登录会话；
4 控制台还没起来；其余是上游出错，原因写在标准错误。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

EXIT_INVALID, EXIT_UNKNOWN, EXIT_NOT_READY, EXIT_UPSTREAM = 2, 3, 4, 1
# 核心等 30 秒（test 60 秒），这里留出余量，免得核心先超时、看不到原因。
HTTP_TIMEOUT = 25
TEST_TIMEOUT = 45

# 推荐的几家（排前面，其余进“更多”），按这个顺序。挑法：国内能直接买到、或一个账号就能用很多模型的；订阅登录
# 里 ChatGPT 用的人最多。都要是上游目录里有的，没有的跳过。
RECOMMENDED = ("deepseek", "openrouter", "openai-codex", "zai", "anthropic", "gemini")
# key 的固定开头，App 据此认出剪贴板里刚复制的 key。只写服务商文档里固定的。
KEY_PREFIXES = {
    "openrouter": "sk-or-", "anthropic": "sk-ant-", "openai-api": "sk-", "deepseek": "sk-",
    "gemini": "AIza", "xai": "xai-", "huggingface": "hf_", "nvidia": "nvapi-",
}
# 上游默认地址在容器里用不了、必须填的（本机桌面服务、按部署分配的地址）。
BASE_URL_REQUIRED = {"lmstudio", "azure-foundry"}
# 新建 OpenAI 兼容服务时的服务商 ID；已建的按控制台给的 ID 列成 custom:<id>（和 /api/model/options 的写法一样）。
CUSTOM = "custom"

TEXT = {
    "custom": {"en": "OpenAI-compatible service", "zh-Hans": "OpenAI 兼容服务", "zh-Hant": "OpenAI 相容服務",
               "ja": "OpenAI 互換サービス"},
    "key_missing": {"en": "Enter the key.", "zh-Hans": "填上 key。", "zh-Hant": "填上金鑰。", "ja": "キーを入力してください。"},
    "key_shape": {"en": "A key is one line without spaces.", "zh-Hans": "key 是一行，不带空格。",
                  "zh-Hant": "金鑰是一行，不含空格。", "ja": "キーは空白を含まない 1 行です。"},
    "no_key": {"en": "This provider doesn't take a key here.", "zh-Hans": "这家不用填 key。",
               "zh-Hant": "這家不用填金鑰。", "ja": "このサービスにはキーを入力しません。"},
    "no_login": {"en": "This provider has no sign-in here.", "zh-Hans": "这家不能在这里登录。",
                 "zh-Hant": "這家不能在這裡登入。", "ja": "このサービスはここではログインできません。"},
    "base_url": {"en": "Enter an address starting with http:// or https://.",
                 "zh-Hans": "填一个 http:// 或 https:// 开头的地址。",
                 "zh-Hant": "填一個 http:// 或 https:// 開頭的位址。",
                 "ja": "http:// か https:// で始まるアドレスを入力してください。"},
    "unreachable": {"en": "Couldn't reach this address.", "zh-Hans": "连不上这个地址。",
                    "zh-Hant": "連不上這個位址。", "ja": "このアドレスに接続できません。"},
    "no_models": {"en": "This service lists no models.", "zh-Hans": "这个服务没有列出模型。",
                  "zh-Hant": "這個服務沒有列出模型。", "ja": "このサービスにはモデルがありません。"},
    "not_connected": {"en": "Connect this provider first.", "zh-Hans": "先接好这家。",
                      "zh-Hant": "先接好這家。", "ja": "先にこのサービスを接続してください。"},
    "unknown_model": {"en": "This provider doesn't offer that model.", "zh-Hans": "这家没有这个模型。",
                      "zh-Hant": "這家沒有這個模型。", "ja": "このサービスにそのモデルはありません。"},
    "confirm": {"en": "Use this model anyway?", "zh-Hans": "这个模型有下面的提醒，还要用它吗？",
                "zh-Hant": "這個模型有下面的提醒，還要用它嗎？", "ja": "次の注意があります。このモデルを使いますか？"},
    "fallback": {"en": "The model didn't answer; a fallback did.", "zh-Hans": "选的模型没回，是备用的回的。",
                 "zh-Hant": "選的模型沒回，是備用的回的。", "ja": "選んだモデルは応答せず、予備が応答しました。"},
}


class Exit(Exception):
    """结束这次命令：退出码，标准输出（JSON）或标准错误（一句原因）。"""

    def __init__(self, code, stdout=None, stderr=""):
        super().__init__(stderr)
        self.code, self.stdout, self.stderr = code, stdout, stderr


def pick_language(languages):
    for tag in languages if isinstance(languages, list) else []:
        tag = str(tag).lower()
        if tag.startswith("zh"):
            hant = any(part in tag for part in ("hant", "-tw", "-hk", "-mo"))
            return "zh-Hant" if hant else "zh-Hans"
        if tag.startswith("ja"):
            return "ja"
        if tag.startswith("en"):
            return "en"
    return "en"


class Command:
    def __init__(self, request):
        self.request = request
        self.lang = pick_language(request.get("languages"))
        self._catalog = None

    def text(self, key):
        return TEXT[key][self.lang]

    def invalid(self, field, key):
        return Exit(EXIT_INVALID, {"field": field, "message": self.text(key)})

    def field(self, name):
        value = self.request.get(name)
        if not isinstance(value, str) or not value.strip():
            raise Exit(EXIT_INVALID, {"field": name, "message": f"{name}?"})
        return value.strip()

    # ---- 控制台 ----

    def api(self, method, path, body=None, allow=()):
        """调控制台；allow 里的状态码照常返回 (状态码, 内容)，其余按退出码约定结束。"""
        token = dashboard_token()
        host = os.environ.get("HERMES_DASHBOARD_HOST", "127.0.0.1")
        port = os.environ.get("HERMES_DASHBOARD_PORT", "9119")
        headers = {"Accept": "application/json", "X-Hermes-Session-Token": token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://{host}:{port}{path}", data=data, method=method, headers=headers)
        # 容器可能带着出网代理变量；回环不能交给代理。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=HTTP_TIMEOUT) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            detail = error.read()[:500].decode("utf-8", "replace")
            if error.code in allow:
                return error.code, parse_json(detail)
            if error.code == 503:
                raise Exit(EXIT_NOT_READY, stderr=f"控制台要重启：{method} {path} {detail}") from None
            raise Exit(EXIT_UPSTREAM, stderr=f"控制台 {method} {path} 回 HTTP {error.code}：{detail}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            raise Exit(EXIT_NOT_READY, stderr=f"连不上控制台（{host}:{port}）：{error}") from None

    # ---- 目录 ----

    def catalog_rows(self):
        """[(服务商 ID, 名字, 认证方式, 环境变量)]：上游目录的顺序；推荐的另行排前。"""
        if self._catalog is None:
            from hermes_cli.provider_catalog import provider_catalog

            _, oauth = self.api("GET", "/api/providers/oauth")
            devices = {row["id"]: row for row in oauth.get("providers", []) if row.get("flow") == "device_code"}
            rows = []
            for entry in provider_catalog():
                keyed = entry.tab == "keys" and bool(entry.api_key_env_vars)
                login = devices.pop(entry.slug, None)
                if not keyed and login is None:
                    continue  # 虚拟（moa）、要云账号凭据（bedrock、vertex）、只能在终端登录的
                rows.append((entry.slug, entry.label, keyed, login, entry))
            rows.extend((row["id"], row["name"], False, row, None) for row in devices.values())
            self._catalog = rows
        return self._catalog

    def provider(self, provider_id):
        for row in self.catalog_rows():
            if row[0] == provider_id:
                return row
        raise Exit(EXIT_UNKNOWN, stderr=f"不认识的服务商：{provider_id}")

    def custom_endpoints(self):
        _, body = self.api("GET", "/api/providers/custom-endpoints")
        return {row["id"]: row for row in body.get("endpoints", []) if isinstance(row, dict) and row.get("id")}

    def op_catalog(self):
        _, env = self.api("GET", "/api/env")
        listed = []
        for slug, name, keyed, login, entry in self.catalog_rows():
            auth, connected, account = [], False, None
            if keyed:
                key_vars = list(entry.api_key_env_vars)
                url = entry.signup_url if entry.signup_url.startswith("https://") else None
                base_url = {"required": True} if slug in BASE_URL_REQUIRED else None
                auth.append({"type": "api_key", "key_url": url, "key_prefix": KEY_PREFIXES.get(slug),
                             "base_url": base_url})
                for var in key_vars:
                    row = env.get(var) or {}
                    if row.get("is_set"):
                        connected, account = True, redacted(row.get("redacted_value"))
                        break
            if login is not None:
                auth.append({"type": "device_login"})
                connected = connected or bool((login.get("status") or {}).get("logged_in"))
            listed.append({"id": slug, "name": name, "recommended": slug in RECOMMENDED, "auth": auth,
                           "connected": connected, "account": account})
        for endpoint in self.custom_endpoints().values():
            listed.append({
                "id": f"{CUSTOM}:{endpoint['id']}", "name": endpoint.get("name") or endpoint["id"],
                "recommended": False, "connected": True, "account": key_preview(endpoint, env),
                "auth": [{"type": "api_key", "base_url": {"required": True, "default": endpoint.get("base_url")}}],
            })
        listed.append({"id": CUSTOM, "name": self.text("custom"), "recommended": False, "connected": False,
                       "auth": [{"type": "api_key", "base_url": {"required": True}}]})
        rank = {slug: index for index, slug in enumerate(RECOMMENDED)}
        listed.sort(key=lambda row: (rank.get(row["id"], len(rank)),))
        return {"providers": listed, "current": current_model()}

    # ---- key ----

    def key(self):
        key = self.request.get("key")
        if not isinstance(key, str) or not key.strip():
            raise self.invalid("key", "key_missing")
        key = key.strip()
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key):
            raise self.invalid("key", "key_shape")
        return key

    def base_url(self, required, default=None):
        value = self.request.get("base_url") or default
        if not value:
            if required:
                raise self.invalid("base_url", "base_url")
            return None
        parsed = urllib.parse.urlparse(str(value).strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise self.invalid("base_url", "base_url")
        return str(value).strip().rstrip("/")

    def op_save_key(self):
        provider_id = self.field("provider")
        if provider_id == CUSTOM or provider_id.startswith(f"{CUSTOM}:"):
            return self.save_custom(provider_id)
        slug, _, keyed, _, entry = self.provider(provider_id)
        if not keyed:
            raise self.invalid("provider", "no_key")
        key = self.key()
        base_url = self.base_url(slug in BASE_URL_REQUIRED)
        var = entry.api_key_env_vars[0]
        _, probe = self.api("POST", "/api/providers/validate", {"key": var, "value": key})
        if probe.get("reachable") and not probe.get("ok"):
            return {"check": "rejected"}
        if base_url and entry.base_url_env_var:
            self.api("PUT", "/api/env", {"key": entry.base_url_env_var, "value": base_url})
        self.api("PUT", "/api/env", {"key": var, "value": key, "provider_setup": True})
        # 上游只有 OpenRouter、OpenAI、xAI、Gemini 有探针，其余回 reachable=false：照存，能不能用看测试。
        return {"check": "accepted" if probe.get("ok") and probe.get("reachable") else "unverified"}

    def save_custom(self, provider_id):
        endpoint_id = provider_id.partition(":")[2]
        existing = None
        if endpoint_id:
            existing = self.custom_endpoints().get(endpoint_id)
            if existing is None:
                raise Exit(EXIT_UNKNOWN, stderr=f"没有这个 OpenAI 兼容服务：{endpoint_id}")
        key = self.key()
        base_url = self.base_url(True, existing and existing.get("base_url"))
        _, probe = self.api("POST", "/api/providers/custom-endpoints/validate",
                            {"name": "probe", "base_url": base_url, "model": "", "api_key": key})
        if not probe.get("reachable"):
            raise self.invalid("base_url", "unreachable")
        if not probe.get("ok"):
            return {"check": "rejected"}
        models = [str(model) for model in probe.get("models") or [] if str(model).strip()]
        if not models:
            raise self.invalid("base_url", "no_models")
        name = (existing or {}).get("name") or urllib.parse.urlparse(base_url).netloc
        model = (existing or {}).get("model") if (existing or {}).get("model") in models else models[0]
        self.api("POST", "/api/providers/custom-endpoints", {
            "id": endpoint_id, "name": name, "base_url": base_url, "model": model, "api_key": key,
            "models": models, "discover_models": True,
        })
        return {"check": "accepted"}

    def op_disconnect(self):
        provider_id = self.field("provider")
        if provider_id.startswith(f"{CUSTOM}:"):
            status, _ = self.api("DELETE", f"/api/providers/custom-endpoints/{quote(provider_id[7:])}", allow=(404,))
            if status == 404:
                raise Exit(EXIT_UNKNOWN, stderr=f"没有这个 OpenAI 兼容服务：{provider_id}")
            return {}
        slug, _, keyed, login, entry = self.provider(provider_id)
        if keyed:
            # 404：本来就没填。别的服务商共用同一个变量时一起断开，和控制台 Keys 页删 key 一样。
            for var in entry.api_key_env_vars:
                self.api("DELETE", "/api/env", {"key": var}, allow=(404,))
        if login is not None:
            # 409：本来就没登录。
            self.api("DELETE", f"/api/providers/oauth/{quote(slug)}", allow=(409,))
        return {}

    # ---- 登录 ----

    def op_login_start(self):
        slug, _, _, login, _ = self.provider(self.field("provider"))
        if login is None:
            raise self.invalid("provider", "no_login")
        status, body = self.api("POST", f"/api/providers/oauth/{quote(slug)}/start", allow=(400,))
        if status == 400:
            raise Exit(EXIT_UPSTREAM, stderr=f"控制台拒绝开始登录：{body}")
        url = str(body.get("verification_url") or "")
        if not url.startswith("https://"):
            raise Exit(EXIT_UPSTREAM, stderr=f"登录网址不是 https：{url!r}")
        # 上游轮询要带服务商，接口只有会话号：拼成一个串给出去，轮询和取消时再拆开。
        return {"session": f"{slug}:{body['session_id']}", "url": url, "code": body.get("user_code") or None,
                "expires_in": int(body.get("expires_in") or 900), "interval": int(body.get("poll_interval") or 5)}

    def session(self):
        # 服务商 ID 里没有冒号，会话号是上游给的，按第一个冒号拆。
        provider, _, session = self.field("session").partition(":")
        if not provider or not session:
            raise Exit(EXIT_UNKNOWN, stderr="不认识的登录会话")
        return provider, session

    def op_login_poll(self):
        provider, session = self.session()
        status, body = self.api("GET", f"/api/providers/oauth/{quote(provider)}/poll/{quote(session)}",
                                allow=(400, 404))
        if status != 200:
            raise Exit(EXIT_UNKNOWN, stderr=f"登录会话不在了（过期或控制台重启过）：{body}")
        state = {"pending": "pending", "approved": "approved", "denied": "denied",
                 "expired": "expired"}.get(body.get("status"), "failed")
        message = body.get("error_message") if state == "failed" else None
        return {"state": state, "message": short(message)}

    def op_login_finish(self):
        # 只给 browser_login；这个插件不声明它，所以没有能收尾的会话。
        raise Exit(EXIT_UNKNOWN, stderr="这个会话不是浏览器回跳登录")

    def op_login_cancel(self):
        _, session = self.session()
        _, body = self.api("DELETE", f"/api/providers/oauth/sessions/{quote(session)}")
        if not body.get("ok"):
            raise Exit(EXIT_UNKNOWN, stderr="登录会话不在了")
        return {}

    # ---- 模型 ----

    def models_of(self, provider_id):
        _, body = self.api("GET", "/api/model/options")
        wanted = provider_id.lower()
        for row in body.get("providers", []):
            # 自建的 OpenAI 兼容服务在这里的 slug 是裸 ID，custom:<id> 在 aliases 里。
            names = [row.get("slug", "")] + list(row.get("aliases") or [])
            if wanted in (str(name).lower() for name in names):
                return [str(model) for model in row.get("models") or [] if str(model).strip()]
        if provider_id.startswith(f"{CUSTOM}:"):
            raise Exit(EXIT_UNKNOWN, stderr=f"没有这个 OpenAI 兼容服务：{provider_id}")
        self.provider(provider_id)
        raise self.invalid("provider", "not_connected")

    def op_models(self):
        provider_id = self.field("provider")
        models = self.models_of(provider_id)
        _, pick = self.api("GET", f"/api/model/recommended-default?provider={quote(provider_id)}")
        recommended = pick.get("model") if pick.get("model") in models else None
        if recommended:
            models.remove(recommended)
            models.insert(0, recommended)
        return {"models": [{"id": model, "recommended": model == recommended} for model in models]}

    def op_select(self):
        provider_id, model = self.field("provider"), self.field("model")
        if model not in self.models_of(provider_id):
            raise self.invalid("model", "unknown_model")
        _, body = self.api("POST", "/api/model/set", {
            "scope": "main", "provider": provider_id, "model": model,
            "confirm_expensive_model": self.request.get("confirmed") is True,
        }, allow=(400,))
        if body.get("confirm_required"):
            warning = str(body.get("confirm_message") or "").strip()
            confirm = f"{self.text('confirm')}\n{warning}" if warning else self.text("confirm")
            return {"current": current_model(), "confirm": confirm[:500]}
        if not body.get("ok", True) or "detail" in body:
            raise Exit(EXIT_UPSTREAM, stderr=f"控制台没存上模型：{body}")
        return {"current": current_model()}

    def op_test(self):
        return run_test(current_model(), self.text("fallback"))


def run_test(current, fallback_text):
    """用这个 Bot 的主模型和凭据真发一条很短的请求，走上游辅助调用的同一套客户端（各家的认证、接口形状都由它管）。"""
    if current is None:
        return {"result": "no_model"}
    from agent.auxiliary_client import call_llm
    from agent.error_classifier import FailoverReason, classify_api_error
    from hermes_cli.env_loader import load_hermes_dotenv

    # key 在数据目录的 .env 里（控制台写的），上游的 hermes 命令启动时也是这样先读进环境。
    load_hermes_dotenv()

    route = {}
    started = time.monotonic()
    try:
        response = call_llm(provider=current["provider"], model=current["model"], max_tokens=16,
                            messages=[{"role": "user", "content": "Reply with OK."}],
                            timeout=TEST_TIMEOUT, route_info=route)
    except Exception as error:  # 上游的客户端各家抛的类型不一样，统一交给它自己的分类
        reason = classify_api_error(error, provider=current["provider"], model=current["model"]).reason
        if reason in (FailoverReason.auth, FailoverReason.auth_permanent):
            result = "rejected"
        elif reason in (FailoverReason.timeout, FailoverReason.ssl_cert_verification) or is_connection(error):
            result = "unreachable"
        else:
            result = "upstream"
        return {"result": result, "message": short(str(error))}
    latency = int((time.monotonic() - started) * 1000)
    if route.get("failure"):
        # 上游在主模型失败后会改走配了的备用：备用回了不算主模型能用。
        return {"result": "upstream", "message": short(f"{fallback_text} {route['failure']}"), "latency_ms": latency}
    if not getattr(response, "choices", None):
        return {"result": "upstream", "message": short(f"{response!r}")}
    return {"result": "ok", "latency_ms": latency}


def is_connection(error):
    names = {type(item).__name__ for item in (error, error.__cause__, error.__context__) if item is not None}
    return bool(names & {"APIConnectionError", "ConnectError", "ConnectionError", "ConnectTimeout"})


def key_preview(endpoint, env):
    """控制台把自建服务的 key 存成 .env 变量的引用，预览给的是 ``${变量名}``：换成那个变量打了码的值。"""
    preview = str(endpoint.get("api_key_preview") or "")
    if preview.startswith("${") and preview.endswith("}"):
        preview = (env.get(preview[2:-1]) or {}).get("redacted_value") or ""
    return redacted(preview)


def redacted(value):
    """控制台打了码的 key 形如 ``«redacted:sk-m...b4b0»``，App 只要里面那段。"""
    value = str(value or "").strip()
    if value.startswith("«redacted:") and value.endswith("»"):
        value = value[len("«redacted:"):-1]
    return value or None


def current_model():
    """配置里的主模型，判据和 wonton 插件“还没选模型”的提示一样：没有 model.default 就是没选。"""
    from hermes_cli.config import load_config_readonly

    model = (load_config_readonly() or {}).get("model")
    if not isinstance(model, dict):
        return None
    name = model.get("default") or model.get("model")
    if not isinstance(name, str) or not name.strip() or "${" in name:
        return None
    return {"provider": str(model.get("provider") or "auto"), "model": name.strip()}


def dashboard_token():
    """和控制台读同一份口令（上游 web_server_session_token 的 file: 写法）。"""
    value = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "")
    if not value.startswith("file:"):
        raise Exit(EXIT_NOT_READY, stderr="容器没设 HERMES_DASHBOARD_SESSION_TOKEN=file:…，拿不到控制台口令")
    try:
        token = open(value[5:], encoding="utf-8-sig").read().strip()
    except OSError as error:
        raise Exit(EXIT_NOT_READY, stderr=f"读不到控制台口令：{error}") from None
    if not token:
        raise Exit(EXIT_NOT_READY, stderr="控制台口令文件是空的")
    return token


def quote(value):
    return urllib.parse.quote(value, safe="")


def parse_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return {"detail": text}


def short(text):
    if not text:
        return None
    text = " ".join(str(text).split())
    return text[:300] or None


def drop_privileges():
    """核心经 docker exec 以 root 执行；改成数据目录的属主，和上游的 hermes 命令一样（hermes-exec-shim.sh），
    免得读写凭据（刷新登录令牌、写缓存）时留下 root 的文件，gateway 读不了。"""
    home = os.environ.get("HERMES_HOME", "/opt/data")
    os.environ["HOME"] = home
    if os.geteuid() != 0:
        return
    owner = os.stat(home)
    if owner.st_uid == 0:
        return
    os.setgroups([])
    os.setgid(owner.st_gid)
    os.setuid(owner.st_uid)


OPS = {
    "catalog": Command.op_catalog, "save_key": Command.op_save_key, "disconnect": Command.op_disconnect,
    "login_start": Command.op_login_start, "login_poll": Command.op_login_poll,
    "login_finish": Command.op_login_finish, "login_cancel": Command.op_login_cancel,
    "models": Command.op_models, "select": Command.op_select, "test": Command.op_test,
}


def main():
    line = sys.stdin.readline()
    secret = None
    try:
        try:
            request = json.loads(line)
        except ValueError:
            raise Exit(EXIT_INVALID, {"field": None, "message": "not JSON"}) from None
        if not isinstance(request, dict) or request.get("op") not in OPS:
            raise Exit(EXIT_INVALID, {"field": "op", "message": "op?"})
        secret = request.get("key") if isinstance(request.get("key"), str) else None
        drop_privileges()
        result = OPS[request["op"]](Command(request))
        code, stdout, stderr = 0, result, ""
    except Exit as done:
        code, stdout, stderr = done.code, done.stdout, done.stderr
    except Exception as error:  # 上游模块抛的意外错误：如实报上游出错，不伪装成功
        code, stdout, stderr = EXIT_UPSTREAM, None, f"{type(error).__name__}: {error}"
    if stdout is not None:
        sys.stdout.write(scrub(json.dumps(stdout, ensure_ascii=False), secret) + "\n")
    if stderr:
        sys.stderr.write(scrub(stderr, secret)[:1000] + "\n")
    sys.exit(code)


def scrub(text, secret):
    return text.replace(secret.strip(), "[key]") if secret and secret.strip() else text


if __name__ == "__main__":
    main()
