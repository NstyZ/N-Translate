"""
install_llama.py —— 下载 llama.cpp 运行时（Index-Translate 等 GGUF 模型要用）

两种口味：
    cpu  —— 约 24 MB，任何电脑都能跑（实测 16 线程约 26 tok/s）
    cuda —— 约 700 MB，需要 NVIDIA 显卡（实测 RTX 5060 约 156 tok/s，快 6 倍）

下载后解压到：
    llama\\             <- cpu 版（llama-server.exe 等）
    llama\\cuda\\        <- cuda 版（服务启动时优先使用）

GitHub 直连在国内常被重置，脚本会自动尝试几个加速前缀。

用法:
    python install_llama.py                 # 默认 cpu
    python install_llama.py --flavor cuda    # NVIDIA 显卡
    python install_llama.py --ver b12211
"""
import argparse
import io
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))

MIRRORS = [
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "",
]
ASSETS = {
    "cpu": ["llama-{ver}-bin-win-cpu-x64.zip"],
    "cuda": ["llama-{ver}-bin-win-cuda-13.5-x64.zip",
             "cudart-llama-bin-win-cuda-13.5-x64.zip"],
}


def fetch(url: str) -> bytes:
    import urllib.request

    last = None
    for prefix in MIRRORS:
        u = prefix + url
        try:
            print(f"  GET {u}", flush=True)
            req = urllib.request.Request(u, headers={"User-Agent": "n-translate/1.0"})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            print(f"  OK  {len(data)/2**20:.1f} MB", flush=True)
            return data
        except Exception as e:
            last = e
            print(f"  fail: {type(e).__name__}: {e}", flush=True)
    raise RuntimeError(f"全部镜像都失败: {last}")


def extract(data: bytes, dest_dir: str) -> None:
    """只取文件名（zip 里带一层顶层目录），全部平铺到 dest_dir。"""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = os.path.basename(info.filename)
            if not name:
                continue
            with z.open(info) as src, open(os.path.join(dest_dir, name), "wb") as dst:
                dst.write(src.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flavor", choices=["cpu", "cuda"], default="cpu")
    ap.add_argument("--ver", default="b12211", help="llama.cpp 构建版本号")
    args = ap.parse_args()

    dest = os.path.join(ROOT, "llama") if args.flavor == "cpu" \
        else os.path.join(ROOT, "llama", "cuda")
    base = f"https://github.com/ggml-org/llama.cpp/releases/download/{args.ver}/"

    print(f"=== 安装 llama.cpp（{args.flavor}）{args.ver} → {dest} ===", flush=True)
    for tpl in ASSETS[args.flavor]:
        name = tpl.format(ver=args.ver)
        print(f"[{name}]", flush=True)
        extract(fetch(base + name), dest)

    files = [f for f in os.listdir(dest) if f.lower().endswith((".dll", ".exe"))]
    size = sum(os.path.getsize(os.path.join(dest, f)) for f in files) / 2**20
    print(f"=== 完成：{len(files)} 个文件，{size:.0f} MB ===", flush=True)
    print("服务启动时会自动选用：" +
          ("llama\\cuda\\llama-server.exe（-ngl 99）" if args.flavor == "cuda"
           else "llama\\llama-server.exe（CPU）"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
