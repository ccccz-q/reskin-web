"""契约层 · 工具 Schema —— 单一真源

为什么工具 Schema 要单独一层
---------------------------
Claude Code 那类 harness 的关键设计：**工具的能力边界写在契约里，而不是散在调用处**。
好处有三：

  1. LLM 看到的 Schema、 dispatch 用的校验、前端要生成的 UI，三者天然一致
  2. 新增工具只改这一处，Agent 自动获得新能力（这正是 Tool-use Loop 的价值所在）
  3. Schema 可以被单元测试穷举校验，避免"模型调用了一个不存在的参数"

本项目没有 LangGraph，Tool-use Loop 是自建的（方案 §0 决策 5），
所以这份契约就是 Agent 的"API 文档"，而不是某个框架自动生成的副产品。
"""
from __future__ import annotations

from typing import Any

TOOL_SPEC_VERSION = "1.0"


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_families",
            "description": "列出所有可用的创作家族（视觉风格流派）。"
                           "用户描述模糊、或你想推荐风格时先调用它。"
                           "它只返回 id/名称/简介，不返回完整参数表。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_family",
            "description": "查看某个家族的完整参数表与必填项。"
                           "渲染提示词之前必须据此确认参数是否齐全。",
            "parameters": {
                "type": "object",
                "properties": {
                    "family_id": {
                        "type": "string",
                        "description": "家族 id，如 full_restyle / zine / portrait_epic",
                    }
                },
                "required": ["family_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_prompt",
            "description": "渲染提示词预览。**不调用生图模型、不花钱、毫秒级返回**。"
                           "每次调参后都应该先看它，确认创意方向对了再生成。",
            "parameters": {
                "type": "object",
                "properties": {
                    "family_id": {"type": "string", "description": "家族 id"},
                    "params": {
                        "type": "object",
                        "description": "参数键值对。必须是 describe_family 返回过的合法取值。",
                    },
                },
                "required": ["family_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "真正调用生图模型产生一张图。**会花钱**，"
                           "必须在 render_prompt 确认过提示词之后再调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "family_id": {"type": "string", "description": "家族 id"},
                    "params": {"type": "object", "description": "参数键值对"},
                },
                "required": ["family_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_image",
            "description": "对**刚生成的那张图**做最小外科修复：只改用户指出的漂移，"
                           "其余与参考图（上一版成品）保持完全一致。**会花钱**。"
                           "使用条件：① 用户对最近一次生成结果明确不满意；"
                           "② 用户能指出具体哪里不对（如『树冠变成圆球了，要方块体素』）。"
                           "两者缺一则不要调用 —— 用户泛泛说『不好看』时，"
                           "应该先追问具体哪里不对，或建议换参数重新 generate_image。",
            "parameters": {
                "type": "object",
                "properties": {
                    "change": {
                        "type": "string",
                        "description": "要修正的**一处**具体问题（用户原话的凝练，一句话）。"
                                       "例：「树冠恢复成方块体素拼接，不要圆润球体」。"
                                       "一次只修一个变量 —— 想改多处就多次调用。",
                    },
                },
                "required": ["change"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_card",
            "description": "从原图提炼「创作卡」：主体是什么、有哪些可视觉化的结构锚点、"
                           "原图主色板、以及二次创作时最容易失真的地方。"
                           "**渲染提示词的保真能力直接取决于它** —— "
                           "没有卡片时，forbid 段只能靠模板里写死的通用约束。"
                           "所以：在你准备出图之前，只要还没提取过，就应该先调用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "use_vlm": {
                        "type": "boolean",
                        "description": "是否额外调用视觉模型来识别主体与锚点。"
                                       "false 只做本地取色（更便宜但拿不到主体信息）。"
                                       "默认 true。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_image_info",
            "description": "读取已上传原图的尺寸、宽高比与朝向，"
                           "用于判断适合哪种画幅。竖图别选横画幅家族。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# 需要真实花钱 / 需要用户确认的工具
SPEND_TOOLS = frozenset({"generate_image", "repair_image"})

# ★ 条件计费工具：参数决定它到底花不花钱。
#   `extract_card(use_vlm=false)` 只做本地 Pillow 取色（0 成本），
#   但 `use_vlm=true` 会真实调用视觉模型。
#
#   为什么单列一张表：第一版把它当普通工具，于是**预览模式（allow_spend=False）
#   下 Agent 照样能调它并真实花钱** —— 直接破坏了「预览 0 成本」的对外承诺，
#   而且这笔钱不进入 MAX_GENERATIONS_PER_SESSION 计数，是无上限的。
#   放在这里之后，dispatch 会按参数判定并强制降级（见 tools/registry.py）。
CONDITIONAL_SPEND_TOOLS = {
    "extract_card": ("use_vlm", True),   # (决定花钱的参数名, 花钱时的取值)
}

TOOL_NAMES = frozenset(
    t["function"]["name"] for t in TOOLS
)


def costs_money(name: str, arguments: dict | None = None) -> bool:
    """这次调用会不会产生真实费用

    无条件计费工具（generate_image）恒 True；
    条件计费工具看参数；其余 False。
    """
    if name in SPEND_TOOLS:
        return True
    spec = CONDITIONAL_SPEND_TOOLS.get(name)
    if not spec:
        return False
    param, spend_value = spec
    args = arguments or {}
    return bool(args.get(param, spend_value)) is bool(spend_value)


def openai_tools(enabled: set[str] | frozenset[str] | None = None) -> list[dict]:
    """返回 OpenAI 格式的 tools 列表

    enabled=None 时给全部。做能力裁剪时用 enabled 控制——
    比如预览模式下屏蔽 generate_image，从 Schema 层面就杜绝误花钱。
    """
    if enabled is None:
        return [dict(t) for t in TOOLS]
    return [dict(t) for t in TOOLS if t["function"]["name"] in enabled]


def validate_call(name: str, arguments: dict) -> list[str]:
    """按 Schema 校验一次工具调用的参数

    返回错误列表，空列表表示合法。LLM 是会填错参数名的，这道校验把
    "模型填了 typo 的参数" 从运行时崩溃变成一条可被模型看见并纠正的观察结果。
    """
    spec = next((t for t in TOOLS if t["function"]["name"] == name), None)
    if spec is None:
        return [f"未知工具 {name!r}，可用工具：{sorted(TOOL_NAMES)}"]

    props = spec["function"]["parameters"].get("properties", {})
    required = set(spec["function"]["parameters"].get("required", []))

    errs: list[str] = []
    for key in arguments:
        if key not in props:
            errs.append(f"工具 {name} 不接受参数 {key!r}，接受的是 {sorted(props)}")
    for key in sorted(required - set(arguments)):
        errs.append(f"工具 {name} 缺少必需参数 {key!r}")
    return errs
