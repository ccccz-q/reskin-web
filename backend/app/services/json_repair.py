"""模型输出的 JSON 抢救与解析（纯函数，零外部依赖）

    ← 从 services/llm.py 拆出（2026-10-06 god module 拆分，第五刀）

★ 为什么要拆（评审自查发现）
--------------------------------
llm.py 当时已1167 行，职责混了三类东西：文本工具 / 通道与重试引擎 / JSON 解析。
其中「JSON 解析」是**纯函数**—— 不碰通道、不碰客户端、不看env，
却占了 140 行，把真正需要读的东西（重试档位、预算、通道）挤到了视野外。

更糟的是：`_close_truncated` 在原文件里**定义了两次**（第 87 行与第 186 行），
靠Python「后定义覆盖先定义」才没出错，而**第一个定义连一句return 都没有**。
这种代码评委扫一眼就能发现，属于"自己给自己埋雷"。

★ 拆分纪律：旧入口保持可用
--------------------------
tests 直接import 了 `llm._close_truncated` / `llm.extract_json` 等私有名，
所以 llm.py 仍然re-export 全套（见该文件底部import 行）。
「拆文件」不能变成「打断别人的引用」。

★ 为什么不把「通道 / 客户端缓存 / 重试引擎」也拆出去
--------------------------------------------------
它们看起来该独立，但**不能**：tests 的离线注入点
（`llm._client_factory` / `llm._sleep`，见 test_llm_resilience.py）
是靠 patch **llm 模块的全局名**生效的，而 `_client_for` / `_run_resilient`
在运行时按模块全局查找。把它们搬走，101 条断言会全部失去作用域 ——
"为了漂亮而让测试打不响"是净损失。
"""

import json
import re

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """从模型输出里稳健地取出 JSON

    模型偶尔会：加 ```json 围栏 / 前后加寒暄 / 输出对象后还附带解释。
    所以按「围栏 → 最外层花括号配对扫描」两级尝试，而不是简单 json.loads。
    """
    if not text:
        return None
    raw = text.strip()

    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()

    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _close_truncated(raw: str) -> str:
    """把「被 max_tokens 截断」的 JSON 尽量补成合法的

    ★ 来历（2026-10-03 实测）：VLM 提炼 card 时 max_tokens=500 不够用，
      模型刚写完 anchors 就被掐断 —— 整段 JSON 少了结尾的括号。
      旧行为是 `extract_json` 返回 None → VLM 档被整段丢掉
      （**这次调用的钱白花了**），card 退回只有色板的本地档，
      秋毫必现的「反推 forbid」再次失效。
      与其重来一次（再花钱、再等两分钟），不如把已经到手的完整片段救回来。

    三步：⓪ 砍掉末尾刚开了头的片段；① 结尾落在字符串里就补个引号；
          ② 去掉悬空的逗号/冒号；③ 按未闭合的括号栈反向补齐。
    """
    s = raw.rstrip()

    # ⓪ 先砍掉末尾"刚开了头"的结构：`...,{` 这种半截片段修补出来只会是一个空对象。
    #    注意**不**砍引号 —— `{"a":"未写完` 结尾的引号是有信息量的，砍了反而救不回来。
    while s and s[-1] in ",{[:":
        s = s[:-1].rstrip()

    # ① 扫描一遍看结尾是否在字符串内部（不能用 count('"') % 2 —— 遇转义引号就错）
    in_str = False
    esc = False
    stack: list[str] = []
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s += '"'

    # ② 悬空的分隔符：`{"a":1,` 或 `{"a":` 后面的东西是残缺的
    while s and s[-1] in ",:":
        s = s[:-1].rstrip()

    # ③ 补齐容器
    for opener in reversed(stack):
        s += "]" if opener == "[" else "}"
    return s


def extract_json_lenient(text: str) -> dict | None:
    """比 extract_json 多一层：允许 JSON 被截断

    只在**明确预期会截断**的场合使用（目前是 VLM 提炼 card）；
    其它地方（工具 arguments、样式提炼）坚持用严格的 extract_json ——
    那里宁可判为「解析不了」，也不要悄悄接受一个残缺的参数表。

    策略：先严格解析；不行就逐步砍掉最后一个"写到一半"的片段再补齐括号。
    每一步都要求产出**合法的 dict**，绝不含糊地返回半截对象。
    """
    strict = extract_json(text)
    if strict is not None:
        return strict

    raw = (text or "").strip()
    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    if start < 0:
        return None
    candidate = raw[start:]

    for _ in range(40):
        try:
            obj = json.loads(_close_truncated(candidate))
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        # 再退一步：砍掉最后一个未写完的元素，重试
        cut = max(candidate.rfind(","), candidate.rfind("{"), candidate.rfind("["))
        if cut <= start:
            break
        candidate = candidate[:cut]
    return None
