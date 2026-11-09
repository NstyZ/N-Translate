"""
npu_host.py —— Chrome Native Messaging 宿主

作用：让浏览器插件能够"内置地"启动/检查本地翻译服务，无需手动双击 bat。

协议（Chrome 标准）：stdin/stdout 上以 4 字节小端长度前缀 + UTF-8 JSON 交换消息。
  Chrome -> 宿主 : {"cmd": "ensure" | "status" | "ping"}
  宿主 -> Chrome : {"ok": true, "state": "...", "health": {...}}

注意：stdout 只能输出协议帧，任何调试信息都必须写文件（npu_host.log）。
"""
import json
import os
import struct
import sys
import traceback

import server_daemon as sd

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "npu_host.log")


def _log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


def read_msg():
    """读一条消息；EOF 返回 None。"""
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length == 0:
        return None
    body = sys.stdin.buffer.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def send_msg(obj) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def handle(msg: dict) -> dict:
    cmd = (msg or {}).get("cmd", "")
    device = (msg or {}).get("device") or "NPU"
    port = int((msg or {}).get("port") or 8765)

    if cmd == "ping":
        return {"ok": True, "state": "pong"}

    if cmd == "status":
        h = sd.health(port)
        return {"ok": bool(h), "state": "running" if h else "stopped", "health": h, "port": port}

    if cmd == "ensure":
        return sd.ensure(device=device, port=port)

    if cmd == "stop":
        return sd.stop(port=port)

    if cmd == "update":
        # 热更新：下载仓库源码包 → 停服务 → 覆盖源码（保留模型/运行时/设置）→ 重启服务
        import subprocess
        import updater

        urls = (msg or {}).get("urls") or []
        url = (msg or {}).get("url") or ""
        args = [sys.executable, os.path.join(ROOT, "updater.py"), "--json"]
        if url:
            args += ["--url", url]
        for u in urls[1:]:
            args += ["--mirror", u]
        try:
            out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                                 timeout=900, encoding="utf-8", errors="replace")
            tail = (out.stdout or "").strip().splitlines()
            for line in reversed(tail):
                line = line.strip()
                if line.startswith("{"):
                    return json.loads(line)
            return {"ok": False, "error": (out.stderr or "更新进程没有返回结果")[-400:]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {"ok": False, "error": f"unknown cmd: {cmd!r}"}


def main() -> int:
    _log("--- host started ---")
    while True:
        try:
            msg = read_msg()
        except Exception as e:
            _log(f"read error: {e}")
            return 1
        if msg is None:
            _log("stdin closed, exiting")
            return 0
        try:
            reply = handle(msg)
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            _log("handle error:\n" + traceback.format_exc())
        try:
            send_msg(reply)
        except Exception as e:
            _log(f"write error: {e}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
