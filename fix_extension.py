r"""
fix_extension.py —— 一键修复「Chrome 每次启动都要重新加载插件」

背景（问题根因）:
  之前为了让扩展 ID 在任何电脑/任何目录都固定，往 manifest.json 里加了 "key"（RSA 公钥）。
  但 Chrome 对「已解压加载」的扩展有一条行为：manifest 带 "key" 时，重启浏览器会把它卸载掉。
  于是每次打开 Chrome 都要重新「加载已解压的扩展程序」。

修复:
  1. 从 manifest.json 里移除 "key"
  2. 用与 Chrome 完全相同的算法算出该目录对应的扩展 ID，并写进
     chrome-extension\extension_id.txt
  3. 把该 ID 写进 Native Messaging 宿主清单与注册表（Chrome / Edge / Chromium，
     只影响当前用户，不需要管理员权限）

用法:
  python fix_extension.py            # 自动计算 ID 并注册
  python fix_extension.py --dry-run  # 只显示将要写入的内容，不落盘、不改注册表
  python fix_extension.py <扩展ID>   # 手动指定 ID（一般不需要）
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.join(ROOT, "chrome-extension")
MANIFEST = os.path.join(ROOT, "native_host_manifest.json")
ID_FILE = os.path.join(EXT_DIR, "extension_id.txt")
HOST_NAME = "com.npu.translate"
HOST_PATH = os.path.join(ROOT, "npu_host.bat")
REG_PATHS = [
    rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
    rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
    rf"Software\Chromium\NativeMessagingHosts\{HOST_NAME}",
]


def chrome_id_from_path(path: str) -> str:
    """Chrome 计算未打包扩展 ID 的方式：sha256(绝对路径) 前 16 字节，
    每字节高/低 nibble 分别映射到 a..p。

    注意：Windows 上 Chrome 哈希的是 base::FilePath 的宽字符值，即 **UTF-16LE** 字节，
    不是 UTF-8。用 UTF-8 会算出不同的（错误的）ID——本脚本已按 UTF-16LE 实现，
    并用本机 Chrome 实际分配的 ID 校验过。
    """
    path = os.path.abspath(path)
    digest = hashlib.sha256(path.encode("utf-16-le")).digest()[:16]
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest)


def detect_ids():
    """从浏览器配置里读出「当前实际加载的」本插件 ID（用于兼容尚未重装的旧实例）。"""
    import glob
    local = os.environ.get("LOCALAPPDATA", "")
    bases = [
        os.path.join(local, r"Google\Chrome\User Data"),
        os.path.join(local, r"Google\Chrome Beta\User Data"),
        os.path.join(local, r"Microsoft\Edge\User Data"),
        os.path.join(local, r"BraveSoftware\Brave-Browser\User Data"),
        os.path.join(local, r"Chromium\User Data"),
    ]
    mine = os.path.normcase(os.path.abspath(EXT_DIR)).rstrip("\\")
    found = []
    for base in bases:
        if not os.path.isdir(base):
            continue
        for pat in ("*/Preferences", "*/Secure Preferences"):
            for path in glob.glob(os.path.join(base, pat)):
                try:
                    with open(path, encoding="utf-8-sig", errors="replace") as f:
                        prefs = json.load(f)
                except Exception:
                    continue
                for ext_id, info in ((prefs.get("extensions") or {}).get("settings") or {}).items():
                    if not isinstance(info, dict):
                        continue
                    p = info.get("path") or ""
                    if p and os.path.normcase(os.path.abspath(p)).rstrip("\\") == mine:
                        if ext_id not in found:
                            found.append(ext_id)
    return found


def main():
    dry = "--dry-run" in sys.argv
    explicit = next((a.strip() for a in sys.argv[1:] if not a.startswith("-")), None)

    mf_path = os.path.join(EXT_DIR, "manifest.json")
    # utf-8-sig：文件若带 BOM 也能正确读；写回时不带 BOM（Chrome 的 JSON 解析器不接受 BOM）
    with open(mf_path, encoding="utf-8-sig") as f:
        mf = json.load(f)

    changed = False
    if "key" in mf:
        if dry:
            print('[dry-run] 将从 manifest.json 移除 "key"')
        else:
            mf.pop("key")
            with open(mf_path, "w", encoding="utf-8") as f:
                json.dump(mf, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print('[1/4] 已从 manifest.json 移除 "key"（这是重启被卸载的根因）')
            changed = True
    else:
        print('[1/4] manifest.json 已无 "key"，无需修改')

    main_id = chrome_id_from_path(EXT_DIR)
    print(f'[2/4] 本目录的扩展 ID  : {main_id}')
    print(f'      依据               : sha256("{EXT_DIR}" UTF-16LE) 前 16 字节')

    ids = [main_id]
    if explicit:
        ids.insert(0, explicit)
        print(f'      手动指定           : {explicit}')
    for i in detect_ids():
        if i not in ids:
            ids.append(i)
    if len(ids) > 1:
        print(f'      一并允许已加载实例 : {", ".join(i for i in ids if i != main_id)}')

    host = {
        "name": HOST_NAME,
        "description": "N-Translate 本地服务的启动器（让插件可以自行拉起本地服务）",
        "path": HOST_PATH,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{i}/" for i in ids],
    }

    if dry:
        print("[dry-run] 宿主清单内容:")
        print(json.dumps(host, ensure_ascii=False, indent=2))
    else:
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump(host, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"[3/4] 已写入宿主清单      : {MANIFEST}")

        with open(ID_FILE, "w", encoding="utf-8") as f:
            f.write(main_id + "\n")
        print(f"      已写入扩展 ID 文件  : {ID_FILE}")

        ok, fail = [], []
        try:
            import winreg
            for rp in REG_PATHS:
                try:
                    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rp)
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, MANIFEST)
                    winreg.CloseKey(k)
                    ok.append(rp)
                except Exception as e:
                    fail.append(f"{rp}  ({type(e).__name__})")
        except Exception as e:
            fail.append(f"winreg 不可用 ({type(e).__name__}: {e})")

        if ok:
            print("[4/4] 已注册宿主到:")
            for p in ok:
                print(f"      HKCU\\{p}")
        if fail:
            print("[4/4] 部分注册失败（可双击 native_host_manifest.reg 导入）:")
            for x in fail:
                print(f"      {x}")

    print("\n接下来在 Chrome 里:")
    print("  1) chrome://extensions → 开发者模式已开")
    print("  2) 若插件仍显示旧状态：先「移除」，再「加载已解压的扩展程序」选")
    print(f"     {EXT_DIR}")
    print(f"  3) 确认卡片上的 ID 是 {main_id}")
    print("  4) 以后重启 Chrome 无需再手动加载")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
