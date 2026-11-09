"""
install_native_host.py —— 注册 Chrome Native Messaging 宿主

做两件事（都只影响当前用户，无需管理员权限）：
  1. 生成宿主清单 E:\\ai\\dsh\\native_host_manifest.json（含允许的扩展 ID）
  2. 写入注册表 HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.npu.translate

用法:
  python install_native_host.py                 # 用内置候选 ID
  python install_native_host.py <扩展ID>        # 指定 ID（从 chrome://extensions 复制）
  python install_native_host.py --uninstall
"""
import base64
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(ROOT, "native_host_manifest.json")
HOST_NAME = "com.npu.translate"
REG_PATHS = [
    rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
    rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
    rf"Software\Chromium\NativeMessagingHosts\{HOST_NAME}",
]
EXT_DIR = os.path.join(ROOT, "chrome-extension")
EXT_ID_FILE = os.path.join(EXT_DIR, "extension_id.txt")


def ids_from_manifest(ext_dir: str):
    """兜底：manifest 里若仍带 "key"（公钥），扩展 ID 由该公钥决定。

    ⚠️ 注意：这条路径**不应该**再被用到。给「已解压加载」的扩展加 "key" 会导致
    Chrome 重启时把它卸载（每次打开浏览器都要重新加载插件），而且同路径出现两个
    不同 ID 的实例时，Chrome 会因重复 key 静默忽略其中一个。
    正确的做法是 manifest 不带 key，ID 由安装目录推导 —— 见 fix_extension.py。
    这里保留解析逻辑只为兼容历史包，正常安装时 EXT_DIR 下不应有 key。
    """
    try:
        with open(os.path.join(ext_dir, "manifest.json"), encoding="utf-8") as f:
            key = json.load(f).get("key")
        if key:
            der = base64.b64decode(key)
            h = hashlib.sha256(der).digest()[:16]
            return ["".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in h)]
    except Exception:
        pass
    return []


def derive_ids(ext_dir: str):
    """按「安装目录」推导未打包扩展的 ID。

    Chrome 在 Windows 上哈希的是 base::FilePath 的宽字符值，即 **UTF-16LE** 字节
    （不是 UTF-8）。这里对路径的若干写法变体都算一遍以提高命中率。
    """
    variants = [
        ext_dir, ext_dir + "\\", ext_dir.replace("\\", "/"),
        ext_dir.lower(), (ext_dir + "\\").lower(), ext_dir.replace("\\", "/").lower(),
    ]
    ids = []
    for v in variants:
        h = hashlib.sha256(v.encode("utf-16-le")).digest()[:16]
        i = "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in h)
        if i not in ids:
            ids.append(i)
    return ids


def detect_browser_ids():
    """从本机 Chromium 系浏览器配置里探测「本插件实际被分配的扩展 ID」。

    为什么需要：加 key 之前加载的旧实例，其 ID 是按路径算出来的（与固定 ID 不同），
    只允许固定 ID 会让这个旧实例继续报 "Access to the specified native messaging host
    is forbidden"。把探测到的 ID 一并允许，旧实例无需移除重装即可工作。
    """
    import glob
    local = os.environ.get("LOCALAPPDATA", "")
    roots = [
        os.path.join(local, r"Google\Chrome\User Data"),
        os.path.join(local, r"Google\Chrome Beta\User Data"),
        os.path.join(local, r"Microsoft\Edge\User Data"),
        os.path.join(local, r"BraveSoftware\Brave-Browser\User Data"),
        os.path.join(local, r"Chromium\User Data"),
    ]
    mine = os.path.normcase(os.path.abspath(EXT_DIR)).rstrip("\\")
    ids = []
    for base in roots:
        if not os.path.isdir(base):
            continue
        for pat in ("*/Preferences", "*/Secure Preferences"):
            for path in glob.glob(os.path.join(base, pat)):
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        prefs = json.load(f)
                except Exception:
                    continue
                settings = (prefs.get("extensions") or {}).get("settings") or {}
                for ext_id, info in settings.items():
                    if not isinstance(info, dict):
                        continue
                    p = info.get("path") or ""
                    if p and os.path.normcase(os.path.abspath(p)).rstrip("\\") == mine:
                        if ext_id not in ids:
                            ids.append(ext_id)
    return ids


def collect_ids(explicit=None):
    ids = []
    for i in (([explicit] if explicit else [])
              + _from_file()                    # 首选：extension_id.txt（由 fix_extension.py 写入）
              + detect_browser_ids()            # 其次：浏览器里已加载实例的真实 ID
              + derive_ids(EXT_DIR)             # 再次：按路径推导
              + ids_from_manifest(EXT_DIR)):    # 兼容：历史包里的公钥（正常应为空）
        if i and i not in ids:
            ids.append(i.strip())
    return ids


def _from_file():
    """读取 extension_id.txt；跳过注释行，并校验格式（32 个 a..p 字符）。"""
    if os.path.exists(EXT_ID_FILE):
        try:
            with open(EXT_ID_FILE, encoding="utf-8") as f:
                out = []
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    if len(s) == 32 and all("a" <= c <= "p" for c in s):
                        out.append(s)
                return out
        except Exception:
            pass
    return []


def write_manifest(ids):
    manifest = {
        "name": HOST_NAME,
        "description": "N-Translate 本地服务的启动器（让插件可以自行拉起本地服务）",
        "path": os.path.join(ROOT, "npu_host.bat"),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{i}/" for i in ids],
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def write_reg_fallback():
    """额外生成 .reg 文件：万一 Python 写注册表被拦截，可双击导入（含各浏览器）。"""
    reg = os.path.join(ROOT, "native_host_manifest.reg")
    path_escaped = MANIFEST.replace("\\", "\\\\")
    body = "Windows Registry Editor Version 5.00\r\n\r\n"
    for rp in REG_PATHS:
        body += f"[HKEY_CURRENT_USER\\{rp}]\r\n@=\"{path_escaped}\"\r\n\r\n"
    with open(reg, "w", encoding="utf-16-le") as f:
        f.write("\ufeff" + body)
    return reg


def install(explicit=None):
    ids = collect_ids(explicit)
    m = write_manifest(ids)
    print(f"[1/3] 已生成宿主清单: {MANIFEST}")
    print(f"      宿主路径  : {m['path']}")
    print(f"      允许的扩展: {len(ids)} 个 ID")

    # 明确告诉用户哪个是「固定 ID」、哪个是当前已加载实例的 ID
    pinned = ids_from_manifest(EXT_DIR)
    detected = detect_browser_ids()
    if pinned:
        print(f"      · 固定 ID（manifest 公钥决定）: {pinned[0]}")
    if detected:
        print(f"      · 浏览器里已加载实例的 ID    : {', '.join(detected)}")

    reg_file = write_reg_fallback()
    print(f"[2/3] 已生成注册表导入文件: {reg_file}")

    ok, fail = [], []
    try:
        import winreg
        for rp in REG_PATHS:
            try:
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rp)
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, MANIFEST)
                winreg.CloseKey(key)
                ok.append(rp)
            except Exception as e:
                fail.append(f"{rp}  ({type(e).__name__})")
    except Exception as e:
        fail.append(f"winreg 不可用 ({type(e).__name__}: {e})")

    if ok:
        print(f"[3/3] 已注册到: " + "; ".join(f"HKCU\\{p}" for p in ok))
    if fail:
        print("[3/3] 以下注册失败（可双击 .reg 文件导入）:")
        for x in fail:
            print(f"      {x}")
        if not ok:
            return 1

    print("\n完成 ✅ 接下来：")
    print("  1) chrome://extensions 里点本插件的「重新加载」（若已重装则跳过）")
    print("  2) 点插件图标，状态显示「未连接」时会自动尝试启动本地服务")
    return 0


def uninstall():
    removed, missing = [], []
    for rp in REG_PATHS:
        try:
            import winreg
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rp)
            removed.append(rp)
        except FileNotFoundError:
            missing.append(rp)
        except Exception as e:
            print(f"删除失败 {rp}: {type(e).__name__}: {e}")
    for rp in removed:
        print(f"已删除注册表项: HKCU\\{rp}")
    if not removed and missing:
        print("注册表项本来就不存在")
    if os.path.exists(MANIFEST):
        os.remove(MANIFEST)
        print(f"已删除清单: {MANIFEST}")
    return 0


def main():
    if "--uninstall" in sys.argv:
        return uninstall()
    explicit = None
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            explicit = a.strip()
            break
    return install(explicit)


if __name__ == "__main__":
    raise SystemExit(main())
