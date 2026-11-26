# 试点：Mirage 旁路容器当终端（ADR-0125 第 2 档）

只在试点分支里，不进正式插件。验证四件事，全部由 `pilot/runner.py` 驱动，不用人点：

1. 隐藏生效：`cat /opt/data/.env` 和用 `python3` 打开它都被拒；
2. ask 允许：`rm` 先打到假收件箱，允许后命令真跑；
3. ask 拒绝：`pip --version` 打到假收件箱，拒绝后命令 exit 126；
4. ask 超时：`mv` 让假收件箱睡 8 秒、客户端 5 秒超时，按拒绝。

## 组成

- 镜像 `docker/Dockerfile.mirage`：官方 Hermes 镜像加 `mirage-ai`（fork 的 `ask_url` 补丁）和 `mirage-fuse`。
- `terminal`（Bot 服务）：只听 `sleep infinity`，有 `/dev/fuse` 和 `SYS_ADMIN`，没有宿主目录、没有 docker socket。
- 网关多挂一个 `/var/run/docker.sock`：Mirage 的 docker 机器用它 `docker exec` 进终端容器。
- `pilot-runner`（全插件一份）：`/ask` 假收件箱加场景脚本；日志走 `scripts/dev.sh logs pilot-runner`。
- `init_mirage.sh`：把 Mirage 的 Hermes 插件放进数据目录，按 `wonton.bot` 标签找到终端容器，写工作区 YAML。

## 跑

```bash
scripts/dev.sh up          # 第一次
scripts/dev.sh install . --yes
scripts/dev.sh logs pilot-runner
```

场景在 `runner.py` 的 `CASES` 里。每个用例打一行 JSON，`result` 是 PASS/FAIL；最后一行是汇总。
