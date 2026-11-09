"""
server_daemon.py —— 本地翻译服务的「按需启动」工具

  * 服务已在运行  -> 直接返回
  * 未在运行      -> 用 pythonw 以**无窗口**方式在后台拉起，并轮询等待就绪
  * 日志写入 E:\\ai\\dsh\\server.log

既能当命令行工具用，也被 npu_host.py（Chrome Native Messaging 宿主）导入复用。

用法:
  python server_daemon.py                 # 启动（默认 NPU / 8765）
  python server_daemon.py --json          # 输出 JSON 结果
  python server_daemon.py --device CPU --port 8765
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, ".venv", "Scripts")
SERVER = os.path.join(ROOT, "server.py")
LOG = os.path.join(ROOT, "server.log")

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def health(port: int, timeout: float = 2.0):
    """服务健康检查；未运行或异常时返回 None。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _python_exe() -> str:
    """优先用 pythonw.exe：GUI 子系统，不会弹控制台窗口。"""
    for name in ("pythonw.exe", "python.exe"):
        p = os.path.join(VENV, name)
        if os.path.exists(p):
            return p
    return sys.executable


def ensure(device: str = "NPU", port: int = 8765, wait: float = 90.0) -> dict:
    """确保服务在运行；返回 {ok, state, health?, error?}"""
    h = health(port)
    if h and h.get("ready") is not False:
        return {"ok": True, "state": "already-running", "health": h, "port": port}
    if h:
        # 进程在但模型还在编译：等它，别重复拉起（端口已被占，第二个实例只会报错退出）
        t0 = time.time()
        while time.time() - t0 < wait:
            h = health(port)
            if h and h.get("ready") is not False:
                return {"ok": True, "state": "already-loading", "health": h,
                        "port": port, "elapsed": round(time.time() - t0, 1)}
            if h is None:
                break                        # 进程挂了，走正常启动流程
            time.sleep(0.3)

    exe = _python_exe()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    # 仅当工作区内存在这些目录时才注入（打包分发的目录里没有，保持干净）
    pysite = os.path.join(ROOT, "pysite")
    if os.path.isdir(pysite):
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = pysite + (os.pathsep + prev if prev else "")
    hf_home = os.path.join(ROOT, ".hf")
    if os.path.isdir(hf_home):
        env["HF_HOME"] = hf_home
        env["HF_ENDPOINT"] = "https://hf-mirror.com"

    try:
        # 日志追加到 server.log；stdin 给 DEVNULL，彻底脱离调用方
        log = open(LOG, "ab", buffering=0)
        subprocess.Popen(
            [exe, SERVER, "--device", device, "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as e:
        return {"ok": False, "state": "spawn-failed", "error": f"{type(e).__name__}: {e}"}

    t0 = time.time()
    while time.time() - t0 < wait:
        h = health(port)
        # 端口一绑上 /health 就能响应，所以还要等 ready（模型编译完）才算启动成功
        if h and h.get("ready") is not False:
            return {"ok": True, "state": "started",
                    "health": h, "port": port, "elapsed": round(time.time() - t0, 1)}
        time.sleep(0.3)

    return {"ok": False, "state": "timeout",
            "error": f"服务在 {wait:.0f}s 内未就绪，详见 {LOG}", "port": port}


def stop(port: int = 8765, timeout: float = 10.0) -> dict:
    """请求服务优雅退出。"""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/shutdown", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "state": "stop-failed", "error": f"{type(e).__name__}: {e}"}

    t0 = time.time()
    while time.time() - t0 < timeout:
        if health(port, timeout=1.0) is None:
            return {"ok": True, "state": "stopped", "port": port}
        time.sleep(0.3)
    return {"ok": False, "state": "still-running", "port": port}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="NPU", help="NPU / CPU / GPU / AUTO")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--wait", type=float, default=90.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--status", action="store_true", help="只检查状态，不启动")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的服务")
    args = ap.parse_args()

    if args.stop:
        res = stop(args.port)
    elif args.status:
        h = health(args.port)
        res = {"ok": bool(h), "state": "running" if h else "stopped", "health": h, "port": args.port}
    else:
        res = ensure(args.device, args.port, args.wait)

    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        if res["ok"]:
            hp = res.get("health") or {}
            print(f"[OK] {res['state']}  device={hp.get('execution_devices')}  port={res.get('port')}")
        else:
            print(f"[FAIL] {res['state']}  {res.get('error','')}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
