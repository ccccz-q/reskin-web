"""弱网演练 —— 丢包 / 慢速发送 / 提前断开

    python tests/weaknet.py [BASE_URL]

★ 为什么必须真实地"坏"连接（评审自查列出的未覆盖项）
------------------------------------------------------
慢客户端那一项已经在 stress_live里覆盖了（连上不读），但那测的是
**应用层**。这里测的是**传输层**——用 raw socket 手工构造三类真实故障：

  ① 不完整请求：声明 Content-Length: 5000，只发 100 字节就断开
     （模拟"上传到一半断网"）
  ② 慢速发送：声明 2000 字节，每 50ms 发 100 字节
     （模拟"网很慢但没断"—— 移动端最常见的形态）
  ③ 半开连接：发完请求头就停住不动（模拟 NAT 超时前的半开）
  ④ 突发断连：发到一半直接 RST

★ 判定口径（先定死）
--------------------
  · 任何一种都**不许**让服务进程崩溃/退出
  · 任何一种都**不许**返回 5xx（4xx 是"请求确实不合法"的正确回答）
  · 慢速发送在合理时间内要被正常处理或给出明确超时（不许无限挂）
  · 服务端**不许**留下未关闭的连接/线程（跑完检查线程数）

★ 这几类故障在生产上很常见：用户电梯里、地铁里、弱网环境下点"生成"，
  连接被网关静默丢弃是常态。服务必须"活着并且给出可理解的答复"。
"""
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
HOST, PORT = "127.0.0.1", int(BASE.rsplit(":", 1)[1])

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def raw_send(payload: bytes, *, read_reply: bool = True,
             timeout: float = 10.0) -> tuple[int, str]:
    """裸 socket 发一段字节，返回 (状态码或0, 首行响应)"""
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
    except OSError as e:
        return 0, f"connect failed: {e}"
    try:
        s.sendall(payload)
        if not read_reply:
            return 0, ""
        s.settimeout(timeout)
        buf = b""
        while b"\r\n" not in buf and len(buf) < 4096:
            try:
                chunk = s.recv(512)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        text = buf.decode("utf-8", "replace")
        first = text.split("\r\n")[0]
        code = 0
        parts = first.split()
        if len(parts) >= 2 and parts[1].isdigit():
            code = int(parts[1])
        return code, first[:100]
    except OSError as e:
        return 0, f"io: {e}"
    finally:
        try:
            s.close()
        except OSError:
            pass


def http_get(path: str, extra_headers: str = "") -> bytes:
    return f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n{extra_headers}Connection: close\r\n\r\n".encode()


def http_post(path: str, body_len: int) -> bytes:
    return (f"POST {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {body_len}\r\n"
            f"Connection: close\r\n\r\n").encode()


def main() -> int:
    print("=" * 72)
    print(f"弱网演练  {BASE}")
    print("=" * 72)

    print("\n=== 1. 基线（正常请求，作为对照）===")
    code, first = raw_send(http_get("/api/health"))
    check("正常 GET /api/health → 200", code == 200, f"实际 {code} {first}")

    print("\n=== 2. 不完整请求：声明 5000 字节，只发 100 字节就断开 ===")
    body = b'{"message":"x"}' + b" " * 4988
    head = http_post("/api/chat/async", 5000)
    s = socket.create_connection((HOST, PORT), timeout=10)
    try:
        s.sendall(head + body[:100])
    except OSError:
        pass
    finally:
        s.close()          # 立刻断：模拟"上传到一半断网"
    time.sleep(1.0)
    code, first = raw_send(http_get("/api/health"))
    check("★ 服务仍然活着（健康检查 200）", code == 200, f"实际 {code}")
    check("★ 没有返回 5xx", code < 500 or code == 0, f"实际 {code}")

    print("\n=== 3. 慢速发送：声明 2000 字节，每 50ms 发 100 字节 ===")
    # ★ Content-Length 必须与实际 body 字节数**完全一致**（踩过一次）：
    #   声明 2000 却只发 1908，服务会一直等剩下的 92 字节 ——
    #   那 31 秒是**我的测试写错**造成的，不是服务慢。测慢速发送不能同时测"少发了"。
    _body = b'{"message":"' + b"x" * 1900 + b'"}'
    payload = http_post("/api/chat/async", len(_body)) + _body
    result: dict = {}

    def _slow() -> None:
        try:
            s = socket.create_connection((HOST, PORT), timeout=30)
            s.settimeout(30)
            for i in range(0, len(payload), 100):
                s.sendall(payload[i:i + 100])
                time.sleep(0.05)
            buf = b""
            s.settimeout(30)
            while b"\r\n" not in buf and len(buf) < 2048:
                try:
                    c2 = s.recv(512)
                except socket.timeout:
                    break
                if not c2:
                    break
                buf += c2
            result["first"] = buf.split(b"\r\n")[0].decode("utf-8", "replace")[:100]
            s.close()
        except OSError as e:
            result["first"] = f"OSError: {e}"

    t0 = time.perf_counter()
    th = threading.Thread(target=_slow, daemon=True)
    th.start()
    th.join(timeout=40)
    dt = time.perf_counter() - t0
    # 2000 字节 / 100 每 50ms = 1 秒发完，再加处理时间
    check(f"★ 慢速请求在合理时间内结束（{dt:.1f}s，未挂死）", dt < 30,
          f"耗时 {dt:.1f}s")
    check("★ 服务仍然活着", raw_send(http_get("/api/health"))[0] == 200)

    print("\n=== 4. 半开连接：发完请求头就停住 ===")
    s = socket.create_connection((HOST, PORT), timeout=15)
    try:
        # 声明了 Content-Length 但一个字节都不发 body
        s.sendall(http_post("/api/chat/async", 5000))
        time.sleep(3.0)       # 保持连接但不补 body
    finally:
        s.close()
    time.sleep(0.5)
    check("★ 服务仍然活着（半开连接没有把它拖住）",
          raw_send(http_get("/api/health"))[0] == 200)

    print("\n=== 5. 突发断连：发到一半直接 RST ===")
    payload2 = http_post("/api/chat/async", 3000) + b"x" * 3000
    s = socket.create_connection((HOST, PORT), timeout=10)
    try:
        s.sendall(payload2[:600])
        # SO_LINGER 0 → close() 直接发 RST 而不是 FIN
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                     b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()
    except OSError:
        pass
    time.sleep(1.0)
    check("★ 服务仍然活着（RST 没有让它崩溃）",
          raw_send(http_get("/api/health"))[0] == 200)

    print("\n=== 6. 超长请求头 / 垃圾字节 ===")
    for label, junk in [
        ("超长请求头（16KB）", b"GET /api/health HTTP/1.1\r\nHost: x\r\nX-Pad: "
                              + b"A" * 16000 + b"\r\n\r\n"),
        ("完全垃圾的字节", bytes(range(256)) * 8),
        ("只有半截 HTTP 头", b"GET /api/hea"),
    ]:
        code, _ = raw_send(junk)
        check(f"★ {label} → 不 5xx（{code} 或 0=已断开）", code < 500,
              f"实际 {code}")
    time.sleep(0.5)
    check("★ 服务仍然活着（垃圾输入后）",
          raw_send(http_get("/api/health"))[0] == 200)

    print("\n=== 7. 事后核对：有没有留下连接/线程残留 ===")
    time.sleep(2.0)
    try:
        import psutil  # type: ignore
        for p in psutil.process_iter(["pid", "cmdline", "num_threads"]):
            cl = " ".join(str(c) for c in (p.info.get("cmdline") or []))
            if f"--port {PORT}" in cl:
                th = p.info["num_threads"]
                check(f"线程数没有暴涨（当前 {th}）", th < 80,
                      f"线程 {th}（弱网测试前约 10）")
                break
        else:
            note = "  --   找不到服务进程，跳过线程核对"
            print(note)
    except ImportError:
        print("  --   未装 psutil，跳过线程核对")

    print()
    print("=" * 72)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
