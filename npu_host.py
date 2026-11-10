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
import subprocess
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


def find_python():
    """优先用 .venv 的解释器；环境没装好时退回系统 Python（doctor.py 是纯标准库）。"""
    venv_py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    if os.path.exists(venv_py):
        return [venv_py]
    import shutil
    for exe, pre in (("py", ["-3"]), ("python", []), ("python3", [])):
        full = shutil.which(exe)
        if full and "windowsapps" not in full.lower():
            return [full] + pre
    return [sys.executable]


def run_doctor(fix: bool = False, with_model: bool = False) -> dict:
    """跑一次环境体检（可带修复），返回 doctor.py 的 JSON。"""
    args = find_python() + [os.path.join(ROOT, "doctor.py"), "--json"]
    if fix:
        args.append("--fix")
    if with_model:
        args.append("--with-model")
    try:
        out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                             timeout=5400, encoding="utf-8", errors="replace")
        for line in reversed((out.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"ok": False, "error": ((out.stderr or out.stdout) or "")[-500:]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


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
        # 启动前先看运行环境在不在；不在就明确告诉插件「需要装环境」，
        # 而不是让用户看到一个没头没尾的 "服务未连接"。
        if not os.path.exists(os.path.join(ROOT, ".venv", "Scripts", "python.exe")):
            return {
                "ok": False, "needSetup": True,
                "error": "运行环境还没装：缺少 .venv",
                "hint": "点「检查运行环境」可自动安装，或双击 setup.bat",
            }
        # 宿主是被浏览器拉起来的 —— 顺着进程树往上找浏览器主进程，
        # 交给服务当「主人」：浏览器一关，服务连同 llama-server 一起退出。
        owner = 0
        try:
            import winproc
            owner = winproc.find_browser_pid()
        except Exception:
            owner = 0
        if owner:
            _log(f"ensure: owner browser pid={owner}")
        res = sd.ensure(device=device, port=port, owner_pid=owner)
        if owner:
            res["owner_pid"] = owner
        return res

    if cmd == "stop":
        return sd.stop(port=port)

    if cmd == "doctor":
        # 插件弹窗里的「检查运行环境 / 一键修复」
        return run_doctor(fix=bool((msg or {}).get("fix")),
                          with_model=bool((msg or {}).get("withModel")))

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
