#!/bin/sh
# 试点用：把 Mirage 的 Hermes 插件放进这个 Bot 的数据目录，找到本 Bot 的终端容器，写出工作区。
# 由 init 的 entrypoint 最后调用；需要 docker socket（找容器）和 WONTON_BOT_NAME。
set -eu

plugin=/opt/data/plugins/mirage
mkdir -p "$plugin"
cp -R /seed/mirage-plugin/. "$plugin/"

# 试点镜像里 config.yaml 是种子的一部分：升级时 boot 先跑、旧 config.yaml 还在（catalog 版没有我们的
# model/terminal/mirage 设置），init 必须把它换成种子版，否则升级后模型和终端都退回宿主模式。
cp /seed/config.yaml /opt/data/config.yaml

# init 容器不注入 WONTON_BOT_NAME（只有 Bot 建好后的服务才有），从兄弟容器的标签里找。
bot="${WONTON_BOT_NAME:-}"
container=
tries=0
while [ "$tries" -lt 120 ]; do
  if [ -z "$bot" ]; then
    bot=$(docker ps \
      --filter "label=wonton.plugin=com.nousresearch.hermes" \
      --filter "label=wonton.service=terminal" \
      --format '{{.Label "wonton.bot"}}' 2>/dev/null | head -n1)
  fi
  if [ -n "$bot" ]; then
    # 只用 running 的、且 docker ps 默认最新在前：失败的旧安装会留下同名标签的僵尸容器，
    # `ps -a` 会挑到它们，把 exec 指到没有 /opt/data 的旧容器上。
    container=$(docker ps \
      --filter "label=wonton.plugin=com.nousresearch.hermes" \
      --filter "label=wonton.bot=${bot}" \
      --filter "label=wonton.service=terminal" \
      --format '{{.ID}}' 2>/dev/null | head -n1)
  fi
  [ -n "$container" ] && break
  tries=$((tries + 1))
  sleep 1
done
if [ -z "$container" ]; then
  echo "init_mirage: 找不到本 Bot 的终端容器（120 秒）" >&2
  exit 1
fi

mkdir -p /opt/data/work
sed "s/__TERMINAL_CONTAINER__/${container}/" /seed/mirage-workspace.yaml > /opt/data/work/workspace.yaml

# 试点跑批用的固定 API key。升级时 .env 会留下网关自己生成的随机 key，跑批会用错，
# 所以每次都改写这一行（只改 key，别的行不动）。
touch /opt/data/.env
sed -i '/^API_SERVER_KEY=/d' /opt/data/.env
echo 'API_SERVER_KEY=pilot-api-key-0123456789' >> /opt/data/.env

# init 容器不注入 PUID/PGID（和 WONTON_BOT_NAME 一样）：按镜像里的 hermes 用户给，别按 root 给，
# 否则 FUSE 服务以 hermes 跑，写 root 名下 755 的目录会被拒。
uid="${PUID:-$(id -u default 2>/dev/null || echo 1000)}"
gid="${PGID:-$(id -g default 2>/dev/null || echo 1000)}"
chown -R "$uid:$gid" /opt/data/plugins/mirage /opt/data/work 2>/dev/null || true
chown "$uid:$gid" /opt/data/.env 2>/dev/null || true
chmod 0755 /opt/data/work 2>/dev/null || true
echo "init_mirage: terminal container ${container}"
