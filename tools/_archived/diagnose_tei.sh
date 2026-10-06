#!/usr/bin/env bash
# 七嘴八舌式排查脚本 · seven smile 主机（100.103.240.78）TEI embedding 服务
#
# 用途：定位「服务反复掉线」的根因，并给出可执行的修复动作。
# 用法：在 seven smile 主机上执行  bash diagnose_tei.sh
#
# 本脚本只读+ 打印诊断结论，**不自动修改任何东西**；修复动作需你确认后手动执行。

set -uo pipefail

echo "=============================================================="
echo " TEI embedding 服务诊断（seven smile 主机）"
echo " 时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="

# ---------- 0. 基础信息 ----------
echo ""
echo "【0】主机与架构"
echo "  hostname : $(hostname)"
echo "  arch     : $(uname -m)"
echo "  负载     : $(uptime | sed 's/.*load average/load/')"
if [ "$(uname -m)" = "aarch64" ]; then
  echo "  ⚠️ 架构是 arm64；若镜像是 amd64，会走模拟，慢且易触发时钟溢出"
fi

# ---------- 1. 服务在不在 ----------
echo ""
echo "【1】服务状态"
curl -s -m 8 -o /dev/null -w "  本机 127.0.0.1:18001/health → http=%{http_code}\n" \
  http://127.0.0.1:18001/health 2>&1 || echo "  本机 18001 连不上"
echo "  --- 监听端口 ---"
if command -v ss >/dev/null 2>&1; then
  ss -tlnp 2>/dev/null | grep -E "18001|:80 " | sed 's/^/    /'
elif command -v netstat >/dev/null 2>&1; then
  netstat -tlnp 2>/dev/null | grep -E "18001|:80 " | sed 's/^/    /'
fi

# ---------- 2. 怎么起的（手动进程 vs 守护）----------
echo ""
echo "【2】启动方式（★ 反复掉线的头号原因）"
if command -v docker >/dev/null 2>&1; then
  echo "  发现 docker，检查容器："
  docker ps -a --format '    {{.Names}} | {{.Status}} | {{.Image}}' 2>/dev/null | grep -iE "tei|embed" | sed 's/^/  /'
  C=$(docker ps -a --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -iE "tei|embed" | head -1 | awk '{print $1}')
  if [ -n "${C:-}" ]; then
    echo "  容器 [$C] 详细："
    echo "    重启次数: $(docker inspect "$C" --format '{{.RestartCount}}' 2>/dev/null)"
    echo "    退出码  : $(docker inspect "$C" --format '{{.State.ExitCode}}' 2>/dev/null)"
    echo "    OOM     : $(docker inspect "$C" --format '{{.State.OOMKilled}}' 2>/dev/null)"
    echo "    内存限制: $(docker inspect "$C" --format '{{.HostConfig.Memory}}' 2>/dev/null) bytes (0=无限制)"
    echo "    启动命令:"
    docker inspect "$C" --format '      {{join .Args " "}}' 2>/dev/null | head -5
    echo "    挂载:"
    docker inspect "$C" --format '{{range .Mounts}}      {{.Source}} -> {{.Destination}}{{println}}{{end}}' 2>/dev/null | head -6
    echo ""
    echo "  ★★ 重启次数 >0 = 反复崩溃，这是掉线的直接证据"
    if [ "$(docker inspect "$C" --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null)" = "no" ]; then
      echo "  ⚠️  restart policy = no → 崩了不会自动重启，必须设always/unless-stopped"
    fi
  fi
fi
echo "  --- systemd 守护？ ---"
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-units --type=service --all 2>/dev/null | grep -iE "tei|embed" | sed 's/^/    /' || echo "    无相关 systemd 服务（若是裸进程裸崩，无守护）"
fi
echo "  --- 裸进程？ ---"
ps aux 2>/dev/null | grep -iE "text-embeddings|text-embeddings-inference" | grep -v grep | sed 's/^/    /' || echo "    无裸进程"

# ---------- 3. 模型文件（★ 日志报错的真凶）----------
echo ""
echo "【3】模型文件完整性（日志报错：/models/bge-m3/onnx/model.onnx does not exist）"
for D in /models/bge-m3 /data/bge-m3 /opt/models/bge-m3 "$HOME/models/bge-m3"; do
  if [ -d "$D" ]; then
    echo "  找到模型目录：$D"
    echo "    顶层内容：$(ls "$D" 2>/dev/null | tr '\n' ' ')"
    if [ -d "$D/onnx" ]; then
      echo "    onnx/ 内容："
      ls -la "$D/onnx" 2>/dev/null | tail -n +2 | awk '{printf "      %s  %s\n", $5, $9}' | sed 's/^/  /'
      # 关键判定
      if [ -f "$D/onnx/model.onnx" ]; then
        SZ=$(stat -c%s "$D/onnx/model.onnx" 2>/dev/null || stat -f%z "$D/onnx/model.onnx" 2>/dev/null)
        echo "    ✅ model.onnx 存在（$SZ 字节）"
      else
        echo "    ❌ model.onnx 不存在 —— 与日志报错完全一致"
      fi
      if [ -f "$D/onnx/model.onnx_data" ]; then
        SZ=$(stat -c%s "$D/onnx/model.onnx_data" 2>/dev/null || stat -f%z "$D/onnx/model.onnx_data" 2>/dev/null)
        echo "    model.onnx_data：$SZ 字节（正常约 2266820608）"
        [ "$SZ" -lt 2000000000 ] 2>/dev/null && echo "    ⚠️ onnx_data 偏小，模型可能没下全"
      else
        echo "    ⚠️ model.onnx_data 不存在 → 大概率是下载不完整"
      fi
    else
      echo "    ❌ 没有 onnx/ 子目录 → TEI 找不到 onnx 后端（candle 会回退但慢）"
    fi
  fi
done
[ -d /models/bge-m3 ] || echo "  （/models/bge-m3 不存在，可能挂载路径不对）"

# ---------- 4. 资源 ----------
echo ""
echo "【4】资源（OOM 是 CPU 推理的头号杀手）"
free -h 2>/dev/null | sed 's/^/    /' || echo "    无法读取 free"
echo "  ---磁盘 ---"
df -h / /data /models 2>/dev/null | sed 's/^/    /' || df -h / | sed 's/^/    /'
echo "  dmesg 里的 OOM 记录："
if command -v dmesg >/dev/null 2>&1; then
  dmesg -T 2>/dev/null | grep -iE "out of memory|oom-kill|killed process" | tail -5 | sed 's/^/    /' || echo "    无 OOM 记录（需 root 才能看 dmesg）"
fi

# ---------- 5. 连跑稳定性 ----------
echo ""
echo "【5】embedding 连打 20 次（★ 判断能否替换本机的前提）"
OK=0; BAD=0
for i in $(seq 1 20); do
  C=$(curl -s -m 15 -o /dev/null -w "%{http_code}" \
      -X POST http://127.0.0.1:18001/v1/embeddings \
      -H 'Content-Type: application/json' \
      -d '{"input":"门店坪效怎么算","model":"/models/bge-m3"}' 2>/dev/null)
  if [ "$C" = "200" ]; then OK=$((OK+1)); else BAD=$((BAD+1)); echo "    第 $i 次失败 http=$C"; fi
  sleep 1
done
echo "  结果：成功 $OK / 失败 $BAD （要求 20/20 才可替换本机）"

# ---------- 6. 结论 ----------
echo ""
echo "=============================================================="
echo " 诊断结论"
echo "=============================================================="
echo ""
echo "【日志三条的判读】"
echo "  ① Rust panic 'overflow when subtracting durations'"
echo "     → 时间运算溢出。arm64 跑 amd64 镜像模拟时常见。"
echo "     → 多数非致命（后续日志显示服务继续启动到 Warming up）"
echo "     → 但若进程反复重启，则它是结果不是原因"
echo ""
echo "  ② ★ 病根：Could not start ORT backend"
echo "     File at /models/bge-m3/onnx/model.onnx does not exist"
echo "     → 模型文件缺失 / 挂载路径错 / 下载不完整"
echo "     → TEI 随后【自动回退 candle 后端】并成功启动"
echo "     → 所以现象是「能起来但很慢」，且掩盖了模型缺失问题"
echo ""
echo "  ③ 服务确实起来过（08:09:30 Warming up model）"
echo "     → 所以掉线不是「起不来」，是「起来之后又没了」"
echo "     → 头号嫌疑：① 无守护裸崩 ② OOM 被杀 ③ 模型路径错导致反复重启"
echo ""
echo "【修复动作】按顺序执行，见脚本末尾或另问我拿命令"
exit 0