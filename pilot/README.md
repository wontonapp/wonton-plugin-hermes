# 试点：Mirage 旁路容器当终端（ADR-0125 第 2 档）

只在试点分支里，不进正式插件。验证“隐藏、命令规则、ask 允许/拒绝/超时、一次性写盘”这一套中间档能力，由 `pilot/runner.py` 全自动跑，不用人点。

## 组成

- 镜像 `docker/Dockerfile.mirage`：官方 Hermes 镜像 + `mirage-ai`（fork 的 `ask_url` 补丁）+ `mirage-fuse` + fuse3。
- `terminal`（Bot 服务）：只跑 `sleep infinity`，挂 `/dev/fuse`、`SYS_ADMIN`、`apparmor:unconfined`，没有宿主目录、没有 docker socket。
- 网关多挂一个 `/var/run/docker.sock`：Mirage 的 docker 机器用它 `docker exec` 进终端容器。
- `pilot-runner`（全插件一份，常驻）：`:8000` 假收件箱 + 契约 fixture + 场景脚本；日志走 `scripts/dev.sh logs pilot-runner`。
- `init_mirage.sh`：把 Mirage 的 Hermes 插件放进数据目录，按 `wonton.bot` 标签找终端容器，写工作区 YAML。

## 跑与读

```bash
scripts/dev.sh up
scripts/dev.sh install . --yes
scripts/dev.sh logs pilot-runner      # JSON 行，result: PASS/FAIL，最后一行为 summary
```

结果看 `summary` 的 `failures`: 0 才算过。`run-green-registered.log` 是最后一次正式登记后的全绿记录（12 条用例、15 次 ask）。

## 用例设计里的坑（改之前先读）

- **ask 的 `session` 存在 Mirage 的共享环境里，跨 Hermes 会话仍生效**：`curl` 用过 `allow/session` 后，同进程再问 `curl` 不会触发。所以超时用例用 `wget -T 20`，不要用 `curl`。
- **跑批容器与终端用户 uid 不同**（前者可能是 root）：预建给终端的文件要 `chmod 0666` + `chown 1000:1000`，否则 FUSE 视图里终端写不进去。
- **同一份 state.db 只起一个网关**：两个网关会把 Hermes 的 SQLite 写锁加倍争用，一轮 chat 能卡二十分钟。
- 内置命令（`cat`、`rm`）不走命令规则；`workspace-read` 用例记录这一事实。终端容器里没有 `docker`（socket 只在网关），`debug-docker` 按 `NO_DOCKER` 断言。
- 假收件箱按 `argv` 里的 `-T 20` 判断“慢探针”：睡 8 秒等插件 5 秒超时；其余 curl/wget 立刻答 `allow/session`，`pip` 答 `deny`，`rm` 答 `allow/session`。
