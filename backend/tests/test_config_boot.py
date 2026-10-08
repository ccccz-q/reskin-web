"""启动期自检 —— 配置的「安全闸门必须永远能跑完」

    python tests/test_config_boot.py

★★ 这里要防的回归（2026-10-06 参赛评审自查发现，已实测复现）
------------------------------------------------------------
`config.py` 里有这么一段安全闸门：

    ADMIN_READY = bool(ADMIN_PASSWORD) and len(ADMIN_PASSWORD) >= 8
    if ADMIN_PANEL and not ADMIN_READY:
        logger.error("ADMIN_PANEL=1 但 ADMIN_PASSWORD 未设置或少于 8 位……")

它的意图很好（宁可没有面板，也不要一个能被猜到的面板）。但当时全文
**没有任何 logger 定义**（import 只有 os / pathlib / dotenv），于是：

    ADMIN_PANEL=1 + 口令不足 8 位  →  NameError: name 'logger' is not defined

**安全闸门自己把进程干掉了** —— 而且是最容易被评委亲手触发的那一类：
按 README 去开管理员面板，就起不来。

★ 为什么不能改成「从 infra.logging 导入 logger」：
  infra/logging.py 反过来 `from config import STORAGE_DIR`（它的审计日志要落盘），
  那样会形成循环导入。所以正确做法是 config 用标准库自建 logger，
  并把「为什么不能用 infra 的」写在代码里，防止后人"顺手改回去"。

★ 这组用例为什么必须在**子进程**里跑：
  ① 模块级 env 只读一次，同进程改 os.environ 再 import 拿到的是缓存模块，等于没测；
  ② `load_dotenv(ENV_PATH)` 会读**项目根的真实 .env**（路径由 __file__ 推导，
     换 cwd 也躲不掉）—— 所以每条用例都必须**显式**给出 ADMIN_PASSWORD
     （哪怕是空串），否则测的是.env 里的真口令，不是这一行代码。
     空串算"已存在的环境变量"，dotenv 默认不覆盖，所以显式给空是有效的隔离。
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def boot(**env_overrides: str) -> tuple[int, str]:
    """在**全新解释器**里 import config，返回 (returncode, 输出)

    每个用例都显式带 ADMIN_PASSWORD，避免读到开发机真实 .env 里的口令。
    """
    env = {**os.environ, "PYTHONPATH": str(APP), "ADMIN_PANEL": "0",
           "ADMIN_PASSWORD": "", **env_overrides}
    with tempfile.TemporaryDirectory() as td:
        p = subprocess.run(
            [sys.executable, "-c",
             "import config;print('PANEL=', config.ADMIN_PANEL, "
             "'| STORAGE=', config.STORAGE_DIR.is_absolute())"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=td, env=env, timeout=90,
        )
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


print("=== 1. 安全闸门：任何 env 组合都不能让进程起不来 ===")
# ★ 本组是本文件存在的全部理由：导入必须成功，且面板必须被关掉

rc, out = boot(ADMIN_PANEL="1", ADMIN_PASSWORD="short")
check("弱口令：进程正常启动（不 NameError）", rc == 0, out[:130])
check("弱口令：面板保持关闭", "PANEL= False" in out, out[:130])

rc, out = boot(ADMIN_PANEL="1", ADMIN_PASSWORD="")
check("空口令：进程正常启动", rc == 0, out[:130])
check("空口令：面板保持关闭", "PANEL= False" in out, out[:130])

rc, out = boot(ADMIN_PANEL="1", ADMIN_PASSWORD="1234567")
check("正好 7 位：仍判定为不合格", rc == 0 and "PANEL= False" in out, out[:130])

rc, out = boot(ADMIN_PANEL="1", ADMIN_PASSWORD="12345678")
check("正好 8 位：判定为合格", rc == 0 and "PANEL= True" in out, out[:130])

rc, out = boot(ADMIN_PANEL="1", ADMIN_PASSWORD="longenough123")
check("合格口令：面板正常开启", rc == 0 and "PANEL= True" in out, out[:130])

rc, out = boot(ADMIN_PANEL="0", ADMIN_PASSWORD="")
check("未开启面板：不受影响", rc == 0 and "PANEL= False" in out, out[:130])

print()
print("=== 2. 闸门要「说出来」，而不是静悄悄关掉 ===")
# 静悄悄关掉= 运维以为面板开了，实际没开，排查时完全看不出发生过什么
rc, out = boot(ADMIN_PANEL="1", ADMIN_PASSWORD="short")
check("弱口令有告警输出（运维看得见发生了什么）",
      rc == 0 and "ADMIN_PASSWORD" in out, out[:170])

print()
print("=== 3. 分层纪律：config 是最底层，不得反向 import infra ===")
# 只能查真实的 import 语句 —— 注释里为了说明原因会提到 infra，不能算命中
src = (APP / "config.py").read_text(encoding="utf-8")
bad = [ln.strip() for ln in src.splitlines()
       if re.match(r"\s*(from|import)\s+infra", ln)]
check("config.py 没有 import infra.*", not bad, bad[:2])
log_src = (APP / "infra" / "logging.py").read_text(encoding="utf-8")
check("infra.logging 确实反向依赖 config（所以那条路走不通）",
      "from config import" in log_src)
check("config.py 用标准库 logging 自建 logger",
      "logging.getLogger" in src)

print()
print("=== 4. 存储路径仍由 __file__ 推导（换个 cwd 也一样） ===")
rc, out = boot()
check("换cwd 后 STORAGE_DIR 仍是绝对路径", rc == 0 and "STORAGE= True" in out,
      out[:130])

# ── SERVE_API_DOCS 的空值处理（2026-10-08）─────────────────────
# ★ 为什么专门测这个：
#   `os.getenv("X", 默认值)` 只在变量**不存在**时返回默认值；
#   而 `.env.example` 里写 `SERVE_API_DOCS=`（等号后留空）会让变量
#   存在但为空串 —— 默认值不生效，空串被算成False，
#   于是**本地开发莫名变成"文档关闭"**。
#   这个 bug 的隐蔽之处：只在"照着 .env.example 复制一份"时出现，
#   本机没那个文件就完全测不到。
#   而 `.env.example` 里那一行正是我自己写的—— 于是自己埋了坑自己踩。
print("\n[测试 14] API 文档开关的空值语义")
_SERVE_PROBE = (
    "import sys; sys.path.insert(0, r'app');"
    "from config import SERVE_API_DOCS;"
    "print('True' if SERVE_API_DOCS else 'False')"
)


def _docs_flag(docs: str, public_mode: str) -> str:
    """在干净的子进程里跑一次，读回 SERVE_API_DOCS 的实际值。"""
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ)
    env["SERVE_API_DOCS"] = docs
    env["PUBLIC_MODE"] = public_mode
    env.setdefault("IMAGE_BACKEND", "openai")
    env.setdefault("IMAGE_API_KEY", "placeholder")
    env.setdefault("LLM_API_KEY", "placeholder")
    backend = str(Path(__file__).resolve().parents[1])          # → backend/
    r = _sp.run([sys.executable, "-c", _SERVE_PROBE],
                capture_output=True, text=True, env=env, cwd=backend)
    if r.returncode != 0:
        # ★ 探针失败时把 stderr 带出来：第一次写这函数时用了 r'app'
        #   这种相对路径 + 错误的 cwd，6 项全 ERR 而看不出原因。
        #   「静默的统一失败」比报错难查—— 至少要能说清失败在哪。
        return "ERR:" + (r.stderr or "").strip().splitlines()[-1][:60]
    return (r.stdout or "").strip().splitlines()[-1] if r.stdout else "ERR:空输出"


# 期望矩阵：空值跟随 PUBLIC_MODE；显式 0/1 无条件覆盖
for _docs, _pm, _want in [
    ("", "0", "True"),    # ★ 这条就是修复前会挂的那条
    ("0", "0", "False"),
    ("1", "0", "True"),
    ("", "1", "False"),
    ("0", "1", "False"),
    ("1", "1", "True"),
]:
    _got = _docs_flag(_docs, _pm)
    check(f"SERVE_API_DOCS={_docs!r} + PUBLIC_MODE={_pm} → {_want}",
          _got == _want, f"实际 {_got!r}")

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)