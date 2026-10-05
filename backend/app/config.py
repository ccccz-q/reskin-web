"""项目配置 —— 全部绝对路径 + 全部可 env 覆盖

重写要点（对照审查报告 P0-5 / P1-11）
----------------------------------
1. **消灭相对路径**。旧版里 `main.py` / `routers/*.py` 有 4 处 `"./storage/images"`，
   以「当前工作目录」为基准。后果已经发生：项目下同时存在 `storage/` 和 `backend/storage/`
   两份存储 —— 从不同目录启动，图就散落在不同地方，表现为「数据库里有记录但画廊是空的」。
   → 现在所有路径都由 `__file__` 推导，与 cwd 彻底无关。

2. **移除 VECTOR_DB_DIR**。方案 §0 决策 6 已明确「移除 ChromaDB」，
   配置项留着只会让人以为是待办。

3. **密钥不再用空串兜底**。缺 key 时给出可读报错，而不是等到某次调用才冒出莫名其妙的 401。

4. **新增治理项**：上传体积上限、单会话生成上限、请求超时、Agent 步数上限。
   这些原本散落在代码里或压根没有（旧版 `await file.read()` 完全没有体积限制）。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ── 路径基准 ────────────────────────────────────────────────
# .../项目/backend/app/config.py
APP_DIR = Path(__file__).resolve().parent          # .../backend/app
BACKEND_DIR = APP_DIR.parent                       # .../backend
PROJECT_ROOT = BACKEND_DIR.parent                  # .../项目

# .env 在项目根（向后兼容 backend/.env）
_env_candidates = (PROJECT_ROOT / ".env", BACKEND_DIR / ".env")
ENV_PATH = next((p for p in _env_candidates if p.exists()), _env_candidates[0])
load_dotenv(ENV_PATH)


def _abs_path(env_key: str, default: Path) -> Path:
    """env 优先，但保证一定解析成绝对路径"""
    raw = os.getenv(env_key)
    if raw and raw.strip():
        p = Path(raw.strip()).expanduser()
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    return default.resolve()


def _abs_dir(env_key: str, default: Path) -> Path:
    p = _abs_path(env_key, default)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── 存储（唯一真源，全项目只引用这里）────────────────────────
STORAGE_DIR = _abs_dir("STORAGE_DIR", PROJECT_ROOT / "storage")
IMAGE_STORAGE_DIR = _abs_dir("IMAGE_STORAGE_DIR", STORAGE_DIR / "images")
SQLITE_DIR = _abs_dir("SQLITE_DIR", STORAGE_DIR / "sqlite")


def _warn_if_too_broad() -> list[str]:
    """存储根目录如果被指到盘符根 / 用户主目录，整个路径白名单就形同虚设

    因为 ensure_within() 的「允许目录」就是 IMAGE_STORAGE_DIR ——
    一旦它变成 C:\\ ，任何路径都在「允许范围」内，穿越防护直接归零。
    配置是用户可改的（.env），所以这里要做一次体检并显式告警。
    """
    warnings: list[str] = []
    for name, p in (("IMAGE_STORAGE_DIR", IMAGE_STORAGE_DIR), ("STORAGE_DIR", STORAGE_DIR)):
        resolved = p.resolve()
        if resolved == resolved.parent:                 # 盘符根，如 C:\
            warnings.append(f"{name} 指向盘符根 {resolved} —— 路径穿越防护将失效")
        elif resolved == Path.home().resolve():
            warnings.append(f"{name} 指向用户主目录 {resolved} —— 范围过大")
        elif len(resolved.parts) <= 2 and os.name == "nt":
            warnings.append(f"{name} 层级过浅：{resolved}")
    return warnings


STORAGE_WARNINGS = _warn_if_too_broad()

TEMPLATES_DIR = APP_DIR / "templates"               # 风格模板 + families/
SQLITE_PATH = SQLITE_DIR / "agent.db"

GENERATED_SUBDIR_FMT = "%Y-%m-%d"                   # 生成图按日期分子目录

# ── LLM（火山方舟 DeepSeek-V4.1-Flash 接入点）────────────────
DEEPSEEK_API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
DEEPSEEK_BASE_URL = (
    os.getenv("DEEPSEEK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
).strip()
DEEPSEEK_MODEL = (os.getenv("DEEPSEEK_MODEL") or "").strip()

# ── 生图（火山方舟 Seedream 接入点）──────────────────────────
SEEDREAM_API_KEY = (os.getenv("SEEDREAM_API_KEY") or "").strip()
SEEDREAM_BASE_URL = (
    os.getenv("SEEDREAM_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
).strip()
SEEDREAM_MODEL = (os.getenv("SEEDREAM_MODEL") or "").strip()

# ── 视觉模型（用于 extract_card：从原图提炼主体 / 锚点 / 风险点）──
# 留空 = 只走本地 Pillow 档（色板 + 尺寸），不产生额外费用。
# 「反推 forbid」的效力取决于它，配了才完整。
VISION_MODEL = (os.getenv("VISION_MODEL") or "").strip()

# 识别大模型可插拔：默认跟随 DeepSeek 渠道，也可单独指定供应商。
# 用户后续只需填 VISION_API_KEY / VISION_MODEL（必要时加 VISION_BASE_URL）即可启用看图能力。
VISION_API_KEY = (os.getenv("VISION_API_KEY") or "").strip() or DEEPSEEK_API_KEY
VISION_BASE_URL = (os.getenv("VISION_BASE_URL") or "").strip() or DEEPSEEK_BASE_URL

# 模板工坊：一次最多分析几张参考图。
# 再多只是烧 token —— 三到六张已经足以归纳出一种风格的色板与结构。
FORGE_MAX_IMAGES = int(os.getenv("FORGE_MAX_IMAGES", "6"))

# ── 关键任务通道（GPT-Plus key：工坊合成/编译/自修/迭代 + 看图）──
# 分工原则：关键问题用这里，非关键（agent 对话/工具轮/摘要）走 DEEPSEEK_*
PREMIUM_API_KEY = (os.getenv("PREMIUM_API_KEY") or "").strip() or DEEPSEEK_API_KEY
PREMIUM_BASE_URL = (os.getenv("PREMIUM_BASE_URL") or "").strip() or DEEPSEEK_BASE_URL
PREMIUM_MODEL = (os.getenv("PREMIUM_MODEL") or "").strip() or DEEPSEEK_MODEL

# ── 图像生成通道 ──
# IMAGE_BACKEND=openai → 任何 OpenAI 兼容的图像接口（images/generations + images/edits）
# IMAGE_BACKEND=ark    → 火山方舟 Seedream（旧通道，返回 url；保留作备胎）
IMAGE_BACKEND = (os.getenv("IMAGE_BACKEND") or "openai").strip().lower()
if IMAGE_BACKEND not in ("openai", "ark"):
    raise ValueError(f"IMAGE_BACKEND 只能是 openai 或 ark，当前是 {IMAGE_BACKEND!r}")
# 出图单独放宽超时：gpt-image-2 实测 43-110s，中转站拥堵时会超过全局 180s，
# 用 REQUEST_TIMEOUT_SEC 会稳定 APITimeoutError。
IMAGE_TIMEOUT_SEC = int(os.getenv("IMAGE_TIMEOUT_SEC", "300"))

# ── 单次尝试的上限 vs 整轮的总预算（2026-10-05）────────────────────
# ★ 这是两个不同的东西，混在一起会出事：
#   · IMAGE_ATTEMPT_TIMEOUT_SEC：**一次**请求最多等多久。
#     实测成功案例只要 26~45s（拥堵时 ~110s），300s 的旧值意味着
#     一次卡死就白占 5 分钟 —— 而重试计划有 5 步，最坏能拖到 25 分钟。
#   · IMAGE_TOTAL_BUDGET_SEC：**整轮**（含所有重试与等待）最多花多久。
#     空窗期长达几分钟时，宁可早点如实告诉用户「现在上游没有容量」，
#     也不要让前端的轮询挂到天荒地老。
IMAGE_ATTEMPT_TIMEOUT_SEC = int(os.getenv("IMAGE_ATTEMPT_TIMEOUT_SEC", "150"))
IMAGE_TOTAL_BUDGET_SEC = int(os.getenv("IMAGE_TOTAL_BUDGET_SEC", "330"))
IMAGE_API_KEY = (os.getenv("IMAGE_API_KEY") or "").strip() or SEEDREAM_API_KEY
IMAGE_BASE_URL = (os.getenv("IMAGE_BASE_URL") or "").strip() or SEEDREAM_BASE_URL
IMAGE_MODEL = (os.getenv("IMAGE_MODEL") or "").strip() or SEEDREAM_MODEL

# ── 服务 ────────────────────────────────────────────────────
BACKEND_HOST = os.getenv("BACKEND_HOST", "127.0.0.1")   # 旧版 0.0.0.0 会监听全网卡
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))

# ── 公开发布模式（S4 发布接线）─────────────────────────
# PUBLIC_MODE=1 时：
#   1) 挂载前端构建产物（FRONTEND_DIST，默认 <项目根>/frontend/dist）——
#      单端口同时服务页面与 API，发布平台只需暴露一个端口；
#   2) /api/audit 关闭（那是本机排障接口，公开版不该让别人翻操作审计）；
#   3) /api/health 瘦身：服务器绝对路径（storage_dir / db_path / 模板目录）一律打码。
# 发布平台若要求监听 0.0.0.0，用 BACKEND_HOST=0.0.0.0 显式打开（默认仍只听本机）。
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
FRONTEND_DIST = _abs_path("FRONTEND_DIST", PROJECT_ROOT / "frontend" / "dist")

_CORS_RAW = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
CORS_ORIGINS: list[str] = [x.strip() for x in _CORS_RAW.split(",") if x.strip()]
# 本项目无账号 / Cookie / 凭据体系 → credentials 恒 False。
# （旧版同时给 allow_origins=["*"] 和 allow_credentials=True，是明确的反模式）
CORS_ALLOW_CREDENTIALS = False

# 本地令牌（可选）：配了之后写操作必须带 X-Local-Token。
# 默认留空 —— 因为默认只监听 127.0.0.1，风险面已经收在本机；
# 要把后端暴露到局域网 / 内网演示时才需要它。
LOCAL_TOKEN = (os.getenv("LOCAL_TOKEN") or "").strip()

# ── 管理员面板：本开源版**不含**任何管理入口 ──────────────────
# 原项目有站点所有者专属的管理面板（口令 + TOTP 动态码 + 回收站 + 全站图片管理）。
# 开源版把这整块**代码层面移除**了（路由、服务模块、管理页、配置项一并摘除）：
#   · 管理入口属于"站点所有者"的私有运维面，不该随代码分发给每一个安装者；
#   · 留着配置项就等于留了一个"看起来能开"的假入口，早晚有人会不小心填上口令开起来。
# 如果你 fork 后想加自己的管理面，请自行实现并**默认关闭**（配置为空即不可用）。

# ── 治理：上传与调用的护栏 ───────────────────────────────────
# 旧版 await file.read() 毫无限制，一个 2GB 文件就能把进程内存打满
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))  # 20MB
ALLOWED_IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
REQUEST_TIMEOUT_SEC = int(os.getenv("REQUEST_TIMEOUT_SEC", "60"))
# 模板工坊的分析类调用（逐图解构 / 视觉卡合成）用更短的超时 ——
# 一次提炼有 9–11 次调用，统一 60s 会让整条链路变成"好几分钟"；分析类出不来就快速失败走降级路径。
ANALYZE_TIMEOUT_SEC = int(os.getenv("ANALYZE_TIMEOUT_SEC", "45"))
# 逐图解构的并发度 —— 解构是 IO 密集（等模型返回），并行能显著降低总耗时。
# ★ 提速专项（10-04）4→6：6 张参考图一批跑完（4 并发要两批），解构墙钟近乎减半；
#   失败有文本解构降级路径兜底，并发压力可控。
FORGE_DECODE_WORKERS = max(1, int(os.getenv("FORGE_DECODE_WORKERS", "6")))

# 单个会话在进程生命周期内最多生成多少张（成本护栏，见方案 §5.3）
# ★ 0 或负数 = **不限额度**（仍会记 used 便于观察用量，但永不拦截）。
#   用户明确要求"额度改为不限额度"——成本护栏改为由用户在服务端自行把握。
MAX_GENERATIONS_PER_SESSION = int(os.getenv("MAX_GENERATIONS_PER_SESSION", "5"))

# ── LLM 通道容错（services/llm.py 消费）────────────────────
#
# ★ 来历（2026-10-03 线上事故）：中转站对本机与线上的 deepseek 通道返回
#   **HTTP 200 + 空的 choices**（`content-type: text/event-stream`，
#   `completion_tokens: 0`）—— 假装成功却一个字不给。Agent 第一步 LLM 调用
#   就拿到空内容，后面的 extract_card / 出图根本到不了，界面表现为
#   「上传了图片却一直生成不了」。同一时刻 premium（gpt-5.5）通道完全正常。
#
#   所以要在一条通道抽风时**自动换通道**：
LLM_FALLBACK_TO_PREMIUM = os.getenv(
    "LLM_FALLBACK_TO_PREMIUM", "1").strip().lower() in ("1", "true", "yes", "on")
# 反向（premium 挂了退到 deepseek）属于**降质**，默认不开。
# 除非你明确接受「关键时刻用便宜模型顶一下」，否则保持 0。
LLM_FALLBACK_DOWNGRADE = os.getenv(
    "LLM_FALLBACK_DOWNGRADE", "0").strip().lower() in ("1", "true", "yes", "on")

# Agent 循环护栏：防止 LLM 死循环烧钱
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "8"))
MAX_TOOL_OBSERVATION_CHARS = int(os.getenv("MAX_TOOL_OBSERVATION_CHARS", "2000"))

# ── 后台任务护栏（infra/tasks.py）──────────────────────────
#
# ★ 存在的理由（2026-10-03 线上事故）：托管平台的反向代理对单个 HTTP 请求
#   有 **60 秒硬超时**（实测 60.099s 返回 504），而「对话 → extract_card
#   （视觉模型）→ 出图」这条链路实测要 1~7 分钟。所以它**必须**改异步：
#   请求毫秒级返回 task_id，前端轮询 —— 让请求和耗时彻底脱钩。
#
#   本机直连没有这层反代，所以这类问题在本地开发中永远测不出来。

# 同时运行的后台任务上限。超了明确 429，而不是让所有人一起变慢
TASK_MAX_RUNNING = max(1, int(os.getenv("TASK_MAX_RUNNING", "4")))
# 单个任务总时限：到点由清扫线程置为失败，防止一个卡死的任务永久占着位
TASK_TIMEOUT_SEC = int(os.getenv("TASK_TIMEOUT_SEC", "1800"))     # 30 分钟
# 终态任务保留多久后清理（内存不能只增不减）
TASK_TTL_SEC = int(os.getenv("TASK_TTL_SEC", "1800"))             # 30 分钟
# 单个任务最多记录多少条中间事件（token 走累积文本，不占这里）
TASK_MAX_EVENTS = int(os.getenv("TASK_MAX_EVENTS", "500"))


class ConfigMissing(RuntimeError):
    """关键配置缺失 —— 调用方应转成 503，而不是让全局兜底变成 500

    ★ 为什么单独定义（审查发现 P1-9）：
      旧实现缺 key 时抛裸 RuntimeError → 冒到全局异常处理器 → 500。
      但「服务没配好」和「服务写错了」是两件完全不同的事：
      前者是 503（依赖未就绪，配好就能用），后者才是 500（程序 bug）。
      混在一起会让前端无法区分「我去填 key」和「我去报 bug」。

      它继承 RuntimeError 是为了不破坏任何既有 except RuntimeError 的调用方。
    """

    def __init__(self, what: str, hint: str = ""):
        self.what = what
        self.code = "config_missing"
        super().__init__(hint or f"{what} 未配置")


def _require(cond: bool, what: str) -> None:
    """缺关键配置时给出可读指引"""
    if not cond:
        raise ConfigMissing(
            what,
            f"{what} 未配置。请在 {ENV_PATH} 中设置，或从 .env.example 复制一份后填入。",
        )


def assert_ready_for_generation() -> None:
    """要花钱调用前才检查 —— 保证纯预览 / 只读接口在无密钥时依然可用"""
    if IMAGE_BACKEND == "openai":
        _require(bool(IMAGE_API_KEY), "IMAGE_API_KEY")
        _require(bool(IMAGE_MODEL), "IMAGE_MODEL")
    else:
        _require(bool(SEEDREAM_API_KEY), "SEEDREAM_API_KEY")
        _require(bool(SEEDREAM_MODEL), "SEEDREAM_MODEL")


def assert_ready_for_llm() -> None:
    _require(bool(DEEPSEEK_API_KEY), "DEEPSEEK_API_KEY")
    _require(bool(DEEPSEEK_MODEL), "DEEPSEEK_MODEL")


def public_dict() -> dict:
    """给 /api/health 的脱敏视图 —— 只说有没有配，绝不说配了什么"""
    return {
        "seedream_key_set": bool(SEEDREAM_API_KEY),
        "seedream_model_set": bool(SEEDREAM_MODEL),
        "llm_key_set": bool(DEEPSEEK_API_KEY),
        "llm_model_set": bool(DEEPSEEK_MODEL),
        "vision_model_set": bool(VISION_MODEL),
        "env_file_found": ENV_PATH.exists(),
        "storage_dir": str(STORAGE_DIR),
        "templates_dir": str(TEMPLATES_DIR),
        "storage_warnings": STORAGE_WARNINGS,
    }
