"""
pipelines.py —— OpenVINO 翻译管线（模型无关，可切换模型与设备）

两种实现：
  StaticPipeline —— 无状态 + 全静态形状解码。NPU 必需；CPU/GPU 也能跑
  CachedPipeline —— 带 KV-cache 的常规导出（optimum-intel）。CPU/GPU 更快

为什么 NPU 必须走静态：Intel NPU 驱动对动态形状会崩溃
（本项目实测 OpenVINO 2025.1 ~ 2026.3.1 六个版本全部崩溃），
而 --disable-stateful 导出的 decoder 是纯函数：
    (input_ids, encoder_attention_mask, encoder_hidden_states) -> logits
把输入固定成静态形状后，借助因果注意力（位置 i 只看 <= i）在尾部填充即可正确解码。

关于 tokenizer（2026-11 优化）：
  这里**不再 import transformers**。冷启动时那一个 import 要 4~20 秒
  （首次还要杀软扫上千个 .py），并多占 ~300MB 内存 / ~900MB 提交。
  现在改成两条零依赖路径：
    * OpenVINO 预导出 tokenizer 图（openvino_tokenizer.xml）—— 0.05s
    * SentencePiece + vocab.json 手工复刻 MarianTokenizer —— 0.05s
  两条路径都已在 prepare_tokenizer.py / 实测脚本里与 HF 输出逐 token 对齐。
"""
import gc
import json
import os
import re
import time

import numpy as np
import openvino as ov

try:                       # 注册 tokenizer 的自定义 opset；必须在 ov.Core() 之前完成
    import openvino_tokenizers  # noqa: F401
except Exception:          # 未安装时退回 SentencePiece / HF tokenizer
    openvino_tokenizers = None

import model_registry as reg

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "ov_cache")      # OpenVINO 编译缓存（跨进程复用）

_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_SPM_SPACE = "\u2581"                          # SentencePiece 的词界符号 ▁
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\u3000-\u303f\uff00-\uffef"
_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK}]) +(?=[{_CJK}])")


def tidy(text: str) -> str:
    """收尾：把残留的 ▁ 换成空格，并去掉中文之间的空格。"""
    if _SPM_SPACE in text:
        text = text.replace(_SPM_SPACE, " ")
    return _CJK_SPACE_RE.sub("", text).strip()


def split_sentences(text: str):
    """按句切分（中英文标点都支持），保留顺序。"""
    return [p.strip() for p in _SPLIT.split(text) if p.strip()]


def _load_cfg(model_dir: str) -> dict:
    with open(os.path.join(model_dir, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _first_int(*vals):
    for v in vals:
        if isinstance(v, int):
            return v
    return None


# ---------------------------------------------------------------------------
# tokenizer：两种零依赖实现
# ---------------------------------------------------------------------------
class MarianTokenizer:
    """SentencePiece + vocab.json 复刻 transformers 的 MarianTokenizer。

    与 HF 的差异只有一处：HF 会在末尾补 </s>(0)，这里也补，保持一致。
    实测（probe_tok.py）编码结果与 HF 逐 token 相同。
    """

    def __init__(self, model_dir: str):
        import sentencepiece                      # 0.07s，比 transformers 便宜两个数量级

        self.sp = sentencepiece.SentencePieceProcessor(
            model_file=os.path.join(model_dir, "source.spm"))
        with open(os.path.join(model_dir, "vocab.json"), encoding="utf-8") as f:
            self.vocab = json.load(f)
        self.inv = {v: k for k, v in self.vocab.items()}
        cfg = {}
        try:
            with open(os.path.join(model_dir, "tokenizer_config.json"), encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
        self.pad_id = _first_int(cfg.get("pad_token_id"), 65000)
        self.eos_id = _first_int(cfg.get("eos_token_id"), 0)
        self.unk_id = _first_int(cfg.get("unk_token_id"), 3)

    def encode(self, text: str):
        ids = [self.vocab.get(p, self.unk_id) for p in self.sp.encode_as_pieces(text)]
        ids.append(self.eos_id)                   # HF 恒定补 </s>
        return ids

    def decode(self, ids, skip_special_tokens: bool = True) -> str:
        drop = {self.pad_id, self.eos_id}
        pieces = [self.inv.get(int(i), "") for i in ids if not (skip_special_tokens and int(i) in drop)]
        return "".join(pieces).replace(_SPM_SPACE, " ").strip()

    def token_id(self, token: str):
        return self.vocab.get(token)


class OVTokenizer:
    """OpenVINO 预导出的 tokenizer / detokenizer 图（optimum-cli 会一并生成）。"""

    def __init__(self, model_dir: str, core: ov.Core):
        tok_xml = os.path.join(model_dir, "openvino_tokenizer.xml")
        detok_xml = os.path.join(model_dir, "openvino_detokenizer.xml")
        # 必须 read_model 再 compile：compile_model(路径) 不会带上 tokenizers 的扩展操作集
        self.tok = core.compile_model(core.read_model(tok_xml), "CPU")
        self.detok = None
        if os.path.exists(detok_xml):
            self.detok = core.compile_model(core.read_model(detok_xml), "CPU")
        self._ids_out = list(self.tok.outputs)[0]
        self._detok_in = list(self.detok.inputs)[0].get_any_name() if self.detok else None
        self._detok_out = list(self.detok.outputs)[0] if self.detok else None

        cfg = {}
        for name in ("config.json", "generation_config.json"):
            try:
                with open(os.path.join(model_dir, name), encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception:
                pass
        self.pad_id = _first_int(cfg.get("pad_token_id"), 1)
        self.eos_id = _first_int(cfg.get("eos_token_id"), 2)
        self.unk_id = _first_int(cfg.get("unk_token_id"), -1)
        # 语言码 -> id（NLLB 用），没有 vocab 表时留空
        self._tok_json = None
        p = os.path.join(model_dir, "tokenizer.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    self._tok_json = json.load(f).get("model", {}).get("vocab")
            except Exception:
                self._tok_json = None

    def encode(self, text: str):
        out = self.tok([text])[self._ids_out]
        return [int(x) for x in np.asarray(out).reshape(-1)]

    def decode(self, ids, skip_special_tokens: bool = True) -> str:
        arr = np.asarray(ids, dtype=np.int64).reshape(1, -1)
        if self.detok is None:
            return " ".join(str(int(i)) for i in ids)
        if skip_special_tokens:
            arr = np.asarray([i for i in (int(x) for x in np.asarray(ids).reshape(-1))
                              if i not in (self.pad_id, self.eos_id)], dtype=np.int64).reshape(1, -1)
            if arr.size == 0:
                return ""
        res = self.detok({self._detok_in: arr})
        out = res[self._detok_out]
        return tidy(str(np.asarray(out).reshape(-1)[0]))

    def token_id(self, token: str):
        if self._tok_json is None:
            return None
        v = self._tok_json.get(token)
        return int(v) if isinstance(v, int) else None


def make_tokenizer(model_dir: str, core: ov.Core):
    if os.path.exists(os.path.join(model_dir, "openvino_tokenizer.xml")):
        return OVTokenizer(model_dir, core)
    if os.path.exists(os.path.join(model_dir, "source.spm")):
        return MarianTokenizer(model_dir)
    from transformers import AutoTokenizer      # 兜底：不应发生
    return AutoTokenizer.from_pretrained(model_dir)


# ---------------------------------------------------------------------------
# 管线
# ---------------------------------------------------------------------------
def _new_core() -> ov.Core:
    """带编译缓存的 Core。

    NPU / GPU 的编译结果会缓存在 CACHE_DIR，二次启动直接导入，
    省掉几十秒的编译（NPU 首次编译 NLLB 实测 40s+）。
    """
    core = ov.Core()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        core.set_property({"CACHE_DIR": CACHE_DIR})
    except Exception:
        pass
    return core


class _Base:
    kind = ""
    dev = None
    compile_time = 0.0

    def __init__(self, model_dir: str, device: str, model_id: str):
        self.model_dir = model_dir
        self.device = device
        self.model_id = model_id
        m = reg.meta(model_id)
        self.src_lang = m.get("src_lang")
        self.tgt_lang = m.get("tgt_lang")
        self.src_len = int(m.get("src_len", 128))
        self.dec_len = int(m.get("dec_len", 64))
        self.core = _new_core()
        self.tok = make_tokenizer(model_dir, self.core)

    def translate(self, text: str) -> str:      # pragma: no cover
        raise NotImplementedError


class StaticPipeline(_Base):
    """无 KV-cache、全静态形状解码。可编译到 NPU。"""

    kind = "static"

    def __init__(self, model_dir: str, device: str, model_id: str):
        super().__init__(model_dir, device, model_id)
        cfg = _load_cfg(model_dir)
        self.d_model = _first_int(cfg.get("d_model"), cfg.get("hidden_size"), 512)

        core = self.core
        enc = core.read_model(os.path.join(model_dir, "openvino_encoder_model.xml"))
        enc.reshape({"input_ids": [1, self.src_len], "attention_mask": [1, self.src_len]})

        # 按实际输入名 reshape，避免写死（Marian 与 NLLB 的 hidden size 不同）
        dec = core.read_model(os.path.join(model_dir, "openvino_decoder_model.xml"))
        shapes = {}
        for inp in dec.inputs:
            n = inp.get_any_name()
            if n == "input_ids":
                shapes[n] = [1, self.dec_len]
            elif n == "encoder_attention_mask":
                shapes[n] = [1, self.src_len]
            elif n == "encoder_hidden_states":
                shapes[n] = [1, self.src_len, self.d_model]
            else:
                raise RuntimeError(f"未预期的 decoder 输入: {n} {inp.partial_shape}")
        dec.reshape(shapes)

        # NPU_TURBO 实测可让 decoder 单次推理从 ~20.5ms 降到 ~11.3ms
        turbo = {"NPU_TURBO": "YES"} if str(device).upper() == "NPU" else {}
        t0 = time.time()
        self.enc = core.compile_model(enc, device, turbo)
        self.dec = core.compile_model(dec, device, turbo)
        self.compile_time = time.time() - t0
        self.dev = self.dec.get_property("EXECUTION_DEVICES")
        self.logits = self.dec.output("logits")

        # 编译完立刻丢掉 IR：NPU 上两份权重同时存在会白占 1~2GB
        del enc, dec
        gc.collect()

        self.pad_id = _first_int(cfg.get("pad_token_id"), 1)
        self.eos_id = _first_int(cfg.get("eos_token_id"), 2)
        start = _first_int(cfg.get("decoder_start_token_id"), self.pad_id)

        # 起始 token 序列：不同模型规则不同（实测得出）
        #   Marian      : [decoder_start(=pad)]
        #   NLLB/M2M100 : [decoder_start, 目标语言码]  —— 少了语言码会退化成别的语种
        self.start_ids = [start]
        if self.tgt_lang:
            tid = self.tok.token_id(self.tgt_lang)
            if isinstance(tid, int) and tid >= 0 and tid != self.tok.unk_id:
                self.start_ids = [start, tid]

    @property
    def start_id(self):
        return self.start_ids[0]

    def translate(self, text: str) -> str:
        ids = self.tok.encode(text)[:self.src_len]
        n = len(ids)
        enc_ids = np.zeros((1, self.src_len), dtype=np.int64)
        enc_ids[0, :n] = ids
        enc_mask = np.zeros((1, self.src_len), dtype=np.int64)
        enc_mask[0, :n] = 1

        hidden = self.enc({"input_ids": enc_ids, "attention_mask": enc_mask})[self.enc.output(0)]

        tokens = np.full((1, self.dec_len), self.pad_id, dtype=np.int64)
        for i, v in enumerate(self.start_ids[:self.dec_len]):
            tokens[0, i] = v
        cur = min(len(self.start_ids), self.dec_len) - 1   # 最后一个真实位置
        out = []
        while cur < self.dec_len - 1:
            lg = self.dec({
                "input_ids": tokens,
                "encoder_attention_mask": enc_mask,
                "encoder_hidden_states": hidden,
            })[self.logits]
            nxt = int(np.argmax(lg[0, cur, :]))
            cur += 1
            if nxt == self.eos_id:
                break
            out.append(nxt)
            tokens[0, cur] = nxt
        return self.tok.decode(out).strip()


class CachedPipeline(_Base):
    """带 KV-cache 的常规管线，CPU / GPU 更快。"""

    kind = "cached"

    def __init__(self, model_dir: str, device: str, model_id: str):
        super().__init__(model_dir, device, model_id)
        from optimum.intel import OVModelForSeq2SeqLM     # 只在真用这条路径时才拉起来

        t0 = time.time()
        self.model = OVModelForSeq2SeqLM.from_pretrained(model_dir, device=device)
        self.compile_time = time.time() - t0
        self.dev = device

        self.forced_bos = None
        if self.tgt_lang:
            tid = self.tok.token_id(self.tgt_lang)
            if isinstance(tid, int) and tid >= 0:
                self.forced_bos = tid

    def translate(self, text: str) -> str:
        inputs = self.tok(text, return_tensors="pt")     # 仅 CachedPipeline 走 HF tokenizer
        kw = {"max_new_tokens": self.dec_len, "num_beams": 1}
        if self.forced_bos is not None:
            kw["forced_bos_token_id"] = self.forced_bos
        out = self.model.generate(**inputs, **kw)
        return self.tok.batch_decode(out, skip_special_tokens=True)[0].strip()


def build(model_id: str, device: str) -> _Base:
    """按模型与设备选择变体并构建管线。"""
    kind, model_dir = reg.resolve(model_id, device)
    cls = StaticPipeline if kind == "static" else CachedPipeline
    return cls(model_dir, device, model_id)
