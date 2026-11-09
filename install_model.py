"""
install_model.py —— 下载并导出某个翻译模型为 OpenVINO IR

  * 每个模型导出两个变体：
      cached  —— 带 KV-cache，适合 CPU / GPU（快）
      static  —— 无状态 + 全静态形状，NPU 必需
  * 已存在的变体会自动跳过
  * 进度直接输出到标准输出（便于重定向到日志文件）

用法:
  python install_model.py --id index-translate-2b
  python install_model.py --id opus-mt-en-zh
  python install_model.py --list
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import model_registry as reg  # noqa: E402


def _ocli_cmd() -> list:
    """优先用 venv 里的 optimum-cli.exe；否则退回 python -c 调用。"""
    exe = os.path.join(ROOT, ".venv", "Scripts", "optimum-cli.exe")
    if os.path.exists(exe):
        return [exe]
    return [sys.executable, "-c",
            "import sys; from optimum.commands.optimum_cli import main; sys.exit(main())"]


def build_env() -> dict:
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    hf_home = os.path.join(ROOT, ".hf")
    if os.path.isdir(hf_home):
        env["HF_HOME"] = hf_home
    return env


def _download(url: str, dest: str, log=print) -> bool:
    """带进度条的下载（urllib，走 hf-mirror）。已存在且大小一致则跳过。"""
    import urllib.request

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "n-translate/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            t0 = time.time()
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 2:
                        last = time.time()
                        pct = (done / total * 100) if total else 0
                        log(f"    {done/2**20:7.1f}/{total/2**20:.1f} MB  {pct:5.1f}%  "
                            f"{done/2**20/max(time.time()-t0, .1):.1f} MB/s")
        os.replace(tmp, dest)
        return True
    except Exception as e:
        log(f"[ERR] 下载失败 {url}: {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def install_llamacpp(model_id: str, log=print) -> bool:
    """llama.cpp 引擎的模型：下载 GGUF + chat_template.jinja。"""
    m = reg.meta(model_id)
    gguf = reg.gguf_path(model_id)
    if os.path.exists(gguf):
        log(f"[skip] GGUF 已存在 -> {os.path.basename(gguf)}")
        return True
    base = f"https://hf-mirror.com/{m['hf']}/resolve/main"
    name = os.path.basename(gguf)
    log(f"[run ] 下载 {m['hf']}/{name}（约 {m['size_mb']} MB）")
    if not _download(f"{base}/{name}", gguf, log=log):
        return False
    # 官方 chat template（用于本地渲染提示词）
    tpl = os.path.join(os.path.dirname(gguf), "chat_template.jinja")
    if not os.path.exists(tpl):
        repo = m["hf"].replace("-GGUF", "")
        _download(f"https://hf-mirror.com/{repo}/resolve/main/tokenizer_config.json",
                  os.path.join(os.path.dirname(gguf), "tokenizer_config.json"), log=log)
        try:
            import json
            with open(os.path.join(os.path.dirname(gguf), "tokenizer_config.json"),
                      encoding="utf-8") as f:
                t = json.load(f).get("chat_template")
            if t:
                with open(tpl, "w", encoding="utf-8") as f:
                    f.write(t)
                log(f"[ok  ] 已生成 {os.path.basename(tpl)}")
        except Exception as e:
            log(f"[WARN] 提取 chat template 失败: {e}")
    return os.path.exists(gguf)


def export(model_id: str, kinds=("static",), log=print) -> dict:
    if not reg.exists(model_id):
        log(f"[ERR] 未知模型: {model_id}（可用: {', '.join(reg.MODELS)}）")
        return {}
    m = reg.meta(model_id)
    env = build_env()
    base = _ocli_cmd()
    result = {}

    for kind in kinds:
        out = reg.dir_for(model_id, kind)
        if reg.has_variant(model_id, kind):
            log(f"[skip] {kind:6} 已存在 -> {os.path.basename(out)}")
            result[kind] = True
            continue

        cmd = base + ["export", "openvino", "--model", m["hf"], "--weight-format", "int8"]
        if kind == "static":
            cmd.append("--disable-stateful")
        cmd.append(out)

        log(f"[run ] {kind:6} {m['hf']} -> {os.path.basename(out)}")
        log(f"       下载约 {m['size_mb']} MB（首次较久），随后量化导出 ...")
        t0 = time.time()
        try:
            rc = subprocess.call(cmd, env=env, cwd=ROOT)
        except Exception as e:
            log(f"[ERR ] {kind}: {type(e).__name__}: {e}")
            result[kind] = False
            continue
        ok = reg.has_variant(model_id, kind)
        log(f"[{'ok  ' if ok else 'FAIL'}] {kind:6} 用时 {time.time()-t0:.0f}s"
            f"{'' if ok else f'（退出码 {rc}）'}")
        result[kind] = ok
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="模型 ID，如 index-translate-2b")
    ap.add_argument("--kinds", default="static",
                    help="要导出的变体，逗号分隔：static（默认，NPU/CPU/GPU 通用）")
    ap.add_argument("--list", action="store_true", help="列出所有模型及安装状态")
    args = ap.parse_args()

    if args.list or not args.id:
        active = reg.get_active()
        print(f"当前使用: {active}\n")
        for mid, m in reg.MODELS.items():
            if reg.engine_of(mid) == "llamacpp":
                state = ["gguf✓" if reg.installed(mid) else "gguf✗"]
            else:
                state = ["static✓" if reg.has_variant(mid, "static") else "static✗"]
            print(f"  {mid:20} {m['label']}")
            print(f"      {m['desc']}")
            print(f"      体积约 {m['size_mb']}MB  质量 {m['quality']}/5  速度 {m['speed']}/5"
                  f"  [{' '.join(state)}]")
        return 0

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    if reg.engine_of(args.id) == "llamacpp":
        print(f"=== 安装模型 {args.id}（llama.cpp / GGUF）===", flush=True)
        ok = install_llamacpp(args.id)
        print("=== 完成 ===" if ok else "=== 失败 ===", flush=True)
        return 0 if ok else 1
    print(f"=== 安装模型 {args.id}（变体: {','.join(kinds)}）===", flush=True)
    res = export(args.id, kinds)
    if not res:
        return 1
    ok = all(res.values())
    print("=== 完成 ===" if ok else "=== 部分失败 ===", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
