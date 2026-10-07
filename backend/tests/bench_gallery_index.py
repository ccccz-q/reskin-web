"""一次性性能对比：画廊扫描 冷索引 vs 热索引（2026-10-07）

    python tests/bench_gallery_index.py

★ 为什么它不在 run_all 清单里
--------------------------
它不是断言型测试，而是**性能测量**：同样的输入在不同机器、不同磁盘
缓存状态下耗时差几倍都是正常的。放进 run_all 只会变成一个经常误报的
"红灯"，久而久之就被无视了 —— 那比没有这个测试更糟。
所以它只作为一份可复现的测量脚本留在这里。

它验证的是两件事：
  ① 热路径确实显著快于冷路径（增量索引真的在生效）；
  ② **冷/热两次的输出逐字节相同** —— 性能优化绝不能改变结果。
第二点比第一点重要得多。

★ 它不创建也不删除任何文件
------------------------
测量对象就是 storage/images 里已有的真实图片。造一批临时文件再删掉
看着更"可控"，但那会让脚本在受限环境里因删文件被拦下而跑不完；
而这个脚本的价值恰恰是"随时能跑"。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import routers.image as im                                   # noqa: E402
from config import IMAGE_STORAGE_DIR                         # noqa: E402

REPS = 5


def best_of(fn, *a):
    """取多次里最好的一次 —— 排除磁盘缓存与调度抖动带来的噪声"""
    best, out = 1e9, None
    for _ in range(REPS):
        t = time.perf_counter()
        out = fn(*a)
        best = min(best, time.perf_counter() - t)
    return best, out


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    total_files = dirs = images = 0
    for dirpath, dirnames, filenames in os.walk(IMAGE_STORAGE_DIR):
        dirs += 1
        total_files += len(filenames)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        images += sum(1 for f in filenames
                      if os.path.splitext(f)[1].lower()
                      in (".png", ".jpg", ".jpeg", ".webp"))

    print(f"扫描根        : {IMAGE_STORAGE_DIR}")
    print(f"目录数        : {dirs}")
    print(f"文件总数      : {total_files}")
    print(f"白名单内图片  : {images}")

    # ① 冷索引：**每次测量前都清空索引**，取这些次里最慢的一次。
    #   ★ 这里不能用 best_of 取"最好的一次" —— 第一次扫完索引就建好了，
    #     后4 次全是热路径，取 min 等于拿热路径当冷路径测（会得到 1.0x
    #     的荒谬结论）。冷路径该取max：这才是用户真正等待的那一次。
    cold = 0.0
    r_cold = None
    for _ in range(REPS):
        im._gallery_index.clear()
        t = time.perf_counter()
        r_cold = im._gallery_sync(500, "default", None)
        cold = max(cold, time.perf_counter() - t)

    # ② 热索引：索引已建立，取最好的一次（排除磁盘缓存等噪声）
    hot, r_hot = best_of(im._gallery_sync, 500, "default", None)

    print()
    print(f"冷索引扫描    : {cold:.4f}s")
    print(f"热索引扫描    : {hot:.4f}s")
    print(f"加速比        : {cold / hot:.1f}x")
    print()
    print(f"返回条数      : {r_cold['count']}")
    print(f"冷/热一致     : {r_cold == r_hot}")
    print(f"索引目录数    : {len(im._gallery_index)}")

    # kinds 过滤也顺带验一遍：索引是跨会话共享的，
    # 这里确认带过滤与不带过滤不会互相污染。
    only_seed = im._gallery_sync(500, "default", {"seed"})
    print(f"kinds=seed    : {only_seed['count']} 条，"
          f"kind 全为 seed: {all(x['kind'] == 'seed' for x in only_seed['items'])}")

    ok = (r_cold == r_hot and hot < cold
          and all(x["kind"] == "seed" for x in only_seed["items"]))
    print()
    print("结论:", "PASS（热更快、结果一致、过滤不串味）" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())