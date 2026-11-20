# shellcheck shell=bash disable=SC2034
# Hermes 的清单有 webui（默认 Bot 的原生 Channels 页，sso: none）：
#   网页打得开由 web_assert 验页面注入的会话 token 和回环模式；
#   dashboard 没有独立账号：用户身份验的是核心网关放行的会话拿页面注入的 token 进到原生的回环身份。
#
# 数据：data 钩子只允许 SMOKE_* 变量和 web_get / dweb，都是打插件网页的通道，页面不写实例数据。
# Bot 数据目录里能留下的只有 Matrix 会话加密状态和对话历史，钩子约束内（无 docker exec、无 Matrix 配对）
# 够不到；加密私聊和会话重载在附加检查里实测。因此声明 DATA_NA=1，升级保数据、重装保数据按声明记 N/A。
#
# 共享验收可能已卸掉预装的 Hermes；仍装着时才保留数据卸载，再由调用方安装工作区版本。
before_install() {
  local installed
  installed="$(probes_dev status)" || return 1
  if grep -q '^com\.nousresearch\.hermes[[:space:]]' <<<"$installed"; then
    probes_dev uninstall
  fi
}

DATA_NA=1
SMOKE_NOTE="原生 dashboard 使用页面注入的会话 token；主人会话由核心网关验证，应用身份固定为 local。"

web_assert() {
  python3 -c 'import re,sys
page=sys.stdin.read()
assert re.search(r"window\.__HERMES_SESSION_TOKEN__=\"[^\"]+\"", page), "缺少 Hermes 原生会话 token"
assert "window.__HERMES_AUTH_REQUIRED__=false" in page, "dashboard 不是回环模式"
'
}

# 原生 dashboard 的请求：页面注入的会话 token 放在 X-Hermes-Session-Token 头里（同上游 web/src/lib/api.ts）；
# 不带 token 的请求即使有网关会话也会被拒。
hermes_token() {
  HERMES_TOKEN="$(curl_t "$SMOKE_WEB_MAX_TIME" "$SMOKE_WEB_ORIGIN/" --noproxy '*' -L -b "$SMOKE_JAR" -c "$SMOKE_JAR" \
    -H 'Accept: text/html' | sed -n 's/.*window\.__HERMES_SESSION_TOKEN__="\([^"]*\)".*/\1/p' | head -1)"
  [[ -n "$HERMES_TOKEN" ]]
}
hermes_dash() { # <方法> <路径>
  curl_t "$SMOKE_WEB_MAX_TIME" "$SMOKE_WEB_ORIGIN$2" --noproxy '*' --fail-with-body -X "$1" \
    -b "$SMOKE_JAR" -c "$SMOKE_JAR" -H "X-Hermes-Session-Token: $HERMES_TOKEN"
}

identity_check() { # 用户的网关会话拿页面注入的 token，进到原生 dashboard 的回环身份（不是另一套 Hermes 账号）
  local me
  hermes_token || { echo "页面里没有注入的会话 token"; return 1; }
  me="$(hermes_dash GET /api/auth/me)" || { echo "/api/auth/me 失败：$me"; return 1; }
  jq -e '.user_id == "local" and .provider == "loopback"' <<<"$me" >/dev/null ||
    { echo "原生身份不是回环的 local：$me"; return 1; }
}

# Agent 专项：附加检查里用套件的 Agent 助手逐项记子项（见 scripts/agent-probes.sh），加上原生 dashboard 的两项。
# /version 是网关自己答的命令，不要模型，验加密私聊和原生重启前后都能收发。
EXTRA_TIMEOUT=2400
extra_check() {
  hermes_check_dashboard
  agent_check_chat /version
  hermes_check_native_restart
  hermes_check_core_restart
  hermes_check_tuwunel_outage
  agent_check_model
  agent_check_mcp_bus
  agent_check_outsider
}

hermes_gateway() { running_container "wonton-$SMOKE_INSTANCE--$SMOKE_SLUG--gateway"; }

# Tuwunel 是核心 compose 里的服务，容器名由 compose 生成，按服务标签找。
hermes_tuwunel() {
  docker ps -q --filter "label=com.docker.compose.project=wonton-$SMOKE_INSTANCE" \
    --filter "label=com.docker.compose.service=tuwunel" | head -1
}

# 原生控制台：看得到在跑的网关进程，Channels 列得出七个平台。
hermes_check_dashboard() {
  local status channels
  hermes_token || { extra_item FAIL 原生控制台 "页面里没有注入的会话 token"; return 0; }
  status="$(hermes_dash GET /api/status)" || { extra_item FAIL 原生控制台 "/api/status 失败：$(agent_tail "$status")"; return 0; }
  jq -e '.gateway_running == true and (.gateway_pid | type == "number")' <<<"$status" >/dev/null ||
    { extra_item FAIL 原生控制台 "dashboard 看不到在跑的网关：$(agent_tail "$status")"; return 0; }
  channels="$(hermes_dash GET '/api/messaging/platforms?profile=default')" ||
    { extra_item FAIL 原生控制台 "Channels 接口失败：$(agent_tail "$channels")"; return 0; }
  if jq -e '[.platforms[].id] as $ids | ["telegram", "matrix", "feishu", "wecom", "dingtalk", "qqbot", "weixin"] | all(. as $id | $ids | index($id) != null)' \
      <<<"$channels" >/dev/null; then
    extra_item PASS 原生控制台 "dashboard 看得到在跑的网关，Channels 列出 telegram、matrix、feishu、wecom、dingtalk、qqbot、weixin"
  else
    extra_item FAIL 原生控制台 "Channels 少了要的平台：$(agent_tail "$channels")"
  fi
}

# 原生重启：经 dashboard 重启网关进程（不经 docker restart），进程号要换、容器不重启，之后加密私聊照样通。
hermes_check_native_restart() {
  local gw before after status pid old i out
  agent_need 重启后还能聊 || return 0
  gw="$(hermes_gateway)" || { extra_item FAIL 重启后还能聊 "找不到 gateway 容器"; return 0; }
  hermes_token || { extra_item FAIL 重启后还能聊 "页面里没有注入的会话 token"; return 0; }
  old="$(hermes_dash GET /api/status | jq -r '.gateway_pid // empty')"
  before="$(docker inspect "$gw" --format '{{.Id}} {{.State.StartedAt}}')"
  pid="$(hermes_dash POST '/api/gateway/restart?profile=default' | jq -r 'select(.ok == true) | .pid // empty')"
  [[ -n "$pid" ]] || { extra_item FAIL 重启后还能聊 "dashboard 发起重启失败"; return 0; }
  for ((i = 0; i < 90; i++)); do
    status="$(hermes_dash GET '/api/actions/gateway-restart/status?lines=200')" || status=""
    jq -e '.running == false and .exit_code != null' <<<"$status" >/dev/null 2>&1 && break
    sleep 2
  done
  jq -e '.exit_code == 0' <<<"$status" >/dev/null 2>&1 ||
    { extra_item FAIL 重启后还能聊 "原生重启没成功：$(agent_tail "$status")"; return 0; }
  for ((i = 0; i < 90; i++)); do
    status="$(hermes_dash GET /api/status)" || status=""
    jq -e --arg old "$old" '.gateway_running == true and (.gateway_pid | tostring) != $old' <<<"$status" >/dev/null 2>&1 && break
    sleep 2
  done
  ((i < 90)) || { extra_item FAIL 重启后还能聊 "原生重启后 180 秒没有新的网关进程"; return 0; }
  agent_wait_healthy "$gw" || { extra_item FAIL 重启后还能聊 "原生重启后容器没回到 healthy"; return 0; }
  after="$(docker inspect "$gw" --format '{{.Id}} {{.State.StartedAt}}')"
  [[ "$before" == "$after" ]] || { extra_item FAIL 重启后还能聊 "原生重启把容器也重建或重启了"; return 0; }
  if out="$(agent_say --reply any --settle 5 --wait 180 --text /version 2>&1)"; then
    extra_item PASS 重启后还能聊 "经 dashboard 原生重启：网关进程号换了、容器没重启，之后加密私聊照样收发"
  else
    extra_item FAIL 重启后还能聊 "原生重启后收不到加密回复：$(agent_tail "$out")"
  fi
  printf '%s\n' "$out"
}

# 核心重启：升级、崩溃、手动重启核心都会这样，Bot 的同步和工具调用都要自己接上。核心在恢复插件、问不到 Tuwunel
# 期间要答可重试的 503（答 403 上游就停掉同步、把 MCP 服务器整轮停下，进程还活着、健康还绿，只有重启网关才好），
# 重启完私聊照样通、经总线调工具也照样成功。
hermes_check_core_restart() {
  local core gw health out file before i
  agent_need 核心重启后还能聊 || return 0
  core="wonton-$SMOKE_INSTANCE"
  gw="$(hermes_gateway)" || { extra_item FAIL 核心重启后还能聊 "找不到 gateway 容器"; return 0; }
  before="$(docker inspect "$gw" --format '{{.Id}} {{.State.StartedAt}}')"
  docker restart "$core" >/dev/null 2>&1 || { extra_item FAIL 核心重启后还能聊 "重启核心容器失败"; return 0; }
  for ((i = 0; i < 120; i++)); do
    health="$(docker inspect -f '{{.State.Health.Status}}' "$core" 2>/dev/null)" || health=""
    [[ "$health" == healthy ]] && break
    sleep 1
  done
  ((i < 120)) || { extra_item FAIL 核心重启后还能聊 "核心 120 秒没回到 healthy"; return 0; }
  if [[ "$(docker inspect "$gw" --format '{{.Id}} {{.State.StartedAt}}')" != "$before" ]]; then
    extra_item FAIL 核心重启后还能聊 "重启核心把网关容器也重启了，这项就测不出适配器自己重连"
    return 0
  fi
  agent_wait_healthy "$gw" || { extra_item FAIL 核心重启后还能聊 "网关没回到 healthy"; return 0; }
  agent_phone_rejoin || { extra_item FAIL 核心重启后还能聊 "核心重启后手机接不回核心的网络：$AGENT_WHY"; return 0; }
  if out="$(agent_say --reply any --settle 5 --wait 180 --text /version 2>&1)"; then
    extra_item PASS 核心重启后还能聊 "重启核心：网关容器没动，同步自己接上，加密私聊照样收发"
  else
    extra_item FAIL 核心重启后还能聊 "重启核心后收不到加密回复：$(agent_tail "$out")"
  fi
  printf '%s\n' "$out"
  # 工具这条要模型：同步接上不等于总线也接上了，MCP 那条要单独验（403 会被上游整轮停掉，只有重启网关才好）。
  agent_need 核心重启后还能调工具 model || return 0
  file="$(agent_tmp)"
  if agent_say --reply any --settle 10 --wait "${AGENT_WAIT:-240}" \
      --text '用 wonton__list_plugins 列出已装的插件，输出每项的 id（形如 xxx.yyy.zzz），一个一行，不要解释。' \
      >"$file" 2>&1 && grep -q 'ai.usemagpie.magpie' "$file"; then
    extra_item PASS 核心重启后还能调工具 "重启核心后 Bot 经总线调 wonton__list_plugins 成功，回答里有预装的模型网关"
  else
    extra_item FAIL 核心重启后还能调工具 "重启核心后 Bot 调不成 wonton 工具：$(agent_tail "$(cat "$file")")"
  fi
  rm -f "$file"
}

# Tuwunel 停着时核心每次启动都失败、被 restart 策略反复拉起：Bot 的 Matrix 同步和 MCP 都得自己接上，不能靠重启网关。
# 修之前核心对恢复期的 Bot 请求答 403（地址只给插件网络里的 Bot 用）：同步按永久错误停掉，MCP 服务器被 park，
# Tuwunel 回来以后也永远不回；修之后答可重试的 503 + Retry-After，同步退避重试，MCP 按可重连处理，主人一发
# 消息就被叫醒（插件挂 pre_gateway_dispatch）。
hermes_check_tuwunel_outage() {
  local core gw tuwunel before t0 restarts now logs park_line sync_line
  local health revived_at sent_at file audit_from reply="-" delta when i
  agent_need "Tuwunel 掉线" || return 0
  core="wonton-$SMOKE_INSTANCE--core"
  gw="$(hermes_gateway)" || { extra_item FAIL "Tuwunel 掉线" "找不到 gateway 容器"; return 0; }
  tuwunel="$(hermes_tuwunel)"
  docker inspect "$tuwunel" >/dev/null 2>&1 ||
    { extra_item FAIL "Tuwunel 掉线" "找不到 Tuwunel 容器（$tuwunel）"; return 0; }
  before="$(docker inspect "$gw" --format '{{.Id}} {{.State.StartedAt}}')"
  t0="$(date --iso-8601=seconds)"
  docker stop "$tuwunel" >/dev/null ||
    { extra_item FAIL "Tuwunel 掉线" "docker stop $tuwunel 失败"; return 0; }
  restarts="$(docker inspect -f '{{.RestartCount}}' "$core")"
  docker restart "$core" >/dev/null 2>&1 || docker start "$core" >/dev/null 2>&1
  for ((i = 0; i < 48; i++)); do
    sleep 5
    now="$(docker inspect -f '{{.RestartCount}}' "$core" 2>/dev/null)" || now="$restarts"
    ((now >= restarts + 2)) && break
  done
  if ((now >= restarts + 2)); then
    extra_item PASS "Tuwunel 掉线" "Tuwunel 停着时核心启动失败、被自己的 restart 策略反复拉起（RestartCount $restarts → $now）"
  else
    extra_item FAIL "Tuwunel 掉线" "Tuwunel 停着时核心没有崩溃重启：RestartCount 停在 $now"
  fi
  logs="$(docker logs -t --since "$t0" "$gw" 2>&1)"
  park_line="$(grep -m1 "MCP server 'wonton'.*parking" <<<"$logs" | sed 's/^[^ ]* //')"
  sync_line="$(grep -m1 -E 'Matrix: (permanent auth error|sync error)' <<<"$logs" | sed 's/^[^ ]* //; s/.*Matrix: /Matrix: /')"
  if [[ -n "$sync_line" ]]; then
    if grep -q 'permanent auth error' <<<"$sync_line"; then
      extra_item PASS "掉线时的答复" "核心答 403，网关按永久错误停掉同步：$sync_line"
    else
      extra_item PASS "掉线时的答复" "核心答可重试的错，网关退避后重试：$sync_line"
    fi
  else
    extra_item FAIL "掉线时的答复" "网关没有记录到同步失败：$(docker logs "$gw" --since "$t0" 2>&1 | tail -2 | tr '\n' ' ')"
  fi
  if [[ -n "$park_line" ]]; then
    extra_item PASS "MCP 被停" "MCP 服务器被 park：$park_line"
  else
    extra_item FAIL "MCP 被停" "网关没有记录到 MCP 服务器被 park"
  fi
  docker start "$tuwunel" >/dev/null
  for ((i = 0; i < 60; i++)); do
    health="$(docker inspect -f '{{.State.Health.Status}}' "$tuwunel" 2>/dev/null)" || health=""
    [[ "$health" == healthy ]] && break
    sleep 5
  done
  if [[ "$health" == healthy ]]; then
    extra_item PASS "Tuwunel 回来后" "Tuwunel 重新 healthy"
  else
    extra_item FAIL "Tuwunel 回来后" "Tuwunel 300 秒没回到 healthy"
  fi
  for ((i = 0; i < 60; i++)); do
    health="$(docker inspect -f '{{.State.Health.Status}}' "$core" 2>/dev/null)" || health=""
    [[ "$health" == healthy ]] && break
    sleep 5
  done
  if ((i >= 60)); then
    extra_item FAIL "Tuwunel 回来后" "核心在 Tuwunel 回来后 300 秒没回到 healthy"
    agent_phone_rejoin || extra_item FAIL "Tuwunel 回来后" "手机接不回核心的网络：$AGENT_WHY"
    return 0
  fi
  if [[ "$(docker inspect "$gw" --format '{{.Id}} {{.State.StartedAt}}')" != "$before" ]]; then
    extra_item FAIL "Tuwunel 回来后" "网关容器被重启过，测不出自己重连"
    return 0
  fi
  # 手机要等核心不再重启再建：核心每重启一次就换一个网络命名空间，早建了也接不上。
  agent_phone_rejoin || extra_item FAIL "Tuwunel 回来后" "手机接不回核心的网络：$AGENT_WHY"
  agent_need "掉线后工具能调" model || return 0
  file="$(agent_tmp)"
  audit_from=0
  docker exec "$core" test -f "$SMOKE_DATA_DIR/core/audit.jsonl" &&
    audit_from="$(docker exec "$core" sh -c "wc -l < '$SMOKE_DATA_DIR/core/audit.jsonl'")"
  sent_at="$(date +%s)"
  if agent_say --reply any --match 'ai.usemagpie.magpie' --settle 10 --wait 180 \
      --text '用 wonton__list_plugins 列出已装的插件，输出每项的 id（形如 xxx.yyy.zzz），一个一行，不要解释。' \
      >"$file" 2>&1 && grep -q 'ai.usemagpie.magpie' "$file"; then
    reply="$(grep -m1 'ai.usemagpie.magpie' "$file")"
    if [[ "$audit_from" -gt 0 ]]; then
      audit_from="$(docker exec "$core" sh -c "tail -n +$((audit_from + 1)) '$SMOKE_DATA_DIR/core/audit.jsonl'" 2>/dev/null |
        grep -m1 '"tool":"[^"]*list_plugins"')" || audit_from=""
    fi
    logs="$(docker logs -t --since "$t0" "$gw" 2>&1)"
    revived_at="$(sed -n 's/^\([^ ]*\) .*MCP server .wonton.: revived.*/\1/p' <<<"$logs" | head -1)"
    if [[ -n "$revived_at" ]]; then
      delta=$(( $(date -d "$revived_at" +%s) - sent_at ))
      if ((delta < 0)); then
        when="消息发出前 $((-delta)) 秒 MCP 自己探活恢复"
      else
        when="消息发出 ${delta} 秒后 MCP 被叫醒"
      fi
      if [[ -n "$audit_from" ]]; then
        extra_item PASS "掉线后工具能调" "$when（$(awk '{print $1}' <<<"$audit_from")），回答列出插件：$reply"
      else
        extra_item PASS "掉线后工具能调" "$when，回答列出插件：$reply"
      fi
    else
      extra_item PASS "掉线后工具能调" "网关没记到 MCP 被叫醒的日志，但回答列出了插件：$reply"
    fi
    printf '%s\n' "$(cat "$file")"
  else
    extra_item FAIL "掉线后工具能调" "Tuwunel 回来后 Bot 不回消息或调不成工具：$(agent_tail "$(cat "$file")")"
    printf '%s\n' "$(cat "$file")"
  fi
  rm -f "$file"
}
