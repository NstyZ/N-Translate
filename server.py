"""
server.py —— 本地翻译 HTTP 服务（供 Chrome 插件调用）

  * 推理全部走本机：默认 --device NPU（全静态形状管线），可选 CPU / GPU / AUTO
  * 只绑定 127.0.0.1，不对外暴露
  * 接口：
      GET  /health          -> {"ok":true,"device":...,"execution_devices":...}
      POST /translate       -> body {"text": "..."}           单段文本，逐句翻译
      POST /translate_batch -> body {"texts": ["...", ...]}   批量（整页翻译用），保序返回
      OPTIONS  *            （CORS 预检，允许浏览器扩展调用）

用法:
  python server.py --device NPU --port 8765
"""
import argparse
import atexit
import gc
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openvino as ov

import model_registry as reg
import pipelines
from pipelines import split_sentences

ROOT = os.path.dirname(os.path.abspath(__file__))

DEVICE = "NPU"
PORT = 8765
LOCK = threading.Lock()          # 单个 infer request，非线程安全，需串行化
PIPELINES = {}                   # (model_id, device) -> 已构建的管线，复用
BUILD_Q = queue.Queue()          # 主线程任务队列（编译 + 推理都在主线程做）
STOP = threading.Event()
ACTIVE = {"model": reg.DEFAULT_MODEL}
STATE = {"ready": False, "loading": False, "error": None, "started_at": time.time()}


def run_on_main(fn, timeout: float = 900.0):
    """把 fn 放到主线程执行并等待结果。

    实测：Intel NPU 的**编译与推理都必须发生在主线程**。一旦在工作线程里做过 NPU 推理，
    之后再编译另一个模型就会得到 ZE_RESULT_ERROR_INVALID_NATIVE_BINARY（推理时报错）。
    因此 HTTP 仍然是多线程的（保证 /health 等可响应），但所有碰管线的操作都在主线程完成。
    """
    if threading.current_thread() is threading.main_thread():
        return fn()
    box, ev = {}, threading.Event()
    BUILD_Q.put((fn, box, ev))
    if not ev.wait(timeout=timeout):
        raise TimeoutError("等待主线程执行超时")
    if "error" in box:
        raise box["error"]
    return box["result"]
HTTPD = None                     # ThreadingHTTPServer 实例（供 /shutdown 使用）


def release_pipelines(device: str = None):
    """释放已编译的管线。

    NPU 驱动限制：同一进程内同时存在两个已编译的 NPU 模型，推理时会报
    ZE_RESULT_ERROR_INVALID_NATIVE_BINARY（实测确认），因此切换前必须先释放旧管线。
    """
    for k in [k for k in PIPELINES if device is None or k[1] == device]:
        pipe = PIPELINES.pop(k, None)
        del pipe
    gc.collect()


RESTART_FLAG = os.path.join(ROOT, "restart.flag")


def stop_for_restart():
    """切换到新模型时：写重启标记并退出，由启动器用新模型重新拉起。

    为什么不能在本进程内换模型：实测 Intel NPU 驱动在同一进程内无法换用第二个已编译
    模型——换模型后推理报 ZE_RESULT_ERROR_INVALID_NATIVE_BINARY。进程重启可彻底规避。
    谁来拉起：start_server.bat 会检测到 restart.flag 并自动重启；插件则通过
    Native Messaging 宿主重新启动服务（服务启动时读取 settings.json 里的模型）。
    """
    print(f"[restart] 已切换到 {ACTIVE['model']}：写入 restart.flag 并退出，"
          f"等待启动器重新拉起", flush=True)
    try:
        with open(RESTART_FLAG, "w", encoding="utf-8") as f:
            f.write(ACTIVE["model"])
    except Exception as e:
        print(f"[restart] 写标记失败: {e}", flush=True)
    llama_stop()
    if HTTPD is not None:
        HTTPD.shutdown()
    STOP.set()
    BUILD_Q.put(None)


def _build_now(model_id: str, device: str):
    """真正构建管线。必须在主线程调用（见 get_pipeline 的说明）。"""
    if device.upper() == "NPU":
        release_pipelines("NPU")
    pipe = pipelines.build(model_id, device)
    PIPELINES[(model_id, device.upper())] = pipe
    return pipe


def get_pipeline(model_id: str = None, device: str = None):
    """按 (模型, 设备) 复用管线。NPU 上编译/释放都必须在主线程，故整体投递给主线程。"""
    model_id = model_id or ACTIVE["model"]
    device = (device or DEVICE).upper()
    key = (model_id, device)
    pipe = PIPELINES.get(key)
    if pipe is not None:
        return pipe
    return run_on_main(lambda: _ensure_pipeline(key, model_id, device))


# ---------------------------------------------------------------------------
# llama.cpp 引擎（GGUF 大模型，跑在独立进程 llama-server 上）
#
# 为什么要独立进程：llama.cpp 是 C++ 程序，没有 Python 绑定；而且和 OpenVINO 的
# NPU 驱动放在同一进程里会互相抢设备。拆成子进程后，两边互不影响：
#   * NPU 上的小模型（OPUS-MT）继续负责整页翻译
#   * llama-server 负责高质量模式，随时可以关掉、内存立刻归还
# ---------------------------------------------------------------------------
LLAMA_PORT = 8766
LLAMA = {"proc": None, "ready": False}


def llama_exe() -> str:
    """优先用 CUDA 版（llama\\cuda\\，需要 NVIDIA 显卡 + 已放好 CUDA 依赖），否则 CPU 版。

    CUDA 版实测在 RTX 5060 上是 156 tok/s，CPU 版只有 26 tok/s，差 6 倍。
    """
    cuda = os.path.join(ROOT, "llama", "cuda", "llama-server.exe")
    if os.path.exists(cuda):
        return cuda
    return os.path.join(ROOT, "llama", "llama-server.exe")


def llama_running() -> bool:
    p = LLAMA["proc"]
    return bool(p is not None and p.poll() is None)


def llama_health() -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{LLAMA_PORT}/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("status") == "ok"
    except Exception:
        return False


def llama_stop():
    global LLAMA
    p = LLAMA["proc"]
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    LLAMA = {"proc": None, "ready": False}


def llama_ensure(timeout: float = 180.0):
    """确保 llama-server 已就绪；返回 True/False。"""
    if llama_running() and llama_health():
        LLAMA["ready"] = True
        return True

    model_id = ACTIVE["model"]
    m = reg.meta(model_id)
    gguf = reg.gguf_path(model_id)
    if not os.path.exists(llama_exe()):
        raise RuntimeError(f"缺少 llama-server.exe（应为 {llama_exe()}）")
    if not os.path.exists(gguf):
        raise RuntimeError(f"模型未安装：{gguf}")

    py_threads = int(m.get("threads") or 0)
    if py_threads <= 0:
        try:
            py_threads = max(4, min(16, (os.cpu_count() or 8) - 4))
        except Exception:
            py_threads = 8

    log_path = os.path.join(ROOT, "llama_server.log")
    logf = open(log_path, "ab", buffering=0)
    flags = (0x00000008 | 0x08000000) if os.name == "nt" else 0   # DETACHED | NO_WINDOW
    exe = llama_exe()
    use_cuda = os.sep + "cuda" + os.sep in exe
    print(f"[llama] 启动 llama-server（{'CUDA' if use_cuda else 'CPU'}，"
          f"{os.path.basename(gguf)}，{py_threads} 线程）", flush=True)
    argv = [exe, "-m", gguf,
            "--host", "127.0.0.1", "--port", str(LLAMA_PORT),
            "-c", str(m.get("ctx", 4096)), "-t", str(py_threads),
            "--jinja", "-b", "512", "-ub", "512"]
    if use_cuda:
        argv += ["-ngl", "99"]          # 全部层放显卡
    proc = subprocess.Popen(
        argv,
        cwd=os.path.dirname(exe), stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
        creationflags=flags, close_fds=True,
    )
    LLAMA["proc"] = proc
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            LLAMA["ready"] = False
            raise RuntimeError(f"llama-server 启动失败（退出码 {proc.returncode}），见 llama_server.log")
        if llama_health():
            LLAMA["ready"] = True
            print(f"[llama] 就绪，用时 {time.time()-t0:.1f}s", flush=True)
            return True
        time.sleep(0.4)
    raise TimeoutError("llama-server 启动超时")


_LLM_TPL = {"tpl": None, "model": None}


def llm_prompt(text: str) -> str:
    """按 Index-Translate 官方 chat template 渲染单轮翻译提示词。"""
    m = reg.meta(ACTIVE["model"])
    gguf_dir = os.path.dirname(reg.gguf_path(ACTIVE["model"]))
    if _LLM_TPL["tpl"] is None or _LLM_TPL["model"] != ACTIVE["model"]:
        import jinja2
        path = os.path.join(gguf_dir, "chat_template.jinja")
        with open(path, encoding="utf-8") as f:
            _LLM_TPL["tpl"] = jinja2.Environment(
                trim_blocks=True, lstrip_blocks=True).from_string(f.read())
        _LLM_TPL["model"] = ACTIVE["model"]
    src = m.get("source_lang", "英文")
    tgt = m.get("target_lang", "中文")
    body = f"请将以下{src}文本翻译为{tgt}，直接输出翻译结果，不要进行任何解释。\n\n{text}"
    return _LLM_TPL["tpl"].render(
        messages=[{"role": "user", "content": body}],
        add_generation_prompt=True, enable_thinking=False, add_vision_id=False)


_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_SPECIAL_RE = re.compile(r"<\|[^|>]*\|>")


def _clean_llm(out: str) -> str:
    out = _THINK_RE.sub("", out or "")
    out = _SPECIAL_RE.sub("", out)
    return out.strip()


def llm_translate(text: str) -> str:
    if not llama_running():
        llama_ensure()
    payload = {
        "prompt": llm_prompt(text),
        "n_predict": 1024,
        "temperature": 0.0,
        "top_k": 1,
        "stop": ["<|im_end|>", "<|endoftext|>"],
        "cache_prompt": True,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{LLAMA_PORT}/completion",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        res = json.loads(r.read().decode("utf-8"))
    return _clean_llm(res.get("content", ""))


def _ensure_pipeline(key, model_id: str, device: str):
    pipe = PIPELINES.get(key)
    if pipe is not None:
        return pipe
    return _build_now(model_id, device)


# ---------------------------------------------------------------------------
# 主线程执行的任务体（取管线 + 翻译）
# ---------------------------------------------------------------------------
def job_batch(texts):
    if reg.engine_of(ACTIVE["model"]) == "llamacpp":
        return [translate_one_llm(t) for t in texts], "llama.cpp"
    pipe = get_pipeline()
    return [translate_one(pipe, t) for t in texts], getattr(pipe, "dev", None)


def job_single(text):
    if reg.engine_of(ACTIVE["model"]) == "llamacpp":
        sentences = split_sentences(text)[:MAX_SENTENCES_PER_ITEM]
        out, lat = [], []
        for s in sentences:
            st = time.time()
            out.append(translate_one_llm(s))
            lat.append(round((time.time() - st) * 1000))
        return sentences, out, lat, "llama.cpp"
    pipe = get_pipeline()
    sentences = split_sentences(text)[:MAX_SENTENCES_PER_ITEM]
    out, lat = [], []
    for s in sentences:
        st = time.time()
        out.append(translate_sentence(pipe, s))
        lat.append(round((time.time() - st) * 1000))
    return sentences, out, lat, getattr(pipe, "dev", None)

MAX_ITEMS = 200                  # 单次批量请求最多条目数（整页翻译会分块，不会撞到这个上限）
MAX_SENTENCES_PER_ITEM = 32      # 单条目最多拆多少句，防止超长段落拖死
MAX_CHARS_PER_ITEM = 4000        # 单条目最大字符数（超出则截断）


# ---------------------------------------------------------------------------
# 常见 UI 词表：超短字符串（导航/按钮）小模型容易译错或复读，精确命中时直接给固定译文
# ---------------------------------------------------------------------------
GLOSSARY = {
    "home": "首页", "sign in": "登录", "log in": "登录", "login": "登录",
    "sign up": "注册", "sign out": "退出登录", "log out": "退出登录", "logout": "退出登录",
    "search": "搜索", "search documentation": "搜索文档", "settings": "设置",
    "preferences": "偏好设置", "help": "帮助", "about": "关于", "about us": "关于我们",
    "contact": "联系我们", "contact us": "联系我们", "support": "支持",
    "download": "下载", "downloads": "下载", "upload": "上传", "install": "安装",
    "next": "下一页", "previous": "上一页", "back": "返回", "close": "关闭",
    "cancel": "取消", "submit": "提交", "save": "保存", "delete": "删除",
    "edit": "编辑", "share": "分享", "copy": "复制", "copy link": "复制链接",
    "more": "更多", "menu": "菜单", "main menu": "主菜单", "open menu": "打开菜单",
    "profile": "个人资料", "account": "账户", "my account": "我的账户",
    "notifications": "通知", "messages": "消息", "language": "语言",
    "english": "英语", "chinese": "中文", "docs": "文档", "documentation": "文档",
    "pricing": "定价", "blog": "博客", "news": "新闻", "products": "产品",
    "solutions": "解决方案", "company": "公司", "careers": "招聘", "jobs": "职位",
    "privacy": "隐私", "privacy policy": "隐私政策", "terms": "条款",
    "terms of service": "服务条款", "cookie settings": "Cookie 设置",
    "read more": "阅读更多", "learn more": "了解更多", "get started": "开始使用",
    "view all": "查看全部", "see more": "查看更多", "show more": "显示更多",
    "load more": "加载更多", "refresh": "刷新", "retry": "重试", "reload": "重新加载",
    "back to top": "回到顶部", "page not found": "页面未找到",
    "error": "错误", "warning": "警告", "success": "成功", "loading": "加载中",
    "please wait": "请稍候", "no results": "没有结果", "not found": "未找到",
    "report a problem": "报告问题", "was this helpful": "这有帮助吗",
    "related articles": "相关文章", "table of contents": "目录",
    "share this page": "分享此页面", "version history": "版本历史",
    "release notes": "发行说明", "known issues": "已知问题", "license": "许可证",
    "subscribe": "订阅", "newsletter": "新闻通讯", "follow us": "关注我们",
    "all rights reserved": "版权所有", "skip to content": "跳到主要内容",
    "dark mode": "深色模式", "light mode": "浅色模式", "go": "前往",
    "overview": "概览", "features": "功能", "pricing plans": "定价方案",
    "documentation home": "文档首页", "getting started": "快速开始",
    "installation guide": "安装指南", "user guide": "用户指南", "faq": "常见问题",
}

_NORM_RE = re.compile(r"[^a-z0-9 ]+")
# 复读型退化输出，例如“內容內容內容”
_DEGEN_RE = re.compile(r"^(.{1,4})\1+$")

# ---------------------------------------------------------------------------
# A. 不应送进模型的字符串：技术标识
#    小模型遇到 URL / 版本号 / 文件路径这类输入会退化出「其他类型」「关键字:」这种译文，
#    比不翻更糟，所以在调用模型前直接原样返回。
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"^(?:https?|ftp|file|chrome|edge|about|view-source)://", re.I)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# 文件 / 路径：像 C:\a\b、/usr/local/bin、index.html、main.py
_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/~.]{1,2}[A-Za-z0-9_.-]+[\\/])[\w\\/.\- ]*$")
_DOTTED_FILE_RE = re.compile(r"^[\w.\-]+\.(?:py|js|mjs|ts|tsx|jsx|json|ya?ml|toml|ini|cfg|md|txt|html?|css|scss|"
                             r"xml|csv|log|bat|ps1|sh|exe|dll|zip|tar|gz|png|jpe?g|gif|svg|ico|mp[34]|wav|pdf|"
                             r"ipynb|lock|whl|pem|crx|db|sqlite|so|dylib|bin|onnx|xml)$", re.I)
# 版本号：3.2.1 / v1.3.2 / 2.0.0-beta.1
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+){1,3}(?:[-+][\w.]+)?$", re.I)
# 十六进制散列 / 长 token：SHA256、a1b2c3d4e5f6、deadbeef
_HEX_RE = re.compile(r"^(?:[0-9a-f]{7,}|[A-Z][A-Z0-9_]{3,})$")
# 键盘快捷键：Ctrl+Shift+T、Alt+T、Cmd+K
_KEY_RE = re.compile(r"^(?:Ctrl|Alt|Shift|Cmd|Meta|Win|Option|⌘|⌥|⌃)(?:\s*\+\s*[A-Za-z0-9\U0001F300-\U0001FAFF]+)+$", re.I)
# 形如 key: value / name=value / 纯符号数字
_CODEY_RE = re.compile(r"^[\w.\-]+\s*[:=]\s*\S{1,40}$")
_SYMBOLIC_RE = re.compile(r"^[\W\d_]+$")
# 中文（含中日韩统一表意文字）字符
_HAN_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")

# ---------------------------------------------------------------------------
# B. 保护词表：品牌 / 专有名词 / 技术名词
#    值为 None 表示「原样保留」，否则给固定中文译名。
#    小模型不经上下文会把这些猜歪（GitHub→"关键字:"、Chrome→"色"、OpenVINO→"开酒"），
#    所以在走模型之前精确命中就直接返回。
# ---------------------------------------------------------------------------
PROTECTED = {
    # 开发 / 平台
    "github": "GitHub", "gitlab": "GitLab", "bitbucket": None, "gitee": "Gitee",
    "git": None, "npm": None, "pnpm": None, "yarn": None, "pip": None, "pypi": None,
    "docker": None, "kubernetes": None, "k8s": None, "nginx": None, "node": None,
    "node.js": "Node.js", "nodejs": "Node.js", "deno": None, "bun": None, "webpack": None, "vite": None,
    "typescript": "TypeScript", "javascript": "JavaScript", "python": None, "java": None, "golang": None,
    "rust": "Rust", "kotlin": None, "swift": "Swift", "php": None, "ruby": None, "perl": None,
    "sql": None, "html": None, "css": None, "scss": None, "json": None, "yaml": None, "xml": None,
    "markdown": "Markdown", "regex": None, "api": "API", "sdk": "SDK", "cli": "CLI",
    "url": "URL", "uri": "URI", "http": "HTTP", "https": "HTTPS", "tcp": "TCP", "udp": "UDP",
    "ssh": "SSH", "tls": "TLS", "ssl": "SSL", "dns": "DNS", "rest": "REST", "graphql": "GraphQL",
    "grpc": "gRPC", "jwt": "JWT", "oauth": "OAuth", "cors": "CORS", "csrf": "CSRF",
    "cpu": "CPU", "gpu": "GPU", "npu": "NPU", "tpu": "TPU", "ram": "内存", "rom": "ROM",
    "ssd": "SSD", "hdd": "HDD", "usb": "USB", "bios": "BIOS", "uefi": "UEFI",
    "ai": "AI", "ml": "ML", "llm": "LLM", "nlp": "NLP", "asr": "ASR", "tts": "TTS",
    "onnx": "ONNX", "int8": "INT8", "int4": "INT4", "fp16": "FP16", "fp32": "FP32", "bf16": "BF16",
    "sha256": "SHA256", "sha1": "SHA1", "md5": "MD5", "base64": "Base64", "utf-8": "UTF-8",
    # 公司与产品（用官方中文名或原样）
    "github copilot": "GitHub Copilot", "copilot": "Copilot",
    "google": "Google", "google drive": "Google 云端硬盘", "google docs": "Google 文档",
    "google chrome": "Google Chrome", "chrome": "Chrome", "chromium": "Chromium",
    "chrome web store": "Chrome 应用商店",
    "microsoft": "Microsoft", "microsoft edge": "Microsoft Edge", "edge": "Edge",
    "windows": "Windows", "windows 11": "Windows 11", "windows 10": "Windows 10",
    "visual studio code": "Visual Studio Code", "vs code": "VS Code", "vscode": "VS Code",
    "openvino": "OpenVINO", "hugging face": "Hugging Face", "huggingface": "Hugging Face",
    "modelscope": "ModelScope", "pytorch": "PyTorch", "tensorflow": "TensorFlow",
    "transformers": "Transformers", "numpy": "NumPy", "pandas": "pandas",
    "intel": "Intel", "intel core": "Intel Core", "intel core ultra": "Intel Core Ultra",    "amd": "AMD", "ryzen": "Ryzen", "nvidia": "NVIDIA", "geforce": "GeForce", "radeon": "Radeon",
    "asus": "ASUS", "dell": "Dell", "lenovo": "Lenovo", "hp": "HP", "acer": "Acer", "msi": "MSI",
    "apple": "Apple", "macos": "macOS", "mac": "Mac", "iphone": "iPhone", "ipad": "iPad",
    "ios": "iOS", "android": "Android", "linux": "Linux", "ubuntu": "Ubuntu", "debian": "Debian",
    "arm": "ARM", "x86": "x86", "x64": "x64",
    "amazon": "Amazon", "aws": "AWS", "azure": "Azure", "cloudflare": "Cloudflare",
    "stack overflow": "Stack Overflow", "hacker news": "Hacker News",
    "visual studio": "Visual Studio", "jetbrains": "JetBrains", "pycharm": "PyCharm",
    "vscodium": "VSCodium", "firefox": "Firefox", "safari": "Safari", "opera": "Opera",
    "brave": "Brave", "tampermonkey": "Tampermonkey",
    "ibm": "IBM", "oracle": "Oracle", "sap": "SAP", "adobe": "Adobe", "figma": "Figma",
    "slack": "Slack", "discord": "Discord", "zoom": "Zoom", "notion": "Notion",
    "youtube": "YouTube", "twitter": "Twitter", "facebook": "Facebook", "instagram": "Instagram",
    "linkedin": "LinkedIn", "reddit": "Reddit", "wikipedia": "Wikipedia", "medium": "Medium",
    "telegram": "Telegram", "whatsapp": "WhatsApp", "wechat": "WeChat", "weixin": "WeChat",
    "alibaba": "Alibaba", "tencent": "Tencent", "huawei": "Huawei", "xiaomi": "Xiaomi",
    "bilibili": "Bilibili", "zhihu": "知乎", "taobao": "淘宝", "jd": "JD",
    "bing": "Bing", "baidu": "百度", "deepseek": "DeepSeek", "openai": "OpenAI",
    "chatgpt": "ChatGPT", "gpt": "GPT", "claude": "Claude", "gemini": "Gemini",
    "llama": "Llama", "qwen": "Qwen", "mistral": "Mistral", "gemma": "Gemma",
    "nllb": "NLLB", "opus-mt": "OPUS-MT", "whisper": "Whisper", "bert": "BERT",
    # UI 领域里小模型必错的常见孤词
    "star": "收藏", "fork": "派生", "watch": "关注", "sponsor": "赞助",
    "pin": "置顶", "unpin": "取消置顶", "archive": "归档", "unarchive": "取消归档",
    "merge": "合并", "rebase": "变基", "commit": "提交", "commits": "提交",
    "branch": "分支", "branches": "分支", "tag": "标签", "tags": "标签",
    "release": "发布", "releases": "发布", "issue": "议题", "issues": "议题",
    "pull request": "拉取请求", "pull requests": "拉取请求", "pr": "PR",
    "repository": "仓库", "repositories": "仓库", "repo": "仓库",
    "codespaces": "Codespaces", "actions": "Actions", "insights": "洞察",
    "security": "安全", "projects": "项目", "sponsors": "赞助", "settings": "设置",
    "cookie": "Cookie", "cookies": "Cookie", "ok": "确定", "cancel": "取消",
    "yes": "是", "no": "否", "beta": "Beta", "alpha": "Alpha", "new": "新建",
    "sign in": "登录", "sign up": "注册", "sign out": "退出登录",
}


def _looks_technical(s: str) -> bool:
    """是否是「不该翻译」的技术标识。"""
    t = s.strip()
    if not t:
        return True
    if _URL_RE.match(t) or _EMAIL_RE.match(t) or _VERSION_RE.match(t):
        return True
    if _DOTTED_FILE_RE.match(t) or _PATH_RE.match(t):
        return True
    if _KEY_RE.match(t):
        return True
    if _SYMBOLIC_RE.match(t):
        return True
    if _HEX_RE.match(t) and not t.isalpha():
        return True
    # key: value / name=value，且右侧没有句子特征（无空格）
    if _CODEY_RE.match(t) and " " not in t:
        return True
    return False


def _mostly_chinese(s: str) -> bool:
    """含大量中文的文本不再翻译（中英混排的短串被模型吞掉英文更糟）。"""
    han = len(_HAN_RE.findall(s))
    return han > 0 and han / max(len(s.strip()), 1) >= 0.25


def _norm(s: str) -> str:
    return " ".join(_NORM_RE.sub(" ", s.strip().lower()).split())


# 句内占位符保护：把品牌/技术标识替换成哨兵，翻完再换回。
# 否则「Install OpenVINO from ...」里的 OpenVINO 会被翻成「开酒」之类；
# 句级词表只能覆盖整句，覆盖不到句中的专名。
_PROTECT_TOKEN_RE = re.compile(r"[A-Za-z][\w.+#\-]*(?:[/\\][\w.\-]+)*")
# 独立数字（左右环视排除花括号，避免命中哨兵 {{0}} 里的数字）
_DIGIT_RE = re.compile(r"(?<![\w{.])\d+(?:[.\-/]\d+)*(?![\w}.])")
_PROTECTED_SORTED = None

# 句中要「保留原文」的专名与缩写（显式分类，不用启发式猜）。
# 说明：普通单词（install / star / merge / tag / release / issue / fork ...）**不在此列**，
# 它们在句中出现时应当按普通英文词正常翻译；只有在整句精确等于该词时，
# 才由 GLOSSARY 给出界面词义。
PROPER_WORDS = {
    # 开发/平台/语言
    "github", "gitlab", "bitbucket", "gitee", "git", "npm", "pnpm", "yarn", "pip", "pypi",
    "docker", "kubernetes", "nginx", "node.js", "nodejs", "deno", "bun", "webpack", "vite",
    "typescript", "javascript", "python", "java", "golang", "rust", "kotlin", "php", "ruby",
    "sql", "html", "css", "scss", "json", "yaml", "xml", "markdown", "regex", "graphql",
    # 协议/格式缩写
    "api", "sdk", "cli", "url", "uri", "http", "https", "tcp", "udp", "ssh", "tls", "ssl",
    "dns", "rest", "grpc", "jwt", "oauth", "cors", "csrf",
    # 硬件缩写
    "cpu", "gpu", "npu", "tpu", "ram", "rom", "ssd", "hdd", "usb", "bios", "uefi",
    "ai", "ml", "llm", "nlp", "asr", "tts", "onnx", "int8", "int4", "fp16", "fp32", "bf16",
    "sha256", "sha1", "md5", "base64", "utf-8", "arm", "x86", "x64",
    # 公司与产品
    "google", "google drive", "google docs", "google chrome", "chrome", "chromium",
    "chrome web store", "microsoft", "microsoft edge", "windows", "windows 11", "windows 10",
    "visual studio", "visual studio code", "vs code", "vscode", "openvino", "hugging face",
    "modelscope", "pytorch", "tensorflow", "transformers", "numpy", "pandas",
    "intel core ultra", "intel core", "intel", "xeon", "pentium", "celeron", "itanium",
    "optane", "vpro", "thunderbolt",
    "amd", "ryzen", "nvidia", "geforce", "radeon",
    "asus", "dell", "lenovo", "apple", "macos", "mac", "iphone", "ipad", "ios", "android",
    "linux", "ubuntu", "debian", "amazon", "aws", "azure", "cloudflare", "stack overflow",
    "jetbrains", "pycharm", "firefox", "safari", "opera", "tampermonkey",
    "ibm", "oracle", "sap", "adobe", "figma", "slack", "discord", "zoom", "notion",
    "youtube", "twitter", "facebook", "instagram", "linkedin", "reddit", "wikipedia", "medium",
    "telegram", "whatsapp", "wechat", "weixin", "alibaba", "tencent", "huawei", "xiaomi",
    "bilibili", "bing", "deepseek", "openai", "chatgpt", "claude", "gemini", "llama",
    "qwen", "mistral", "gemma", "nllb", "whisper", "bert", "copilot", "codespaces",
}


def _proper_spellings(key: str):
    """该专名在句中可能出现的写法（用于大小写敏感匹配）。

    首选词表 value 里已写好的规范写法（OpenVINO / GitHub / Google Drive），
    再补键本身的大写形式（缩写）或首字母大写形式。
    """
    value = PROTECTED.get(key)
    out = []
    if value and value != key:
        out.append(value)
    if " " in key:
        out.append(" ".join(w[:1].upper() + w[1:] for w in key.split()))
    elif len(key) <= 4:
        # 短词要同时给「全大写」（URL / API / CPU）和「首字母大写」（Xeon / Core / Atom）。
        # 曾经只给全大写，导致 "Xeon" 匹配不上、被当普通词翻译（实际踩到：
        # "Intel Xeon 6+ processors" → "子处理器"）。
        out.append(key.upper())
        out.append(key[:1].upper() + key[1:])
    else:
        out.append(key[:1].upper() + key[1:])
    return list(dict.fromkeys(out))


def _proper_pattern(key: str):
    """专名的匹配正则：允许中间出现空格/连字符差异（Google Drive / GoogleDrive）。"""
    words = [re.escape(w) for w in key.split()]
    return re.compile(r"(?<![\w.])" + r"[\s\-]?".join(words) + r"(?![\w])")


def _protected_sorted():
    """返回 [(写法, 匹配正则), ...]，长的优先。"""
    global _PROTECTED_SORTED
    if _PROTECTED_SORTED is None:
        items = []
        for k in PROPER_WORDS:
            for sp in _proper_spellings(k):
                items.append((sp, _proper_pattern(sp)))
        _PROTECTED_SORTED = sorted(items, key=lambda x: len(x[0]), reverse=True)
    return _PROTECTED_SORTED


def protect_terms(text: str):
    """把句中的专名 / 技术标识 / 数字换成 {{n}} 哨兵，返回 (替换后文本, [(哨兵, 原文), ...])。

    哨兵用 {{n}} 是实测结果：NLLB 会吃掉【n】这类符号哨兵（译文里留下裸数字），
    而 {{n}} / [n] / __n__ 能被原样保留。

    实现刻意保持"单轮"：
      - 第一轮替换专名（长的先处理，避免 core 先于 Intel Core Ultra 命中）
      - 第二轮替换技术标识与数字
    曾尝试"迭代到不动点 + 掩码"，但掩码在迭代中会被重复包裹，实测反复出错；
    单轮已覆盖实际页面里的所有形态（含 /api/v1/models 这类路径）。
    """
    mapping = []          # [(token, original), ...]
    out = text

    def put(original):
        token = "{{" + str(len(mapping)) + "}}"
        mapping.append((token, original))
        return token

    # 1) 专名（显式分类表 PROPER_WORDS，长的先处理）
    for _spelling, pat in _protected_sorted():
        while True:
            m = pat.search(out)
            if not m:
                break
            out = out[:m.start()] + put(m.group(0)) + out[m.end():]

    # 2) 技术标识（URL / 路径 / 版本号 / 文件名 / 邮箱）
    def repl(m):
        tok = m.group(0)
        return put(tok) if _looks_technical(tok) else tok

    out = _PROTECT_TOKEN_RE.sub(repl, out)

    # 3) 纯数字：被汉化会失真（"Intel Core Ultra 7"→"七个"、"3 sections"→"三节"）。
    #    正则用左右环视排除花括号，避免匹配到哨兵 {{0}} 内部的数字
    #    （曾因匹配到哨兵内部数字，把 {{0}} 层层包裹成 {{{{1}}}}）。
    out = _DIGIT_RE.sub(lambda m: put(m.group(0)), out)
    return out, mapping


def restore_terms(text: str, mapping) -> str:
    """把哨兵换回原文。

    模型偶尔会改写哨兵（实测 {{0}} 会变成 [0}} / {0} / [0]），所以不能只做精确替换，
    这里对每个序号接受几种常见变体。
    """
    for i, (token, original) in enumerate(mapping):
        variants = [
            token,
            "{" + str(i) + "}", "[" + str(i) + "]", "(" + str(i) + ")",
            "[" + str(i) + "}}", "{" + str(i) + "]", "[" + str(i) + "}",
            "【" + str(i) + "】",
        ]
        for v in variants:
            if v and v in text:
                text = text.replace(v, original)
    return text


_CHAR_RUN_RE = re.compile(r"(.)\1{7,}")          # 同一字符连续 8 次以上


def _looks_degenerate(out: str, src: str) -> bool:
    """译文是否为复读/乱码等退化输出。

    判定不看输入长度：短输入会退化，长输入同样会（实测
    "Intel® Xeon® 6+ processors" 译成 46 个"子"）。
    """
    o = (out or "").strip()
    if not o:
        return True

    compact = re.sub(r"\s+", "", o)
    if not compact:
        return True

    # 1) 整串是短片段复读："子子子…"、"。。。"
    if _DEGEN_RE.match(compact):
        return True
    # 2) 出现长复读片段（8 个相同字符连续）
    if _CHAR_RUN_RE.search(compact):
        return True
    # 3) 字符多样性过低：长文本却只用极少数不同字符
    if len(compact) >= 8 and len(set(compact)) <= max(2, len(compact) // 10):
        return True
    # 4) 长度严重失配：英译中通常会变短，但不该短到 1/8 以下、也不该膨胀 3 倍以上
    src_len = len(re.sub(r"\s+", "", src or ""))
    if src_len >= 10 and (len(compact) > src_len * 3 or len(compact) < src_len / 8):
        return True

    return False


def translate_sentence(pipe, s: str) -> str:
    """单句翻译：先做「不该翻」判断与词表，再走模型，最后对退化输出兜底。"""
    raw = s.strip()
    if len(raw) < 2:
        return s

    # 1) 技术标识（URL / 路径 / 版本号 / 快捷键 / 符号）：原样返回
    if _looks_technical(raw):
        return s

    # 2) 含较多中文：不再翻译（避免"这是 English 混排的句子"→"英语"这类吞掉）
    if _mostly_chinese(raw):
        return s

    key = _norm(s)

    # 3) 保护词表：品牌 / 专有名词 / 技术名词
    if key in PROTECTED:
        hit = PROTECTED[key]
        return hit if hit is not None else s.strip()

    # 4) 常见 UI 词表
    hit = GLOSSARY.get(key)
    if hit:
        return hit

    # 4.5) 中英混排的整句：不翻（拆句后模型会把英文/中文吞掉一半，实测
    #      "登录 Sign in"→"登录"、"微信 WeChat"→"WeChat"）
    if _HAN_RE.search(raw) and re.search(r"[A-Za-z]", raw):
        return s

    # 4.6) 句中专名保护：把专名/技术标识/数字换成哨兵，避免被译歪
    #      （哨兵必须由字母数字构成，见 protect_terms 的说明；
    #        还原时容忍模型对哨兵的轻微改写，见 restore_terms）
    protected, mapping = protect_terms(s)

    # 整句只剩哨兵时不要送模型：实测会把 "{{0}} {{1}}" 译成无关垃圾
    # （例如 "Intel Core Ultra 7" 整条 → "现在,我们要去做什么?"）。
    # 这种情况直接原样返回，本来就是该保留的专名/型号。
    # 哨兵占比过高时不要送模型：
    #   1) 整句只剩哨兵 —— 实测 "{{0}} {{1}}" 会被译成无关垃圾
    #      （"Intel Core Ultra 7" 整条 → "现在,我们要去做什么?"）
    #   2) 去掉哨兵后剩余英文词太少 —— 同样容易退化
    #      （"Intel Xeon 6+ processors"：3 个哨兵 + 1 个词 → "子处理器"）
    # 两种情况都直接原样返回，本来就是该保留的专名/型号。
    if mapping:
        remainder = re.sub(r"\{\{\d+\}\}", " ", protected)
        if not remainder.strip(" \t.,;:!?、，。；：！？"):
            return s
        if len(re.findall(r"[A-Za-z]{2,}", remainder)) < 2:
            return s

    out = pipe.translate(protected)
    if mapping:
        out = restore_terms(out, mapping)
    if not out or not out.strip():
        return s

    # 5) 退化输出兜底：只要译文像复读/乱码就保留英文，宁可少翻也不要给出垃圾。
    #    曾经只在 len(s.split()) <= 2 时判定，导致 "Intel® Xeon® 6+ processors"
    #    → 46 个"子" 这种明显垃圾被原样返回（实际发生）。
    if _looks_degenerate(out, s):
        return s
    return out


def translate_one_llm(text: str) -> str:
    """大模型（Index-Translate）的单条翻译。

    与逐词保护那套逻辑不同：2B 模型自己能处理专名和术语，
    再把 openvino 小模型用的哨兵替换套上去反而会干扰它。
    这里只保留「不该翻」的判断。
    """
    raw = text.strip()
    if len(raw) < 2:
        return text
    if _looks_technical(raw) or _mostly_chinese(raw):
        return text
    if _HAN_RE.search(raw) and re.search(r"[A-Za-z]", raw):
        return text
    out = llm_translate(raw[:MAX_CHARS_PER_ITEM])
    if not out or _looks_degenerate(out, raw):
        return text
    return out


def translate_one(pipe, text: str) -> str:
    """把一段文本按句切分、逐句翻译，再拼回一段（中文之间不加空格）。"""
    text = text[:MAX_CHARS_PER_ITEM]

    # 中英混排的整段不翻：必须在拆句之前判断，否则拆句会把混排切碎
    # （实测 "登录 Sign in" 被拆后只翻出一半）
    if _HAN_RE.search(text) and re.search(r"[A-Za-z]", text):
        return text

    sentences = split_sentences(text)[:MAX_SENTENCES_PER_ITEM]
    if not sentences:
        return ""
    return "".join(translate_sentence(pipe, s) for s in sentences)


class Server(ThreadingHTTPServer):
    """禁用端口复用。

    Python 默认 allow_reuse_address=1，在 Windows 上会让**第二个实例也能绑定同一端口**，
    于是两个服务静默共存、请求被随机分发（实测踩到，极难排查）。
    关掉后第二个实例会直接报 "address already in use"，行为可预期。
    """
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- helpers -------------------------------------------------------
    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 关闭逐请求日志，保持控制台干净
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def _not_ready(self) -> bool:
        """模型还在编译时，翻译请求直接给「加载中」，而不是让浏览器干等 30 秒。"""
        if STATE["ready"]:
            return False
        self._send(503, {
            "ok": False, "ready": False, "loading": STATE["loading"],
            "model": ACTIVE["model"],
            "error": "本地服务正在加载模型，请稍候",
        })
        return True

    # ---- routes --------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path == "/health":
            active_pipe = PIPELINES.get((ACTIVE["model"], DEVICE))
            eng = reg.engine_of(ACTIVE["model"])
            self._send(200, {
                "ok": True,
                "ready": STATE["ready"],
                "loading": STATE["loading"],
                "load_error": STATE["error"],
                "device": DEVICE,
                "engine": eng,
                "model": ACTIVE["model"],
                "model_label": reg.meta(ACTIVE["model"])["label"],
                "execution_devices": ("llama.cpp" if eng == "llamacpp"
                                      else getattr(active_pipe, "dev", None)),
                "openvino": ov.__version__,
                "max_batch": MAX_ITEMS,
                "loaded": [{"model": k[0], "device": k[1], "kind": p.kind}
                           for k, p in PIPELINES.items()],
            })
            return

        if path == "/models":
            self._send(200, {
                "ok": True,
                "active": ACTIVE["model"],
                "device": DEVICE,
                "models": reg.describe_all(ACTIVE["model"]),
                "loaded": [{"model": k[0], "device": k[1], "kind": p.kind}
                           for k, p in PIPELINES.items()],
            })
            return

        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        """统一兜底：任何未捕获异常都返回 500 并打印 traceback（避免连接被直接掐断）。"""
        try:
            self._handle_post()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(tb, file=sys.stderr, flush=True)
            try:
                self._send(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}",
                                 "traceback": tb[-1200:]})
            except Exception:
                pass

    def _handle_post(self):
        path = self.path.split("?")[0].rstrip("/")

        # ---------- 优雅停止（供插件/脚本使用） ----------
        if path == "/shutdown":
            self._send(200, {"ok": True, "state": "shutting-down"})
            STOP.set()
            BUILD_Q.put(None)            # 唤醒主线程循环退出
            if HTTPD is not None:
                threading.Thread(target=HTTPD.shutdown, daemon=True).start()
            return

        # ---------- 模型：切换 / 安装 / 安装进度 ----------
        if path in ("/models/select", "/models/install", "/models/install_status"):
            try:
                body = self._read_json()
                mid = str(body.get("id", "")).strip()
            except Exception as e:
                return self._send(400, {"ok": False, "error": f"bad request: {e}"})
            if not reg.exists(mid):
                return self._send(400, {"ok": False, "error": f"未知模型: {mid}"})

            if path == "/models/select":
                if not reg.installed(mid):
                    return self._send(400, {
                        "ok": False, "error": f"模型未安装: {mid}",
                        "hint": f"可点插件里的「安装」按钮，或双击 install_model.bat {mid}",
                    })
                prev = ACTIVE["model"]
                ACTIVE["model"] = mid
                reg.set_active(mid)
                if mid != prev:
                    # 换模型必须换进程（NPU 驱动限制）：退出并由启动器重新拉起
                    BUILD_Q.put((stop_for_restart, {}, threading.Event()))
                    return self._send(200, {
                        "ok": True, "active": mid, "label": reg.meta(mid)["label"],
                        "restart": True,
                        "note": "本地服务正在用新模型重启（NPU 需重新编译，约 5–30 秒）",
                    })
                return self._send(200, {
                    "ok": True, "active": mid, "label": reg.meta(mid)["label"],
                    "devices": reg.devices_for(mid),
                })

            if path == "/models/install":
                return self._send(200, start_install(mid))

            return self._send(200, install_status(mid))

        # ---------- 批量：整页翻译用 ----------
        if path == "/translate_batch":
            if self._not_ready():
                return
            try:
                body = self._read_json()
                texts = body.get("texts") or []
                if not isinstance(texts, list):
                    raise ValueError("'texts' must be a list")
                texts = ["" if t is None else str(t) for t in texts][:MAX_ITEMS]
            except Exception as e:
                return self._send(400, {"ok": False, "error": f"bad request: {e}"})
            if not texts:
                return self._send(400, {"ok": False, "error": "empty texts"})

            t0 = time.time()
            try:
                results, dev = run_on_main(lambda: job_batch(texts))
            except Exception as e:
                return self._send(503, {"ok": False,
                                        "error": f"翻译失败: {type(e).__name__}: {e}"})
            total = round((time.time() - t0) * 1000)
            return self._send(200, {
                "ok": True,
                "device": DEVICE,
                "model": ACTIVE["model"],
                "execution_devices": dev,
                "count": len(texts),
                "translations": results,
                "total_ms": total,
            })

        # ---------- 单段 ----------
        if path == "/translate":
            if self._not_ready():
                return
            try:
                body = self._read_json()
                text = str(body.get("text", "")).strip()
            except Exception as e:
                return self._send(400, {"ok": False, "error": f"bad request: {e}"})
            if not text:
                return self._send(400, {"ok": False, "error": "empty text"})

            t0 = time.time()
            try:
                sentences, translations, latencies, dev = run_on_main(lambda: job_single(text))
            except Exception as e:
                return self._send(503, {"ok": False,
                                        "error": f"翻译失败: {type(e).__name__}: {e}"})
            total = round((time.time() - t0) * 1000)

            return self._send(200, {
                "ok": True,
                "device": DEVICE,
                "model": ACTIVE["model"],
                "execution_devices": dev,
                "sentences": sentences,
                "translations": translations,
                "latency_ms": latencies,
                "total_ms": total,
                "translation": "".join(translations),
            })

        self._send(404, {"ok": False, "error": "not found"})


# ---------------------------------------------------------------------------
# 模型安装：后台调用 install_model.py 下载并导出，供插件一键安装
# ---------------------------------------------------------------------------
INSTALL_PROCS = {}


def _install_log(model_id: str) -> str:
    return os.path.join(ROOT, f"install_{model_id}.log")


def start_install(model_id: str) -> dict:
    if reg.engine_of(model_id) == "llamacpp":
        if reg.installed(model_id):
            return {"ok": True, "state": "already-installed", "model": model_id}
        proc = INSTALL_PROCS.get(model_id)
        if proc is not None and proc.poll() is None:
            return {"ok": True, "state": "already-running", "model": model_id,
                    "log": os.path.basename(_install_log(model_id))}
        log_path = _install_log(model_id)
        try:
            logf = open(log_path, "ab", buffering=0)
            flags = (0x00000008 | 0x08000000) if os.name == "nt" else 0
            proc = subprocess.Popen(
                [sys.executable, os.path.join(ROOT, "install_model.py"), "--id", model_id],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                creationflags=flags, close_fds=True,
            )
            INSTALL_PROCS[model_id] = proc
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "state": "started", "model": model_id,
                "size_mb": reg.meta(model_id)["size_mb"],
                "log": os.path.basename(log_path)}
    if reg.has_variant(model_id, "cached") and reg.has_variant(model_id, "static"):
        return {"ok": True, "state": "already-installed", "model": model_id}
    proc = INSTALL_PROCS.get(model_id)
    if proc is not None and proc.poll() is None:
        return {"ok": True, "state": "already-running", "model": model_id,
                "log": os.path.basename(_install_log(model_id))}

    log_path = _install_log(model_id)
    try:
        logf = open(log_path, "ab", buffering=0)
        flags = (0x00000008 | 0x08000000) if os.name == "nt" else 0   # DETACHED | NO_WINDOW
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "install_model.py"), "--id", model_id],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
            creationflags=flags, close_fds=True,
        )
        INSTALL_PROCS[model_id] = proc
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "state": "started", "model": model_id,
            "size_mb": reg.meta(model_id)["size_mb"],
            "log": os.path.basename(log_path)}


def install_status(model_id: str) -> dict:
    proc = INSTALL_PROCS.get(model_id)
    running = bool(proc is not None and proc.poll() is None)
    tail = ""
    try:
        with open(_install_log(model_id), "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        tail = "\n".join(lines[-8:])
    except Exception:
        pass
    engine = reg.engine_of(model_id)
    return {"ok": True, "model": model_id, "running": running,
            "installed": reg.installed(model_id),
            "engine": engine,
            "has_cached": reg.has_variant(model_id, "cached") if engine != "llamacpp" else False,
            "has_static": reg.has_variant(model_id, "static") if engine != "llamacpp" else False,
            "devices": reg.devices_for(model_id),
            "log_tail": tail}


def main() -> int:
    global DEVICE, PORT, HTTPD
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="NPU", help="NPU / CPU / GPU / AUTO")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", help="启动使用的模型 ID（默认取 settings.json 里上次选中的）")
    args = ap.parse_args()
    DEVICE, PORT = args.device, args.port

    mid = args.model or reg.get_active()
    if not reg.exists(mid) or not reg.installed(mid):
        avail = [m for m in reg.MODELS if reg.installed(m)]
        if not avail:
            print("[ERR] 还没有安装任何模型。请先运行 setup.bat，或直接安装某个模型："
                  "install_model.bat index-translate-2b", flush=True)
            return 1
        print(f"[WARN] 模型 {mid} 未安装，改用 {avail[0]}", flush=True)
        mid = avail[0]
    ACTIVE["model"] = mid
    try:
        if os.path.exists(RESTART_FLAG):
            os.remove(RESTART_FLAG)      # 已按（新）模型启动，清掉重启标记
    except Exception:
        pass
    if args.model:
        reg.set_active(mid)              # 命令行指定模型时同步写入设置

    print(f"OpenVINO {ov.__version__} | devices={ov.Core().available_devices}", flush=True)
    print(f"model: {mid} ({reg.meta(mid)['label']})", flush=True)

    # ---- 1) 先绑端口 ----
    # 以往是「编译完再绑端口」，插件在编译期间连不上，用户看到的是几十秒空白。
    # 现在先让 /health 可用（返回 ready=false），插件可以立刻显示「正在加载模型」。
    httpd = None
    for attempt in range(24):
        try:
            httpd = Server(("127.0.0.1", PORT), Handler)
            break
        except OSError as e:
            if attempt == 0:
                print(f"[..] 端口 {PORT} 暂时被占用（{e}），等待释放…", flush=True)
            time.sleep(0.5)
    if httpd is None:
        print(f"[ERR] 无法绑定 127.0.0.1:{PORT}。"
              f"可能已有另一个服务实例在运行（本程序已禁用端口复用）。", flush=True)
        return 1

    HTTPD = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"listening on http://127.0.0.1:{PORT}  (模型加载中…)", flush=True)

    # ---- 2) 再加载模型（NPU 要求在主线程）----
    STATE["loading"] = True
    t0 = time.time()
    engine = reg.engine_of(mid)

    if engine == "llamacpp":
        # llama.cpp 引擎：模型跑在独立的 llama-server 进程里
        try:
            llama_ensure()
        except Exception as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
            STATE["loading"] = False
            print(f"[ERR] llama-server 启动失败：{e}", flush=True)
            return 1
        atexit.register(llama_stop)
        STATE["loading"] = False
        STATE["ready"] = True
        print(f"[ready] {mid} 就绪，总耗时 {time.time()-STATE['started_at']:.2f}s", flush=True)
        return _serve_loop(httpd)

    try:
        pipe = _build_now(mid, DEVICE)          # 启动构建：主线程
    except Exception as e:
        print(f"[WARN] 在 {DEVICE} 上加载失败：{type(e).__name__}: {e}", flush=True)
        if str(DEVICE).upper() == "NPU":
            print("       回退到 CPU 启动（插件里可再切回设备）", flush=True)
            DEVICE = "CPU"
            try:
                pipe = _build_now(mid, DEVICE)
            except Exception as e2:
                STATE["error"] = f"{type(e2).__name__}: {e2}"
                STATE["loading"] = False
                print(f"[ERR] CPU 也失败：{e2}", flush=True)
                return 1
        else:
            STATE["error"] = f"{type(e).__name__}: {e}"
            STATE["loading"] = False
            return 1
    load_s = time.time() - t0
    print(f"ready in {load_s:.2f}s | kind={pipe.kind} | "
          f"EXECUTION_DEVICES={pipe.dev}", flush=True)

    pipe.translate("warm up")        # 预热，避免第一句偏慢
    STATE["loading"] = False
    STATE["ready"] = True
    print(f"[ready] 总耗时 {time.time()-STATE['started_at']:.2f}s", flush=True)
    return _serve_loop(httpd)


def _serve_loop(httpd) -> int:

    # ---- 主线程循环：只做「编译模型」这一件事 ----
    # NPU 插件要求在主线程编译；在工作线程编译第二个模型会得到
    # ZE_RESULT_ERROR_INVALID_NATIVE_BINARY（实测），所以必须这样分工。
    try:
        while not STOP.is_set():
            item = BUILD_Q.get()
            if item is None:
                break
            fn, box, ev = item
            try:
                box["result"] = fn()
            except Exception as e:
                box["error"] = e
            finally:
                ev.set()
    except KeyboardInterrupt:
        pass
    print("bye", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
