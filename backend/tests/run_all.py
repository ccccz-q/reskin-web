#!/usr/bin/env python
"""跑一遍全部自测 —— 全部离线，不花任何真实 API 调用

    python tests/run_all.py

为什么自己写一个 runner 而不是直接上 pytest：
本机不一定装 pytest，而这个项目要保证「clone 下来 pip install 完就能跑」。
每个测试文件本身也是可执行脚本，单独跑也没问题：

    python tests/test_context_store.py
    python tests/test_agent_loop.py
    python tests/test_api_smoke.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
FILES = [
    "test_forge.py",
    "test_security.py",
    "test_card.py",
    "test_renderer.py",
    "test_preflight.py",   # 编译后漂移自检 + 核心规则按相关性选择（造梦师 preflight 移植）
    "test_repair.py",      # repair_image 局部修复：上一版成品为参考图 + CHANGE ONLY 外科指令
    "test_drift.py",       # 漂移诊断（repair v1）：解析健壮性 + 降级路径
    "test_repair_http.py", # repair/diagnose HTTP 层：会话隔离 + 落盘结构 + 限速
    "test_helper_doc.py",  # 小助手知识索引：新功能问得到 + 单节不被截断
    "test_governance.py",
    "test_context_store.py",
    "test_agent_loop.py",
    "test_api_smoke.py",
    "test_identity.py",
    "test_async_chat.py",     # 后台任务八条护栏（云端 60 秒网关的解法）
    "test_llm_resilience.py",  # 通道容错：上游「假装成功」时空响应 → 重试换通道
    "test_image_gen.py",       # 生图：画幅只三档（表达不了就跟随原图）+ 重试计划
    "test_public_isolation.py",  # 公开版隔离：无身份不得列举/写入他人图片
    "test_image_payload.py",     # 回图形态：上游改回 url 也能认（+ 取图 SSRF 两套判据）
    "test_mobile_upload.py",     # 手机相册 MPO 动态照片 + 出图回填时机
    "test_db_persistence.py",       # 数据持久性：WAL 会丢数据 → 必须 DELETE（线上事故固化）
    "test_error_taxonomy.py",    # 错误分诊：网关「假 400」要重试，原文不得直出用户
]

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# 测试期间锁死开关，避免手抖真的花了钱
os.environ["DISABLE_IMAGE_GENERATION"] = "1"

results: list[tuple[str, int, str, float]] = []
banner = "=" * 62

for name in FILES:
    print(f"\n{banner}\n▶ {name}\n{banner}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(TESTS / name)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.perf_counter() - t0
    out = (proc.stdout or "") + (proc.stderr or "")
    # 过滤掉 INFO 日志，只保留测试断言输出
    lines = [
        ln for ln in out.splitlines()
        if ln.startswith(("  OK", "  FAIL", "   ")) or ln.startswith("结果：")
    ]
    summary = next((ln for ln in out.splitlines() if ln.startswith("结果：")), "")
    print("\n".join(lines[-40:]) if len(lines) > 40 else "\n".join(lines))
    code = proc.returncode
    failed = summary.count("失败")
    results.append((name, code, summary, elapsed))

print(f"\n{banner}\n总览\n{banner}")
total_fail = 0
for name, code, summary, elapsed in results:
    status = "PASS" if code == 0 else "FAIL"
    print(f"  {status:5} {name:28} {elapsed:5.1f}s  {summary}")
    if code != 0:
        total_fail += 1

print(banner)
if total_fail == 0:
    print("全部通过 ✔")
else:
    print(f"{total_fail} 个文件有失败项 ✘")
sys.exit(0 if total_fail == 0 else 1)

# ⚠️ 这里曾经有过一个自欺欺人的 bug，值得留个警示：
#
#     status = "PASS" if code == 0 and "失败: 0" not in summary.replace("失败","失败") or ...
#
# 两个错叠在一起：
#   ① summary.replace("失败","失败") 是恒等操作，第一个子句永远为真
#      → 不管串里写的是「0 失败」还是「20 失败」，status 一律是 PASS
#   ② 三个测试脚本末尾只 print 不 sys.exit，退出码恒为 0
#      → 连 code != 0 这个分支都进不去
#
# 结果：整套自测「永远绿」，绿得毫无意义。
# 现在改成：测试脚本用 sys.exit(1) 报告失败，runner 只信退出码，不解析字符串。
