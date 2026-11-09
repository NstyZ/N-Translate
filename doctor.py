"""
doctor.py —— 运行环境体检 / 自动修复

回答两个问题：**这台机器能不能跑？缺的东西能不能自己补上？**

    python doctor.py                # 只体检，出报告（不会动任何文件）
    python doctor.py --fix          # 缺什么补什么（venv / 依赖 / llama.cpp / 模型）
    python doctor.py --fix --with-model   # 连 Index-Translate 主模型（1.2GB）一起下
    python doctor.py --json         # 机器可读，供插件与 Native Messaging 用

体检项：Windows 版本 / 64 位 / 磁盘空间 / Python / 虚拟环境 / 依赖 /
        llama.cpp / 模型 / NPU 设备 / 端口 / 启动器注册

退出码：0 = 没有失败项（警告不算），1 = 有失败项。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, ".venv")
VPY = os.path.join(VENV, "Scripts", "python.exe")
PORT = 8765
HOST_NAME = "com.npu.translate"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _run(args, timeout=1800, **kw):
    """执行外部命令，返回 (returncode, output)。不抛异常。"""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", **kw)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _size_free_gb(path=ROOT):
    try:
        import shutil
        return shutil.disk_usage(path).free / 2**30
    except Exception:
        return -1.0


def _store_stub(exe):
    """Microsoft Store 的 python 残桩：能被执行但什么也不做。"""
    return "windowsapps" in (exe or "").lower()


def find_python():
    """找一个可用的 Python 3.10+（64 位）。返回 dict 或 None。"""
    cands = []
    for exe, pre in (("py", ["-3"]), ("py", ["-3.13"]), ("python", []), ("python3", [])):
        cands.append((exe, pre))
    import glob
    for pat in (r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe",
                r"C:\Python3*\python.exe", r"C:\Program Files\Python3*\python.exe"):
        for p in glob.glob(os.path.expandvars(pat)):
            cands.append((p, []))
    for exe, pre in cands:
        if exe in ("py", "python", "python3"):
            import shutil
            full = shutil.which(exe)
            if not full or _store_stub(full):
                continue
            exe = full
        if not os.path.exists(exe):
            continue
        rc, out = _run([exe] + pre + ["-c",
                                      "import sys;print(sys.version_info[0],sys.version_info[1],"
                                      "8*__import__('struct').calcsize('P'))"], timeout=30)
        if rc != 0:
            continue
        try:
            major, minor, bits = (out.strip().splitlines()[-1]).split()
            if int(major) == 3 and int(minor) >= 10 and int(bits) == 64:
                return {"exe": exe, "pre": pre, "version": f"3.{minor}", "bits": 64}
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 体检项
# ---------------------------------------------------------------------------
class Item:
    def __init__(self, key, title, status, detail="", fixable=False, fix=None):
        self.key, self.title, self.status = key, title, status
        self.detail, self.fixable, self.fix = detail, fixable, fix

    def as_dict(self):
        return {"key": self.key, "title": self.title, "status": self.status,
                "detail": self.detail, "fixable": self.fixable}


def check_all():
    items = []

    # --- 1. 系统 ---
    if os.name != "nt":
        items.append(Item("os", "操作系统", "fail", "只支持 Windows（NPU 驱动与服务器均在 Windows 上验证）"))
    else:
        major, minor, build = (sys.getwindowsversion() if hasattr(sys, "getwindowsversion")
                               else (10, 0, 0))[:3]
        v = f"{major}.{minor}.{build}"
        if build >= 22000:
            items.append(Item("os", "Windows 版本", "ok", f"Windows 11 (build {build})"))
        elif build >= 19041:
            items.append(Item("os", "Windows 版本", "warn",
                              f"Windows 10 build {build}：能用，但 NPU 驱动建议 Win11 24H2+"))
        else:
            items.append(Item("os", "Windows 版本", "warn", f"build {build}：偏旧，可能缺运行库"))

    bits = 64 if sys.maxsize > 2**32 else 32
    items.append(Item("arch", "系统位数", "ok" if bits == 64 else "fail",
                      f"{bits} 位" + ("" if bits == 64 else "（需要 64 位系统）")))

    free = _size_free_gb()
    if free < 0:
        items.append(Item("disk", "磁盘空间", "warn", "无法读取"))
    elif free >= 8:
        items.append(Item("disk", "磁盘空间", "ok", f"可用 {free:.1f} GB"))
    elif free >= 3:
        items.append(Item("disk", "磁盘空间", "warn", f"可用 {free:.1f} GB（装完约需 3~4 GB）"))
    else:
        items.append(Item("disk", "磁盘空间", "fail", f"只剩 {free:.1f} GB，至少需要 3 GB"))

    # --- 2. Python / 虚拟环境 ---
    py = find_python()
    if py:
        items.append(Item("python", "Python 3.10+", "ok",
                          f"{py['exe']} ({py['version']}, {py['bits']} 位)"))
    else:
        items.append(Item("python", "Python 3.10+", "fail",
                          "未找到可用的 Python（注意：Microsoft Store 的 python 是残桩，不算）"))

    if os.path.exists(VPY):
        rc, out = _run([VPY, "-c", "import sys;print(sys.version.split()[0])"], timeout=60)
        if rc == 0:
            items.append(Item("venv", "虚拟环境 .venv", "ok", f"Python {out.strip()}"))
        else:
            items.append(Item("venv", "虚拟环境 .venv", "fail", "存在但无法运行（需重建）",
                              True, "venv"))
    else:
        items.append(Item("venv", "虚拟环境 .venv", "fail", "不存在", True, "venv"))

    # --- 3. 依赖 ---
    deps = [("numpy", "numpy"), ("openvino", "openvino"),
            ("openvino_tokenizers", "openvino-tokenizers"),
            ("sentencepiece", "sentencepiece"), ("jinja2", "jinja2")]
    if os.path.exists(VPY):
        missing = []
        for mod, pkg in deps:
            rc, _ = _run([VPY, "-c", f"import {mod}"], timeout=120)
            if rc != 0:
                missing.append(pkg)
        if not missing:
            items.append(Item("deps", "推理依赖", "ok", "numpy / openvino / tokenizers / sentencepiece / jinja2"))
        else:
            items.append(Item("deps", "推理依赖", "fail", "缺少：" + ", ".join(missing),
                              True, "deps"))
    else:
        items.append(Item("deps", "推理依赖", "fail", "虚拟环境缺失，待创建后安装", True, "deps"))

    # --- 4. llama.cpp 运行时 ---
    exe = os.path.join(ROOT, "llama", "llama-server.exe")
    cuda = os.path.join(ROOT, "llama", "cuda", "llama-server.exe")
    if os.path.exists(cuda):
        items.append(Item("llama", "llama.cpp 运行时", "ok", "CUDA 版（显卡加速）"))
    elif os.path.exists(exe):
        items.append(Item("llama", "llama.cpp 运行时", "ok", "CPU 版"))
    else:
        items.append(Item("llama", "llama.cpp 运行时", "warn",
                          "未安装：Index-Translate 需要它（约 24MB 下载）", True, "llama"))

    # --- 5. 模型 ---
    opus = os.path.join(ROOT, "models", "opus-en-zh-int8-stateless-ov",
                        "openvino_decoder_model.xml")
    items.append(Item("model-opus", "轻量模型 OPUS-MT", "ok" if os.path.exists(opus) else "warn",
                      "已就绪" if os.path.exists(opus) else "未安装（约 111MB，NPU 直接用）",
                      not os.path.exists(opus), "model-opus"))

    gguf = os.path.join(ROOT, "models", "index-translate-2b-gguf",
                        "Index-Translate-2B.Q4_K_M.gguf")
    has_index = os.path.exists(gguf)
    items.append(Item("model-index", "主模型 Index-Translate 2B", "ok" if has_index else "warn",
                      "已就绪" if has_index else "未安装（约 1.2GB；不装会回退到 OPUS-MT）",
                      not has_index, "model-index"))

    if not os.path.exists(opus) and not has_index:
        # 两个都没有就是真跑不了
        items[-2].status = "fail"
        items[-1].status = "fail"

    # --- 6. NPU 设备 ---
    if os.path.exists(VPY):
        rc, out = _run([VPY, "-c",
                        "import openvino as ov;print(','.join(ov.Core().available_devices))"],
                       timeout=180)
        if rc == 0 and out.strip():
            devs = [d.strip() for d in out.strip().splitlines()[-1].split(",")]
            if "NPU" in devs:
                items.append(Item("npu", "Intel NPU", "ok", "AI Boost 可用"))
            else:
                items.append(Item("npu", "Intel NPU", "warn",
                                  f"未检测到 NPU（现有：{','.join(devs)}），将使用 CPU"))
        else:
            items.append(Item("npu", "Intel NPU", "warn", "无法探测（依赖未就绪）"))
    else:
        items.append(Item("npu", "Intel NPU", "warn", "无法探测（虚拟环境未就绪）"))

    # --- 7. 端口 ---
    s = socket.socket()
    s.settimeout(0.4)
    busy = s.connect_ex(("127.0.0.1", PORT)) == 0
    s.close()
    items.append(Item("port", "端口 8765", "warn" if busy else "ok",
                      "已被占用（若本服务正在运行则正常）" if busy else "空闲"))

    # --- 8. 启动器注册 ---
    reg_ok = False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"):
            reg_ok = True
    except Exception:
        reg_ok = False
    items.append(Item("host", "插件启动器", "ok" if reg_ok else "warn",
                      "已注册" if reg_ok else "未注册（双击 install_native_host.bat 或重跑 setup.bat）",
                      not reg_ok, None))

    return items


# ---------------------------------------------------------------------------
# 自动修复
# ---------------------------------------------------------------------------
def do_fix(action, with_model=False, say=print):
    """返回 (是否成功, 说明)。"""
    if action == "venv":
        py = find_python()
        if not py:
            return False, "没有可用的 Python，无法创建虚拟环境"
        import shutil
        if os.path.isdir(VENV):
            say("  - 现有 .venv 不可用，先删除 …")
            shutil.rmtree(VENV, ignore_errors=True)
        say("  - 创建 .venv（--without-pip，随后手动引导 pip）…")
        rc, out = _run([py["exe"]] + py["pre"] + ["-m", "venv", "--without-pip", VENV])
        if rc != 0 or not os.path.exists(VPY):
            return False, f"创建虚拟环境失败：{out[-300:]}"
        rc, out = _run([VPY, "-m", "ensurepip", "--upgrade", "--default-pip"])
        rc2, _ = _run([VPY, "-m", "pip", "--version"])
        if rc2 != 0:
            return False, "pip 引导失败，Python 安装可能不完整"
        return True, "虚拟环境已就绪"

    if action == "deps":
        if not os.path.exists(VPY):
            return False, "虚拟环境不存在"
        req = os.path.join(ROOT, "requirements.txt")
        say("  - 安装依赖（首次约 3~8 分钟）…")
        args = [VPY, "-m", "pip", "install", "-r", req]
        rc, out = _run(args, timeout=3600)
        if rc != 0:
            say("  - 默认源失败，改走清华镜像重试 …")
            rc, out = _run(args + ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
                           timeout=3600)
        if rc != 0:
            return False, f"依赖安装失败：{out[-400:]}"
        return True, "依赖安装完成"

    if action == "llama":
        say("  - 下载 llama.cpp（CPU 版，约 24MB）…")
        rc, out = _run([sys.executable, os.path.join(ROOT, "install_llama.py"),
                        "--flavor", "cpu"], timeout=1800)
        ok = os.path.exists(os.path.join(ROOT, "llama", "llama-server.exe"))
        return ok, ("llama.cpp 已就绪" if ok else f"下载失败：{out[-300:]}")

    if action == "model-opus":
        if not os.path.exists(VPY):
            return False, "需要先装好虚拟环境"
        say("  - 下载并导出 OPUS-MT（约 111MB）…")
        rc, out = _run([VPY, os.path.join(ROOT, "install_model.py"),
                        "--id", "opus-mt-en-zh"], timeout=3600)
        ok = os.path.exists(os.path.join(ROOT, "models", "opus-en-zh-int8-stateless-ov",
                                         "openvino_decoder_model.xml"))
        return ok, ("OPUS-MT 已就绪" if ok else f"导出失败：{out[-400:]}")

    if action == "model-index":
        if not with_model:
            return False, "主模型 1.2GB，需要显式加 --with-model 才会下载"
        if not os.path.exists(VPY):
            return False, "需要先装好虚拟环境"
        say("  - 下载 Index-Translate 2B（约 1.2GB）…")
        rc, out = _run([VPY, os.path.join(ROOT, "install_model.py"),
                        "--id", "index-translate-2b"], timeout=7200)
        ok = os.path.exists(os.path.join(ROOT, "models", "index-translate-2b-gguf",
                                         "Index-Translate-2B.Q4_K_M.gguf"))
        return ok, ("主模型已就绪" if ok else f"下载失败：{out[-400:]}")

    return False, f"未知的修复动作：{action}"


FIX_ORDER = ["venv", "deps", "llama", "model-opus", "model-index"]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
MARK = {"ok": "[OK] ", "warn": "[!]  ", "fail": "[X]  "}


def render(items):
    for it in items:
        print(f"{MARK[it.status]} {it.title:<22} {it.detail}")
    ok = sum(1 for i in items if i.status == "ok")
    warn = sum(1 for i in items if i.status == "warn")
    fail = sum(1 for i in items if i.status == "fail")
    print(f"\n合计 {len(items)} 项：{ok} 通过 / {warn} 警告 / {fail} 失败")
    if fail:
        print("需要处理的问题：")
        for i in items:
            if i.status == "fail":
                print(f"  - {i.title}：{i.detail}"
                      + ("（可自动修复）" if i.fixable and i.fix else ""))
    return fail


def main() -> int:
    ap = argparse.ArgumentParser(description="N-Translate 运行环境体检 / 自动修复")
    ap.add_argument("--fix", action="store_true", help="自动补齐缺失的运行环境")
    ap.add_argument("--with-model", action="store_true", help="连主模型一起下载（1.2GB）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供插件调用）")
    args = ap.parse_args()

    quiet = args.json
    say = (lambda *a, **k: None) if quiet else print

    items = check_all()

    if args.fix:
        todo = [i for i in items if i.status in ("fail", "warn") and i.fixable and i.fix]
        todo.sort(key=lambda i: FIX_ORDER.index(i.fix) if i.fix in FIX_ORDER else 99)
        if todo:
            say("=== 开始自动修复 ===")
            for it in todo:
                if it.fix == "model-index" and not args.with_model:
                    continue
                say(f"-> {it.title}")
                ok, msg = do_fix(it.fix, with_model=args.with_model, say=say)
                say(f"   {'完成' if ok else '失败'}：{msg}")
            say("=== 修复结束，重新体检 ===\n")
            items = check_all()

    if args.json:
        fail = sum(1 for i in items if i.status == "fail")
        print(json.dumps({
            "ok": fail == 0,
            "items": [i.as_dict() for i in items],
            "fail": fail,
            "warn": sum(1 for i in items if i.status == "warn"),
            "need_fix": [i.fix for i in items if i.status == "fail" and i.fix],
        }, ensure_ascii=False))
        return 0 if fail == 0 else 1

    print("=" * 66)
    print("  N-Translate 运行环境体检")
    print("=" * 66)
    fail = render(items)
    if fail:
        print("\n提示：运行  python doctor.py --fix  可自动补齐上面的失败项。")
        return 1
    print("\n环境就绪，可以启动服务了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
