r"""
updater.py —— 热更新：从 GitHub 仓库拉取源码包并就地覆盖

覆盖范围（只动代码，不动你的东西）：
    ✔  *.py / *.bat / *.ps1 / *.md / *.json（顶层，除 settings.json）
    ✔  chrome-extension\**（保留本机 extension_id.txt）
    ✔  docs\**
    ✘  models\  llama\  .venv\  ov_cache\  dist\  _scratch\
    ✘  settings.json  restart.flag  native_host_manifest.json

流程：下载 zip（带镜像兜底）→ 解压到临时目录 → 记录版本 → 停服务 → 覆盖 → 重启服务
最后再让用户在 chrome://extensions 点一次「重新加载」，新插件代码才会生效
（扩展自己没权限改自己的文件，所以这一步只能借本地宿主来做）。

用法:
    python updater.py                          # 用默认仓库
    python updater.py --url <zip 地址>
    python updater.py --json
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = "NstyZ/N-Translate"
DEFAULT_URL = f"https://github.com/{DEFAULT_REPO}/archive/refs/heads/main.zip"
MIRROR_PREFIXES = ["https://gh-proxy.com/", "https://ghproxy.net/", ""]

# 这些顶层条目不动（用户数据 / 运行时 / 依赖）
PROTECTED = {
    "settings.json", "restart.flag", "native_host_manifest.json",
    "native_host_manifest.reg", "extension_id.txt",
}
PROTECTED_DIRS = {"models", "llama", ".venv", "ov_cache", "dist", "_scratch", ".hf",
                  "keys", "__pycache__"}
COPY_EXT = {".py", ".bat", ".ps1", ".md", ".json", ".txt", ".jinja", ".css", ".js", ".html",
            ".png", ".svg", ""}


def _log(msg, quiet):
    if not quiet:
        print(msg, flush=True)


def fetch(urls, quiet=False):
    import urllib.request

    # 去重 + 补镜像前缀（已经是镜像地址的保持原样）
    cands = []
    for u in urls:
        if not u:
            continue
        if any(u.startswith(p) for p in MIRROR_PREFIXES if p):
            cands.append(u)
            continue
        for prefix in MIRROR_PREFIXES:
            cands.append(prefix + u if prefix else u)
    seen, ordered = set(), []
    for u in cands:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    last = None
    for full in ordered:
        try:
            _log(f"[..] GET {full}", quiet)
            req = urllib.request.Request(full, headers={"User-Agent": "n-translate/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read()
            if len(data) < 1024:
                last = RuntimeError("下载内容过小，可能 404")
                continue
            _log(f"[ok] {len(data)/2**20:.1f} MB", quiet)
            return data
        except Exception as e:
            last = e
            _log(f"[..] 失败 {type(e).__name__}: {e}", quiet)
    raise RuntimeError(f"下载失败：{last}")


def pick_root(names):
    """GitHub 源码包里所有条目都在 <repo>-<分支>/ 下，取这个前缀。"""
    tops = {n.split("/", 1)[0] for n in names if n}
    tops.discard("")
    if len(tops) == 1:
        only = next(iter(tops))
        if all(n == only or n.startswith(only + "/") for n in names):
            return only + "/"
    return ""


def install_from_zip(data, quiet=False):
    files = 0
    from_ver = to_ver = ""
    cur_manifest = os.path.join(ROOT, "chrome-extension", "manifest.json")
    try:
        from_ver = json.load(open(cur_manifest, encoding="utf-8")).get("version", "")
    except Exception:
        pass

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        prefix = pick_root(names)
        if not prefix:
            raise RuntimeError("源码包结构异常：找不到仓库根目录")

        # 先读新版本号
        try:
            to_ver = json.loads(z.read(prefix + "chrome-extension/manifest.json")
                                .decode("utf-8")).get("version", "")
        except Exception:
            pass

        tmp = tempfile.mkdtemp(prefix="ntranslate-upd-")
        try:
            z.extractall(tmp)
            src = os.path.join(tmp, prefix.replace("/", os.sep)).rstrip(os.sep)
            for name in sorted(os.listdir(src)):
                s = os.path.join(src, name)
                if name in PROTECTED_DIRS or name in PROTECTED:
                    continue
                if os.path.isdir(s):
                    if name in ("docs", "chrome-extension", "icons"):
                        files += _merge_dir(s, os.path.join(ROOT, name), quiet)
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in COPY_EXT:
                    continue
                shutil.copy2(s, os.path.join(ROOT, name))
                files += 1
                _log(f"  + {name}", quiet)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return files, from_ver, to_ver


def _merge_dir(src, dst, quiet=False):
    n = 0
    os.makedirs(dst, exist_ok=True)
    for cur, _dirs, fs in os.walk(src):
        rel = os.path.relpath(cur, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in fs:
            if f in PROTECTED:
                continue
            if os.path.splitext(f)[1].lower() in (".log", ".pyc"):
                continue
            shutil.copy2(os.path.join(cur, f), os.path.join(target, f))
            n += 1
    _log(f"  + {os.path.basename(dst)}/ ({n} 个文件)", quiet)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL, help="源码包 zip 地址")
    ap.add_argument("--mirror", action="append", default=[], help="备用镜像地址（可多次）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-restart", action="store_true")
    args = ap.parse_args()
    quiet = args.json

    result = {"ok": False, "from": "", "to": "", "files": 0, "error": ""}
    try:
        data = fetch([args.url] + args.mirror, quiet=quiet)
    except Exception as e:
        result["error"] = str(e)
        print(json.dumps(result, ensure_ascii=False) if args.json else f"[ERR] {e}")
        return 1

    try:
        import server_daemon as sd
        _log("[..] 停止本地服务（覆盖源码前必须先停）", quiet)
        try:
            sd.stop()
        except Exception:
            pass
    except Exception:
        sd = None

    try:
        files, from_ver, to_ver = install_from_zip(data, quiet=quiet)
        result.update(ok=True, files=files, **{"from": from_ver, "to": to_ver})
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        print(json.dumps(result, ensure_ascii=False) if args.json else f"[ERR] {e}")
        return 1

    if sd is not None and not args.no_restart:
        _log("[..] 重新启动本地服务", quiet)
        try:
            r = sd.ensure()
            result["service"] = r.get("state", "")
        except Exception as e:
            result["service"] = f"restart-failed: {e}"

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[OK] 已更新 {files} 个文件（v{from_ver} → v{to_ver}）")
        print("     最后一步：chrome://extensions → 点本插件的「重新加载」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
