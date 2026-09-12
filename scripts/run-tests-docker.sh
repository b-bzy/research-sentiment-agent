#!/usr/bin/env bash
# 在 Docker 里跑全套测试。
#
#   ./scripts/run-tests-docker.sh          # 默认 Python 3.12
#   ./scripts/run-tests-docker.sh all      # 3.10 / 3.11 / 3.12 / 3.13 全跑一遍
#
# 为什么要在 Docker 里跑：
#   1. 验证「零第三方依赖」这条约束——干净的 Linux 镜像里没有任何预装包
#   2. 验证跨平台（开发在 macOS，容器是 Linux，路径/编码/locale 都可能出问题）
#   3. 验证跨 Python 版本（项目声称 3.10+）
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"

VERSIONS=("3.12")
[ "${1:-}" = "all" ] && VERSIONS=("3.10" "3.11" "3.12" "3.13")

fail=0
for v in "${VERSIONS[@]}"; do
  img="python:${v}-slim"
  echo "════════════════════════════════════════════════════════════"
  echo "  Python ${v}"
  echo "════════════════════════════════════════════════════════════"

  # 挂载成只读，防止容器往宿主机代码目录写东西污染测试；
  # 运行时产物（db / out）写到容器内的 tmpfs
  out=$(docker run --rm \
      -v "$ROOT":/src:ro \
      -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 \
      -w /app "$img" \
      bash -c 'cp -r /src/run.py /src/sentiment /src/tests /src/data /app/ 2>/dev/null;
               python -m unittest discover -s tests 2>&1 | tail -5;
               echo "---- 端到端 ----";
               python run.py demo >/dev/null 2>&1 && echo "demo 退出码 0 ✓" || echo "demo 失败 ✗";
               echo "---- 幂等（再跑一次 ingest）----";
               python run.py ingest 2>&1 | grep -oE "新增 [0-9]+ 篇，重复跳过 [0-9]+ 篇"' 2>&1)

  echo "$out"
  echo "$out" | grep -q "^OK$" || { echo "  ✗ Python ${v} 单测未通过"; fail=1; }
  echo "$out" | grep -q "demo 退出码 0" || { echo "  ✗ Python ${v} 端到端未通过"; fail=1; }
  # ★ BUG-13：幂等这项原先只打印不断言（末尾 `|| true` 让它永不失败）。
  #   第二次 ingest 必须「新增 0 篇」，否则说明 MD5 去重失效。
  echo "$out" | grep -q "新增 0 篇" || { echo "  ✗ Python ${v} 幂等未通过（第二次 ingest 不应新增）"; fail=1; }
  echo
done

echo "════════════════════════════════════════════════════════════"
[ "$fail" = 0 ] && echo "  全部通过 ✓" || echo "  存在失败 ✗"
exit "$fail"
