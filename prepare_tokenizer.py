"""
prepare_tokenizer.py —— 把模型的 HF tokenizer 转成 OpenVINO 图（一次即可）

为什么需要：
    `import transformers` 在冷启动时要 4~20 秒（杀软扫上千个 .py 文件），
    还要多占 300MB 内存。翻译服务只需要「文本↔token id」，完全不必拖上整个 transformers。

    转换后模型目录里会多出：
        openvino_tokenizer.xml/.bin
        openvino_detokenizer.xml/.bin
    运行时用 OpenVINO 直接跑这两个小图，耗时 ~0.05s。

用法:
    python prepare_tokenizer.py                    # 处理所有缺少 tokenizer 图的模型
    python prepare_tokenizer.py --id opus-mt-en-zh
"""
import argparse
import os
import sys
import time

import openvino as ov
import openvino_tokenizers

import model_registry as reg


def prepare(model_id: str, force: bool = False) -> None:
    kind = "static" if reg.has_variant(model_id, "static") else "cached"
    mdir = reg.dir_for(model_id, kind)
    tok_xml = os.path.join(mdir, "openvino_tokenizer.xml")
    detok_xml = os.path.join(mdir, "openvino_detokenizer.xml")
    if os.path.exists(tok_xml) and os.path.exists(detok_xml) and not force:
        print(f"[skip] {model_id}: 已有 openvino_tokenizer.xml / openvino_detokenizer.xml")
        return

    from transformers import AutoTokenizer      # 只在这里用一次

    src_lang = reg.meta(model_id).get("src_lang")
    kw = {"src_lang": src_lang} if src_lang else {}
    t = time.time()
    hf_tok = AutoTokenizer.from_pretrained(mdir, **kw)
    print(f"[..] {model_id}: 载入 HF tokenizer {time.time()-t:.2f}s")

    t = time.time()
    tok_model, detok_model = openvino_tokenizers.convert_tokenizer(
        hf_tok, with_detokenizer=True
    )
    ov.save_model(tok_model, tok_xml, compress_to_fp16=False)
    ov.save_model(detok_model, detok_xml, compress_to_fp16=False)
    print(f"[ok] {model_id}: 已生成 {os.path.basename(tok_xml)} "
          f"({os.path.getsize(tok_xml)/2**20:.1f}MB) / "
          f"{os.path.basename(detok_xml)} ({time.time()-t:.2f}s)")

    # 自检：编码结果必须与 HF 完全一致
    core = ov.Core()
    cm = core.compile_model(tok_xml, "CPU")
    samples = [
        "The quick brown fox jumps over the lazy dog.",
        "Intel Core Ultra 7 255HX integrates an NPU for local AI inference.",
        "Click the button to download the report.",
    ]
    ok = True
    for s in samples:
        ref = hf_tok(s)["input_ids"]
        got = [int(x) for x in cm([s])[list(cm.outputs)[0]][0]]
        got = [g for g in got if g != (hf_tok.pad_token_id or -1)]
        if ref != got:
            ok = False
            print(f"  [MISMATCH] {s!r}\n    hf ={ref[:12]}\n    ov ={got[:12]}")
    print(f"[{'ok' if ok else 'FAIL'}] {model_id}: 编码自检 "
          f"{'全部一致' if ok else '存在差异（勿使用）'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="只处理指定模型")
    ap.add_argument("--force", action="store_true", help="已存在也重新生成")
    args = ap.parse_args()
    ids = [args.id] if args.id else [m for m in reg.MODELS if reg.installed(m)]
    for mid in ids:
        if not reg.installed(mid):
            print(f"[skip] {mid}: 未安装")
            continue
        prepare(mid, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
