"""
model_registry.py —— 可切换翻译模型的注册表

每个模型有两个 OpenVINO 变体：
  * cached  ：带 KV-cache 的常规导出，适合 CPU / GPU，速度快
  * static  ：无状态 + 全静态形状导出，NPU 必需（NPU 驱动对动态形状会崩，见实测报告）

只有 static 变体时，CPU/GPU 也能跑（走静态管线，稍慢）。
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ROOT, "models")
SETTINGS_FILE = os.path.join(ROOT, "settings.json")

DEFAULT_MODEL = "index-translate-2b"

MODELS = {
    "opus-mt-en-zh": {
        "label": "OPUS-MT 英→中（快速）",
        "short": "OPUS-MT",
        "desc": "轻量专用翻译模型，速度最快，适合整页翻译与日常浏览",
        "engine": "openvino",
        "hf": "Helsinki-NLP/opus-mt-en-zh",
        "license": "CC-BY-4.0",
        "size_mb": 112,
        "quality": 2,
        "speed": 5,
        "cached_dir": "opus-en-zh-int8-ov",
        "static_dir": "opus-en-zh-int8-stateless-ov",
        "src_lang": None,          # Marian 不需要语言码
        "tgt_lang": None,
        "src_len": 128,            # encoder 固定长度
        "dec_len": 64,             # decoder 最大生成长度
    },
    "index-translate-2b": {
        "label": "Index-Translate 2B（B站·高质量）",
        "short": "Index-2B",
        "desc": "哔哩哔哩开源的 150 语种翻译大模型（Qwen3.5 底座）。质量最好，"
                "走 llama.cpp 引擎，可在 CPU/GPU 上跑；整页翻译较慢，适合选中段落",
        "engine": "llamacpp",
        "hf": "IndexTeam/Index-Translate-2B-GGUF",
        "license": "Apache-2.0",
        "size_mb": 1251,
        "quality": 5,
        "speed": 2,
        "gguf": "index-translate-2b-gguf/Index-Translate-2B.Q4_K_M.gguf",
        "target_lang": "中文",
        "source_lang": "英文",
        "ctx": 2048,
        "threads": 0,              # 0 = 自动（物理核数）
    },
}


def exists(model_id: str) -> bool:
    return model_id in MODELS


def meta(model_id: str) -> dict:
    return MODELS.get(model_id) or MODELS[DEFAULT_MODEL]


def dir_for(model_id: str, kind: str) -> str:
    """kind: 'cached' | 'static'"""
    key = "cached_dir" if kind == "cached" else "static_dir"
    return os.path.join(MODELS_DIR, meta(model_id).get(key, ""))


def engine_of(model_id: str) -> str:
    """'openvino'（OpenVINO IR 管线）或 'llamacpp'（GGUF + llama.cpp 引擎）。"""
    return meta(model_id).get("engine", "openvino")


def gguf_path(model_id: str) -> str:
    g = meta(model_id).get("gguf")
    return os.path.join(MODELS_DIR, g.replace("/", os.sep)) if g else ""


def has_variant(model_id: str, kind: str) -> bool:
    d = dir_for(model_id, kind)
    return os.path.exists(os.path.join(d, "openvino_encoder_model.xml"))


def installed(model_id: str) -> bool:
    if engine_of(model_id) == "llamacpp":
        p = gguf_path(model_id)
        return bool(p) and os.path.exists(p)
    return has_variant(model_id, "static") or has_variant(model_id, "cached")


def devices_for(model_id: str):
    """该模型当前实际可用的推理设备。"""
    if engine_of(model_id) == "llamacpp":
        return ["CPU", "GPU"]        # llama.cpp 版：CPU 或 CUDA（装了 CUDA 包后）
    devs = []
    if has_variant(model_id, "static"):
        devs.append("NPU")
    if has_variant(model_id, "cached") or has_variant(model_id, "static"):
        devs += ["CPU", "GPU"]
    return devs


def resolve(model_id: str, device: str):
    """决定用哪个变体目录；返回 (kind, dir)。

    2026-11 起**统一优先用 static 变体**（所有设备）：
      * NPU 只能跑 static（驱动限制）
      * CPU/GPU 跑 static 只慢一点点，却省掉 optimum-intel + torch
        —— 那套东西光 import 就要 4~20 秒、多占 300MB 内存，是启动慢的主因之一
      只有 static 缺失时才退回 cached（老目录）。
    """
    device = (device or "NPU").upper()
    if has_variant(model_id, "static"):
        return "static", dir_for(model_id, "static")
    if device == "NPU":
        raise FileNotFoundError(
            f"模型 {model_id} 缺少 NPU 所需的全静态变体（{meta(model_id)['static_dir']}）"
        )
    if has_variant(model_id, "cached"):
        return "cached", dir_for(model_id, "cached")
    raise FileNotFoundError(f"模型 {model_id} 未安装")


def describe_all(active: str):
    out = []
    for mid, m in MODELS.items():
        out.append({
            "id": mid,
            "label": m["label"],
            "short": m["short"],
            "desc": m["desc"],
            "engine": m.get("engine", "openvino"),
            "hf": m["hf"],
            "license": m["license"],
            "size_mb": m["size_mb"],
            "quality": m["quality"],
            "speed": m["speed"],
            "installed": installed(mid),
            "has_cached": has_variant(mid, "cached") if m.get("engine") != "llamacpp" else False,
            "has_static": has_variant(mid, "static") if m.get("engine") != "llamacpp" else False,
            "devices": devices_for(mid),
            "active": mid == active,
        })
    return out


def load_settings() -> dict:
    import json
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    import json
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_active() -> str:
    mid = load_settings().get("model")
    if mid and exists(mid):
        return mid
    return DEFAULT_MODEL


def set_active(model_id: str) -> None:
    s = load_settings()
    s["model"] = model_id
    save_settings(s)
