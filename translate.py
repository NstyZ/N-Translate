"""
translate.py —— 英文 → 中文 本地实时/流式翻译（OpenVINO），支持 Intel NPU

设备与模型选择：
  * --model  选择翻译模型（见 --list-models），默认用设置里上次选中的模型
  * --device NPU / CPU / GPU / AUTO
        NPU  -> 必须走「无状态 + 全静态形状」变体（驱动对动态形状会崩）
        CPU/GPU -> 优先带 KV-cache 的常规变体（更快）

用法:
  python translate.py --list-models
  python translate.py --text "Hello, how are you today?"
  python translate.py --device NPU --model opus-mt-en-zh --bench
  python translate.py --file in.txt --out out.txt
"""
import argparse
import os
import re
import sys
import time

import openvino as ov

import model_registry as reg
import pipelines

BENCH = [
    "Hello, how are you today?",
    "The weather is really nice this afternoon.",
    "Please send me the report before Friday.",
    "This machine learning model runs entirely on your local device.",
    "Translation quality depends on the size and training data of the model.",
]

_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def split_sentences(text):
    return [p.strip() for p in _SPLIT.split(text) if p.strip()]


def print_models(active):
    print(f"当前模型: {active}\n")
    for mid, m in reg.MODELS.items():
        flags = []
        flags.append("cached✓" if reg.has_variant(mid, "cached") else "cached✗")
        flags.append("static✓" if reg.has_variant(mid, "static") else "static✗")
        mark = "*" if mid == active else " "
        print(f" {mark} {mid:16} {m['label']}")
        print(f"     {m['desc']}")
        print(f"     约 {m['size_mb']}MB · 质量 {m['quality']}/5 · 速度 {m['speed']}/5"
              f" · 设备 {','.join(reg.devices_for(mid)) or '未安装'} [{' '.join(flags)}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="模型 ID（默认取设置里上次选中的）")
    ap.add_argument("--device", default=None, help="NPU / CPU / GPU / AUTO")
    ap.add_argument("--text")
    ap.add_argument("--file")
    ap.add_argument("--out")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--list-models", action="store_true")
    args = ap.parse_args()

    active = reg.get_active()
    if args.list_models:
        print_models(active)
        return 0

    model_id = args.model or active
    if not reg.exists(model_id):
        print(f"未知模型: {model_id}（可用: {', '.join(reg.MODELS)}）", file=sys.stderr)
        return 2
    if not reg.installed(model_id):
        print(f"模型 {model_id} 未安装。请先运行：python install_model.py --id {model_id}",
              file=sys.stderr)
        return 2

    device = (args.device or ("NPU" if reg.has_variant(model_id, "static") else "CPU")).upper()
    core = ov.Core()
    print(f"OpenVINO {ov.__version__} | devices={core.available_devices}", file=sys.stderr)
    print(f"loading {model_id} on {device} ...", file=sys.stderr)
    try:
        tr = pipelines.build(model_id, device)
    except Exception as e:
        print(f"BUILD FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(f"ready in {tr.compile_time:.2f}s | kind={tr.kind} | EXECUTION_DEVICES={tr.dev}",
          file=sys.stderr)

    tr.translate("warm up")   # 预热

    if args.bench:
        lat = []
        print("\n=== benchmark ===", flush=True)
        for s in BENCH:
            t0 = time.time()
            out = tr.translate(s)
            dt = time.time() - t0
            lat.append(dt)
            print(f"[{dt*1000:6.0f} ms] {s}\n            -> {out}", flush=True)
        total = sum(lat)
        print(f"\nsentences={len(lat)} total={total:.2f}s "
              f"avg={total/len(lat)*1000:.0f} ms/句 throughput={len(lat)/total:.2f} 句/秒", flush=True)
        return 0

    fout = open(args.out, "w", encoding="utf-8") if args.out else None

    def handle(sent):
        t0 = time.time()
        dst = tr.translate(sent)
        dt = time.time() - t0
        print(f"\nEN: {sent}\nZH: {dst}   ({dt*1000:.0f} ms)", flush=True)
        if fout:
            fout.write(dst + "\n")

    if args.text:
        for s in split_sentences(args.text):
            handle(s)
    elif args.file:
        with open(args.file, encoding="utf-8") as f:
            for s in split_sentences(f.read()):
                handle(s)
    else:
        print("交互模式：输入英文回车即出中文；Ctrl+Z 回车 结束。", file=sys.stderr)
        for line in sys.stdin:
            for s in split_sentences(line):
                handle(s)

    if fout:
        fout.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
