#!/usr/bin/env bash
# 出片冒烟（fd-ci image-build.yml 的 smoke_script 输入）：构建后、推 tag 前跑。
#
# 为什么需要它：2026-11-26 实测 sha-80f4bf2 在 fd-prod CrashLoop 16 次 / 33 小时，
# 报 `ErpError: 数据库 schema 版本落后于代码要求`——迁移文件在镜像里却没跑。
# 而本仓的 CI（gates.yml）只跑「源码 + 真 Postgres 的 pytest」，永远不碰镜像本身；
# 出片链又没配 smoke_script ⇒ **「构建 success」只等于 docker build 过了**。
# 这个脚本补的就是那一环：让镜像真的启动一次。
#
# 验什么（按启动顺序，`serve` 的真实路径）：
#   1. 入口可执行 —— `--version` 不炸（import 期依赖缺失会在这里暴露）；
#   2. 服务能起来 —— 不带 DB 起 `serve --skip-schema-check`，`/health` 应答。
#      跳过 schema 闸是**故意的**：这层闸的判据是「库里的版本 vs 代码要的版本」，
#      构建机没有那个库；拿一个空库去跑只会红在「库没登记版本」，与本次要验的
#      「镜像能不能启动」无关。版本闸的覆盖在发布侧（滚版后必须看 pod 是否真起来）。
set -euo pipefail

: "${IMG:?IMG 未设置（应由 fd-ci image-build.yml 注入 registry/ns/image:main）}"

echo "== 1. 入口可执行 =="
docker run --rm --entrypoint fd-erp-ledger-mcp "$IMG" --version

echo "== 2. 服务能起来（无 DB，跳过 schema 闸）=="
CID=$(docker run -d -p 18000:8000 --entrypoint fd-erp-ledger-mcp "$IMG" \
        serve --host 0.0.0.0 --port 8000 --transport http --skip-schema-check)
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT

for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:18000/health >/tmp/health.json 2>/dev/null; then
    echo "health OK after ${i}s:"; head -c 200 /tmp/health.json; echo
    exit 0
  fi
  # 容器已经退出就没必要等满 30 秒 —— 立刻把日志打出来，红得有信息量
  if [ "$(docker inspect -f '{{.State.Running}}' "$CID")" != "true" ]; then
    echo "FAIL: 容器启动后即退出，日志：" >&2
    docker logs "$CID" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "FAIL: 30s 内 /health 不应答，日志：" >&2
docker logs "$CID" >&2 || true
exit 1
