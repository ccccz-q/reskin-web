"""context_store 完整验收 —— 不花任何真实 API 调用"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# 让本文件既能被 pytest 收集，也能 python xxx.py 直接运行：
# 把 backend/app 加进 import root（与 uvicorn main:app 的约定一致）
_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import services.context_store as cs  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


def fts_equal_msg(path):
    conn = sqlite3.connect(str(path))
    try:
        a = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        b = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return a == b, a, b
    finally:
        conn.close()


tmpdir = tempfile.mkdtemp(prefix="ctx_")


def reset_to(path):
    cs.SQLITE_PATH = Path(path)


print("=== A. 全新库：读写 / 双路召回 / 清空 ===")
pa = Path(tmpdir) / "a.db"
reset_to(pa)
cs.append_message("t", "user", "我想要一张雪山的小人国插画，暖色调")
cs.append_message("t", "assistant", "好的，我先看看可用风格")
cs.append_message("t", "tool", '{"families":["second_world"]}',
                  tool_call_id="c1", tool_name="list_families")
cs.append_message("t", "user", "再谈谈 yesterday 的 sunset 参数")

for q in ("雪山", "小人国", "参数", "插画", "sunset", "可用风格"):
    check(f"双路召回 {q}", len(cs.search("t", q)) > 0)

check("空查询返回空", cs.search("t", "") == [])
check("FTS 语法字符不炸", isinstance(cs.search("t", '"*('), list))
check("不存在词返回空", cs.search("t", "zzzz_no_such") == [])

h = cs.history("t")
check("history 条数=4", len(h) == 4, f"实际 {len(h)}")
check("history 保序 user 打头", h[0]["role"] == "user" and "雪山" in h[0]["content"])
check("tool 消息带 tool_call_id", h[2].get("tool_call_id") == "c1")
check("tool 消息带 name", h[2].get("name") == "list_families")
check("system 不入历史表", all(m["role"] != "system" for m in h))

check("未知 role 应报错", raises(lambda: cs.append_message("t", "bogus", "x")))

print()
print("=== B. 长期记忆 + 计数器 ===")
cs.save_long_term("t", "用户在做旅行图二次创作，偏暖色", {"喜欢的风格": ["小人国"]})
lt = cs.load_long_term("t")
check("长期摘要可读", "暖色" in lt["summary"])
check("偏好 dict 正确", lt["prefs"].get("喜欢的风格") == ["小人国"])
check("跨会话摘要", len(cs.all_summaries()) >= 1)
c0 = cs.get_counter("gen:t")
v = cs.bump_counter("gen:t", 2)
check("计数器自增 2", v == c0 + 2, f"{c0} -> {v}")
cs.reset_counter("gen:t")
check("计数器清零", cs.get_counter("gen:t") == 0)

print()
print("=== C. 索引自愈：手动制造失步 ===")
# 假装触发器和外部索引失步
conn = sqlite3.connect(str(pa))
conn.execute("DELETE FROM messages_fts")       # 索引掏空，messages 还在
conn.commit()
sync_cnt = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()[0]
conn.execute("INSERT INTO messages_fts(rowid, content, thread_id) "
             "SELECT -9999, 'dummy', 't'")     # 塞一条不在 messages 里的脏数据
conn.commit()
conn.close()
check("失步已制造", sync_cnt > 0)
cs.init_db()                                    # 应自动检测并重建
eq, a, b = fts_equal_msg(pa)
check("自愈后 FTS 与 messages 一致", eq, f"fts={a} msg={b}")

print()
print("=== D. 旧 unicode61 库迁移到 trigram ===")
pb = Path(tmpdir) / "b.db"
c = sqlite3.connect(str(pb))
c.execute("PRAGMA journal_mode=WAL")
c.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT,"
          " role TEXT, content TEXT, tool_call_id TEXT, tool_name TEXT, ts TEXT, meta TEXT)")
c.execute("CREATE TABLE long_term(thread_id TEXT PRIMARY KEY, summary TEXT,"
          " prefs TEXT, updated TEXT)")
c.execute("CREATE TABLE session_counter(key TEXT PRIMARY KEY, value INTEGER, updated TEXT)")
c.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, thread_id UNINDEXED,"
          " content='messages', content_rowid='id', tokenize='unicode61')")
c.execute("CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN"
          " INSERT INTO messages_fts(rowid, content, thread_id)"
          " VALUES (new.id, new.content, new.thread_id); END")
c.execute("CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN"
          " INSERT INTO messages_fts(messages_fts, rowid, content, thread_id)"
          " VALUES('delete', old.id, old.content, old.thread_id); END")
c.commit()
c.close()
reset_to(pb)
cs.init_db()
chk = sqlite3.connect(str(pb)).execute("PRAGMA integrity_check").fetchone()[0]
check("迁移后 integrity ok", chk == "ok", chk)

print()
print("=== E. 真实库健康度（只读，绝不写入）===")
# ⚠️ 这里曾经把 cs.SQLITE_PATH 切回真实生产库再调 stats()，
#    而 stats() 会触发 init_db()（含 _self_heal_fts 的 DROP + 重建）。
#    也就是说「跑一次自测就会动一次生产库」—— 测试绝不该有这种副作用。
#    现在只做只读的 integrity_check，不 reset_to。
real = Path(__file__).resolve().parents[2] / "storage" / "sqlite" / "agent.db"
if real.exists():
    conn = sqlite3.connect(str(real))
    try:
        chk = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    check("真实库 integrity ok", chk == "ok", chk)
    check("真实库已建表", "messages" in tables, str(tables[:5]))
else:
    print("   （生产库还不存在，跳过）")

print()

print()
print("=== F. ★ 历史回放的 tools 协议配对（复审 P0-2）===")
# 真实会话会产生两种「不配对」状态，都是正常的、不是数据损坏：
#   ⓐ 窗口截断把某轮开头切掉 → assistant 带 tool_calls 的那条不在窗口里
#   ⓑ 断连/熔断提前中止 → 声明了 3 个 tool_calls 只执行了 2 个
# 直接喂给模型会让请求体不合法（服务端 400）。
ptid = "proto"
cs.clear_thread(ptid)


def ok_pairs(msgs):
    """校验 OpenAI tools 协议：每个 tool_call_id 有且只有一条 tool 回复"""
    pending = set()
    answered = []
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending |= {tc["id"] for tc in m["tool_calls"]}
        if m.get("role") == "tool":
            answered.append(m.get("tool_call_id"))
    return pending == set(answered) and len(answered) == len(set(answered))


# 完整的一轮：1 个 assistant 带 2 个 tool_calls + 2 条 tool 回复
cs.append_message(ptid, "user", "帮我选个风格")
cs.append_message(ptid, "assistant", None,
                  meta={"tool_calls": [
                      {"id": "a1", "name": "list_families", "arguments": {}},
                      {"id": "a2", "name": "read_image_info", "arguments": {}},
                  ]})
cs.append_message(ptid, "tool", '{"ok":1}', tool_call_id="a1", tool_name="list_families")
cs.append_message(ptid, "tool", '{"ok":2}', tool_call_id="a2", tool_name="read_image_info")
cs.append_message(ptid, "assistant", "我建议用 zine。")

h = cs.history_for_llm(ptid)
check("完整轮次：协议配对正确", ok_pairs(h), str([m.get("role") for m in h]))
check("★ assistant 的 tool_calls 被还原出来",
      any(m.get("tool_calls") for m in h),
      "旧实现丢掉 meta.tool_calls → assistant 变成无 content 无 tool_calls 的空壳")
check("tool_calls 是 OpenAI 形状",
      all(set(tc) >= {"id", "type", "function"} for m in h
          for tc in (m.get("tool_calls") or [])))
check("arguments 是 JSON 字符串",
      all(isinstance(tc["function"]["arguments"], str) for m in h
          for tc in (m.get("tool_calls") or [])))

# 场景 ⓑ：中止 —— 声明 3 个只回了 1 个
cs.clear_thread(ptid + "b")
cs.append_message(ptid + "b", "user", "看看这张图")
cs.append_message(ptid + "b", "assistant", None,
                  meta={"tool_calls": [
                      {"id": "b1", "name": "list_families", "arguments": {}},
                      {"id": "b2", "name": "extract_card", "arguments": {}},
                      {"id": "b3", "name": "render_prompt", "arguments": {}},
                  ]})
cs.append_message(ptid + "b", "tool", '{"ok":1}', tool_call_id="b1", tool_name="list_families")
cs.append_message(ptid + "b", "assistant", "（已中止：客户端断开连接）")

hb = cs.history_for_llm(ptid + "b")
check("中止轮次：协议被修好", ok_pairs(hb), str([m.get("role") for m in hb]))
check("孤立的 tool 被丢掉", not any(m.get("role") == "tool" for m in hb),
      str([m.get("role") for m in hb]))
check("不完整的 tool_calls 降级成普通文本",
      any(m.get("role") == "assistant" and "中止" in (m.get("content") or "") for m in hb),
      str(hb))

# 展示用的 history() 不做净化 —— 界面应如实呈现库里的内容
raw = cs.history(ptid)
check("展示用 history 保留孤立 tool 行",
      any(m.get("role") == "tool" for m in raw),
      f"{len(raw)} 条")

print()
print("=== G. 票据表（治理层依赖）===")
cs.add_reservation("tok-abc", "th1")
check("写入后可读回", cs.peek_reservation("tok-abc") is not None)
check("首次兑现成功", cs.consume_reservation("tok-abc") is True)
check("二次兑现失败（一次性）", cs.consume_reservation("tok-abc") is False)
check("兑现后查不到", cs.peek_reservation("tok-abc") is None)
cs.add_reservation("tok-old", "th1")
check("按 TTL 能筛出残票", len(cs.list_reservations(older_than_sec=0)) >= 1)
check("未给 TTL 时全部返回", len(cs.list_reservations()) >= 1)
cs.consume_reservation("tok-old")
check("票据统计可读", "pending" in cs.reservations_stats())


# ★ 必须用退出码报告失败 —— 只 print 不 exit 的话，
#   run_all.py 永远看到 returncode=0，整套自测就会假绿。
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
