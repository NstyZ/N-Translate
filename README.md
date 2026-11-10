# N-Translate

**基于哔哩哔哩开源 [Index-Translate](https://github.com/bilibili/Index-Translate) 的本地翻译插件。**

Chrome 扩展 + 本地推理服务，英译中，全程离线。

划词翻译、整页翻译、一键还原。**单句 140 ms，整页 20~30 秒**，不联网、不上传任何一个字。
默认用质量最好的 Index-Translate 2B（跑在显卡上）；想要更省内存，一键切到 NPU 上的轻量模型（773 MB / 128 ms）。

<sub>Windows 11 · Intel Core Ultra（NPU3720 及以上）· 可选 NVIDIA 显卡加速</sub>

---

## 目录

- [为什么做这个](#为什么做这个)
- [功能](#功能)
- [实测数据](#实测数据)
- [架构](#架构)
- [快速开始](#快速开始)
- [模型说明](#模型说明)
- [与浏览器同生命周期](#与浏览器同生命周期)
- [运行环境自检与自动修复](#运行环境自检与自动修复)
- [热更新](#热更新)
- [实现要点](#实现要点)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [致谢与许可](#致谢与许可)

---

## 为什么做这个

在线翻译插件要把你正在看的网页内容发到别人的服务器。本地翻译器不存在这个问题，但端侧有三道坎：

1. **小模型质量不够**：OPUS-MT 这类 100 MB 的专用模型，长句还行，短词和专名经常翻错（`OpenVINO` → "开酒"）。
2. **大模型跑不动**：能翻译好的都是 LLM 级别，但 2B 模型在纯 CPU 上只有几十 tok/s，整页翻译要几分钟。
3. **NPU 看起来很美，实际上很难用**：这块 NPU 只吃全静态形状，动态形状直接崩驱动（实测 6 个 OpenVINO 版本）。

N-Translate 的做法是**分层**，两个模型都常驻可选：

- 默认用 **Index-Translate 2B**（放显卡）：质量最好，单句 140 ms，划词/整页都够用；
- 想省内存或没有独显时，一键切到 **OPUS-MT @ NPU**：773 MB、单句 128 ms，整页更快。

两条路都跑通了，而且都能在一台普通笔记本上离线跑。

## 功能

| | |
|---|---|
| **划词翻译** | 选中英文 → `Alt+T` → 选区下方弹出译文气泡（Shadow DOM，不污染页面样式） |
| **整页翻译** | 右键菜单 / `Alt+Shift+T`，逐块写回，边翻边看；可一键还原原文 |
| **增量翻译** | 页面动态加载的新内容（瀑布流、SPA 路由切换）自动补翻 |
| **模型切换** | Index-Translate 2B（默认，显卡）/ OPUS-MT（NPU 轻量），插件里一键切换 |
| **自动唤起服务** | 注册 Native Messaging 宿主后，插件发现服务没开会自己拉起来，不用手动双击 bat |
| **开机自启** | 可选，注册后完全零操作 |
| **离线** | 只监听 `127.0.0.1`，不产生任何外部请求 |

服务端还内置了几层兜底，避免小模型的典型翻车：

- **约 100 条 UI 词表**（`Home` / `Sign in` / `Load more` …）与**专有名词表**（`GitHub` / `OpenVINO` / `Node.js` …）精确命中，不送模型；
- 句中专名替换成 `{{n}}` 哨兵，翻完再换回，避免 `Intel Core Ultra 7` 被翻成"七个"；
- **退化输出兜底**：译文如果是复读（46 个"子"）、长度严重失配或字符多样性过低，直接保留英文，宁可少翻也不给垃圾；
- URL / 文件路径 / 版本号 / 快捷键一律原样返回。

## 实测数据

测试机：**Intel Core Ultra 7 255HX（NPU3720）+ RTX 5060 Laptop 8GB**，Windows 11 24H2。

### 启动与常驻内存

| 指标 | 优化前（NLLB-600M @ NPU，已移除） | 现在（OPUS-MT @ NPU） |
|---|---|---|
| 进程启动 → 能翻译 | 约 46 秒 | **1.5 秒** |
| 常驻内存 | 4455 MB | **773 MB** |
| 单句延迟 | 1832 ms | **128 ms** |
| 整页 100 段 | 约 3 分钟 | 约 13 秒 |

### 三个模型的横向对比

| 模型 / 设备 | 单句 | 整页（150~250 条去重文本） | 内存 | 质量 |
|---|---|---|---|---|
| **OPUS-MT @ NPU** | **128 ms** | 13~25 s | 773 MB | 一般 |
| **Index-Translate 2B @ RTX 5060** | **140 ms** | 20~30 s | 1.8 GB | **好** |
| Index-Translate 2B @ CPU（16 线程） | 0.4~0.9 s | 2~4 min | 2.1 GB | 好 |
| OPUS-MT @ CPU | 192 ms | 20~40 s | 366 MB | 一般 |

生成速度：Index-Translate 2B 在 **RTX 5060 上 156 tok/s**，CPU 上 26 tok/s。

## 架构

```
┌────────────────────┐   HTTP (127.0.0.1:8765)   ┌──────────────────────────────┐
│   Chrome 扩展      │ ────────────────────────> │  server.py                   │
│   content.js       │ <──────────────────────── │  ├─ OpenVINO 引擎             │
│   background.js    │      JSON 译文            │  │   Index-Translate / OPUS   │
│   popup.js         │                           │  │   → NPU / CPU / iGPU       │
└─────────┬──────────┘                           │  └─ llama.cpp 引擎            │
          │ Native Messaging                    │      Index-Translate 2B      │
          ▼                                      │      → CPU / dGPU            │
┌────────────────────┐                           │      (独立 llama-server 进程) │
│  npu_host.py       │ ── 无服务时自动拉起 ────> └──────────────────────────────┘
│  (本地消息宿主)     │
└────────────────────┘
```

**为什么是两个引擎而不是一个**：OpenVINO 能吃到 NPU，但 Index-Translate 是 Qwen3.5 底座、24 层里 18 层是线性注意力，`transformers` 没有这个架构、`optimum-intel` 导不出、连 OpenVINO GenAI 的 GGUF 读取器里都查无 `qwen35`。而 llama.cpp 原生支持它。拆成独立进程后二者互不干扰，不用时把 llama-server 关掉，2 GB 内存立刻归还。

**为什么换模型要重启进程**：Intel NPU 驱动在同一进程内无法换用第二个已编译模型（`ZE_RESULT_ERROR_INVALID_NATIVE_BINARY`）。服务切模型时写 `restart.flag` 后退出，由启动器或插件再用新模型拉起，全程 2~11 秒、无需人工干预。

## 快速开始

### 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 11 24H2+ |
| 硬件 | Intel Core Ultra（含 NPU）可用 NPU 加速；**没有 NPU 也能用**，设备选 CPU 即可 |
| Python | 3.10+（3.13 已验证），安装时勾选 *Add python.exe to PATH* |
| 浏览器 | Chrome / Edge（Chromium 内核，Manifest V3） |
| 显卡（可选） | NVIDIA，用于 Index-Translate 加速 |
| NPU 驱动 | 建议装最新：<https://www.intel.com/content/www/us/en/download/794734/intel-npu-driver-windows.html> |

### 1. 装环境

```bat
git clone https://github.com/NstyZ/N-Translate.git
cd N-Translate
setup.bat
```

`setup.bat` 会自动：建 `.venv` → 装依赖（3~8 分钟）→ 导出基础模型（OPUS-MT）→
下载主模型（Index-Translate 2B，1.2GB，可跳过）→ 下载 llama.cpp 运行时 →
NPU 自检 → 询问是否注册"插件自动唤起"与"开机自启"。

> 有 NVIDIA 显卡的话，装完再跑一次 `install_llama.bat cuda`，Index-Translate 会快约 6 倍。

### 2. 装插件

1. 打开 `chrome://extensions`，开右上角 **开发者模式**
2. **加载已解压的扩展程序** → 选择本仓库的 `chrome-extension` 文件夹
3. 建议固定到工具栏

### 3. 用

| 操作 | 效果 |
|---|---|
| 选中英文 → `Alt+T` | 划词翻译 |
| `Alt+Shift+T` / 右键 | 整页翻译 |
| 点插件图标 | 粘贴翻译、**切换模型**、启动/停止服务、切换设备 |

## 模型说明

| 模型 ID | 来源 | 体积 | 许可 | 说明 |
|---|---|---|---|---|
| `index-translate-2b`（**默认**） | [IndexTeam/Index-Translate-2B](https://huggingface.co/IndexTeam/Index-Translate-2B) | 1.2 GB | Apache-2.0 | 哔哩哔哩开源的 150 语种翻译模型，质量最好 |
| `opus-mt-en-zh` | [Helsinki-NLP/opus-mt-en-zh](https://huggingface.co/Helsinki-NLP/opus-mt-en-zh) | 111 MB | CC-BY-4.0 | 轻量模式：没有独显或想省内存时用，跑在 NPU 上 |

模型不随仓库分发。安装方式任选：

```bat
install_model.bat index-translate-2b
install_model.bat opus-mt-en-zh
```

或直接在插件弹窗里点「安装」（后台下载，可关掉弹窗）。

## 与浏览器同生命周期

**浏览器一关，服务就退出，显存和内存立刻还回去。**

插件通过 Native Messaging 拉起服务时，宿主会顺着进程树找到**浏览器主进程 PID**，交给服务当"主人"
（`/health` 里可以看到 `browser_linked: true` 和 `owner_pid`）。

| 机制 | 行为 |
|---|---|
| **看门狗** | 服务每 2 秒检查一次浏览器进程，消失就关掉 `llama-server` 并退出（实测 **2 秒内**） |
| **Job Object** | `llama-server` 被放进「父进程一死就杀」的 Job，**即使服务被任务管理器硬杀**，它也会跟着消失，不会留下占 1.7GB 内存 / 显存的孤儿进程 |
| **自动认领** | 如果服务先前是手动启动的，插件第一次调用 `ensure` 时会用 `POST /owner` 把它接管过来 |
| **手动模式** | `start_server.bat` 起的服务没有主人，行为保持原样（不会自杀），适合调试 |

> 顺带把**开机自启**降级为「不推荐」：既然服务会跟随浏览器，自启反而会让它常驻。
> 已经装过的可以用 `uninstall_autostart.bat` 取消。

`doctor.bat` 里也多了一项「服务跟随浏览器」，随时能确认当前服务是否已经绑上。

## 运行环境自检与自动修复

第一次用、或者换了台机器，**不需要先读懂报错**——`doctor.py` 会把全部前提条件过一遍，缺什么补什么：

```bat
doctor.bat              :: 只体检，出报告（不动任何文件）
doctor.bat fix          :: 缺什么装什么（venv / 依赖 / llama.cpp / 模型）
doctor.bat fix model    :: 连主模型（1.2GB）一起下
```

体检的 12 项：

| 类别 | 检查内容 | 缺了会自动补 |
|---|---|---|
| 系统 | Windows 版本 / 64 位 / 磁盘空间 | — |
| Python | 找得到 3.10+ 64 位（会自动跳过 Microsoft Store 的 python 残桩） | — |
| 虚拟环境 | `.venv` 存在且能跑 | ✅ 重建 |
| 依赖 | numpy / openvino / openvino-tokenizers / sentencepiece / jinja2 | ✅ `pip install -r requirements.txt`（默认源失败自动切清华镜像） |
| 推理运行时 | `llama\llama-server.exe` | ✅ 自动下载 CPU 版 |
| 模型 | OPUS-MT（111MB）、Index-Translate 2B（1.2GB） | ✅（大模型需 `--with-model`） |
| NPU | `Core().available_devices` 是否含 NPU | 只警告，自动回退 CPU |
| 端口 | 8765 是否被占用 | — |
| 启动器 | Native Messaging 注册项 | 只提示（要写注册表） |

### 三处自动触发点

1. **`start_server.bat` 启动前**：先用 0.1 秒确认「venv 能跑 + 至少有一个模型」。
   不满足就自动跑一遍 `doctor.py --fix` 补齐再启动，而不是甩一句 "Python environment not found"。
2. **插件弹窗**：服务启动失败时（例如新电脑还没装环境），会直接出现
   **「检查运行环境」/「自动修复」** 两个按钮，体检结果逐条列在弹窗里。
   这一路走的是 Native Messaging → `doctor.py`，所以**连 `.venv` 都还没建的裸机也能用**
   （`npu_host.bat` 会退回系统 Python 应答）。
3. **`setup.bat` 收尾**：装完自动跑一次体检，有缺项当场修。

```bat
:: 机器可读（插件就是这么调的）
python doctor.py --json
```

## 热更新

插件自带更新通道，不用重新下载分发包。入口在弹窗底部 **「设置 · 热更新」**：

| 控件 | 作用 |
|---|---|
| **更新地址** | 留空即用本仓库（`NstyZ/N-Translate`）；fork 后可在弹窗里改成自己的地址 |
| **检查更新** | 手动比对远端 `update.json` 与本机版本 |
| **立即更新** | 发现新版本时出现，一键拉取并覆盖源码 |
| **每小时自动检查** | 默认开启。有新版时工具栏图标上会打一个 `↑` 角标 |

**更新的实现**（`updater.py` + Native Messaging 宿主）：

```
插件读 update.json → 版本更高?
      ↓ 是
交给本地宿主(Python)下载仓库源码包 zip（raw.githubusercontent / jsDelivr / gh-proxy 三路兜底）
      ↓
停本地服务 → 覆盖 *.py *.bat *.ps1 docs/ chrome-extension/
      ↓
重启服务 → 弹窗提示"去 chrome://extensions 点一次重新加载"
```

为什么最后还要手点一下：**扩展没有权限改写自己的文件**，只有本地宿主能做；而 Chromium 对"已解压加载"的扩展不会自动重载代码。所以是「一键更新 + 一次重新加载」。

更新时**不会动**你的 `models/`、`llama/`、`.venv/`、`ov_cache/`、`settings.json`、`chrome-extension/extension_id.txt`。

### 更新源指向哪里

`chrome-extension/background.js` 顶部（默认已经指向本仓库）：

```js
const REPO_SLUG = "NstyZ/N-Translate";
```

**如果你 fork 了这个项目**，把这一行改成 `<你的用户名>/N-Translate`，重新加载插件即可自建更新通道。
也可以不改代码，直接在弹窗「更新地址」里填自己的 `https://raw.githubusercontent.com/<你>/<仓库>/main/update.json`。

### 发布一个新版本

1. 改 `chrome-extension/manifest.json` 的 `version`（例如 `1.0.1`）
2. 改 `update.json` 的 `version` 与 `notes`
3. `git commit && git push` —— 已安装的用户最多一小时后收到角标

## 实现要点

想自己改的人可以看这几条，都是踩过的坑：

### 1. NPU 只吃全静态形状

动态形状会让 NPU 驱动直接崩（`0xC0000409`），换了 6 个 OpenVINO 版本都一样。
解法是用 `--disable-stateful` 导出无状态 decoder：`(input_ids, encoder_attention_mask, encoder_hidden_states) -> logits`，
再把所有输入 reshape 成固定形状，借因果注意力在尾部填充解码。见 `pipelines.py`。

### 2. 删掉 `transformers` 省下 4~20 秒

服务只需要"文本 ↔ token id"，却曾经为此 `import transformers`——冷启动实测 **20.0 秒**、+322 MB 内存。

现在两条零依赖路径（编码结果与 HF 逐 token 一致）：

- OpenVINO 预导出的 tokenizer 图（`openvino_tokenizer.xml`，0.05 s）
- SentencePiece + `vocab.json` 复刻 MarianTokenizer（0.05 s）

`prepare_tokenizer.py` 可以把任意 HF tokenizer 转成 OpenVINO 图。

### 3. 先绑端口，再编译模型

模型编译要几秒，以前是"编译完才 `listen`"，插件在空白期里只会报连接失败。
现在端口先绑上，`/health` 立刻返回 `ready: false`，插件端轮询等就绪，用户看到的是"正在加载模型"。

### 4. 编译结果缓存在 `ov_cache\`

`ov.Core().set_property({"CACHE_DIR": "ov_cache"})` 让二次启动直接导入编译产物（OPUS 5~7 s → 0.35 s）。
这个目录与机型/驱动绑定，换机器没意义，已在 `.gitignore` 里。

### 5. OpenVINO 里那个 NVIDIA 设备不可用

`Core().available_devices` 会出现 `GPU.1 = NVIDIA GeForce RTX 5060`，看着像能用，其实是
Intel GPU 插件借 NVIDIA 的 OpenCL 驱动跑出来的，实测出现 4.5 秒离群值、译文错乱（`The quick brown fox...` → "跳"）。
**真正的显卡加速必须走 llama.cpp 的 CUDA 构建。**

### 6. 换模型必须换进程

同一进程内不能存在两个已编译的 NPU 模型，见上文「架构」。

更多细节（包括每次崩溃的完整报错、逐版本对比）见 [docs/实测报告.md](docs/实测报告.md)。

## 目录结构

```
N-Translate/
├─ setup.bat / setup.ps1          一次性安装
├─ README.md / LICENSE
├─ requirements.txt
├─ server.py                      本地 HTTP 服务（127.0.0.1:8765）
├─ pipelines.py                   OpenVINO 翻译管线（全静态形状）
├─ model_registry.py              模型注册表
├─ prepare_tokenizer.py           HF tokenizer → OpenVINO 图
├─ winproc.py                    看门狗：浏览器 PID 探测（纯 ctypes）
├─ doctor.py / doctor.bat        运行环境体检与自动修复
├─ updater.py                     热更新：拉取源码包并就地覆盖
├─ update.json                    更新清单（版本号 + 更新说明）
├─ install_model.py / .bat        下载并导出指定模型
├─ install_llama.py / .bat        下载 llama.cpp 运行时（cpu / cuda）
├─ server_daemon.py               起停服务（无窗口）
├─ npu_host.py / .bat             Native Messaging 宿主
├─ install_native_host.py / .bat  注册宿主（写 HKCU，无需管理员）
├─ fix_extension.py               按安装路径重算扩展 ID 并注册
├─ install_autostart.ps1 / .bat   开机自启
├─ start_server.bat / stop_server.bat
├─ translate.py                   命令行翻译 / 基准测试
├─ npu_sanity.py                  NPU 自检
├─ chrome-extension/              Chrome 扩展（加载这个文件夹）
├─ docs/                          实测报告、技术方案
├─ models/                        模型目录（不入库，见 .gitignore）
└─ llama/                         llama.cpp 运行时（不入库，用 install_llama.py 下载）
```

## 常见问题

**Q：插件弹窗显示「未连接」？** 点「启动本地服务」。若提示未注册启动器，双击 `install_native_host.bat`（或 `fix_extension.py`）。

**Q：每次重启 Chrome 插件就消失了？** `manifest.json` 里不能有 `key` 字段——已解压加载的扩展带 `key` 时会被 Chrome 卸载。本仓库的 manifest 不含该字段；如果你改过，删掉它再重新加载。

**Q：NPU 自检失败？** 多半是驱动缺失或太旧，装最新驱动后重跑 `npu_sanity.py`；也可以先把设备切到 `CPU`。

**Q：Index-Translate 启动失败？** 确认 `llama\llama-server.exe` 存在（跑 `install_llama.bat`），模型在 `models\index-translate-2b-gguf\`（跑 `install_model.bat index-translate-2b`）。日志见 `llama_server.log`。

**Q：想改端口？** 插件弹窗里改「服务地址」，同时改 `start_server.bat` 里的 `--port`。

**Q：路径里有中文会不会出问题？** 脚本内部都用 `%~dp0` / `__file__` 定位，但 Python 虚拟环境和部分 .bat 对非 ASCII 路径敏感，建议放在纯英文路径下。

## 已知限制

- **只支持英→中**。Index-Translate 本身支持 150 语种，改 `model_registry.py` 里的 `target_lang` 即可扩展。
- **仅 Windows**。NPU 依赖 Intel 的 Windows 驱动，Linux 端未验证。
- **NPU3720 跑不了 LLM**：13 TOPS、编译器对 KV-cache 算子支持很弱，2B 模型每 token 要过一遍 1.2 GB 权重，带宽喂不饱。要 NPU 上跑 LLM 得等 NPU40XX 级别机型。
- 跨域 iframe 内的文字不翻译；代码块、输入框、`contenteditable` 区域会跳过。
- 单条超过 4000 字符会截断；整页单次上限 1200 处文本。
- 小模型质量有限，短词和专名靠词表兜底，仍可能有误。

## 路线图

- [ ] 中→英方向（换模型即可，`target_lang` 已参数化）
- [ ] 批处理合并解码，进一步压低整页耗时
- [ ] Firefox 适配（`browser.*` 兼容层）
- [ ] 鼠标悬停即时预览

## 致谢与许可

感谢这些项目撑起了全部推理：

- [OpenVINO](https://github.com/openvinotoolkit/openvino) — NPU / CPU / iGPU 推理
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — GGUF 推理（MIT）
- [optimum-intel](https://github.com/huggingface/optimum-intel)、[NNCF](https://github.com/openvinotoolkit/nncf) — 模型导出与量化
- [Index-Translate](https://github.com/bilibili/Index-Translate)（哔哩哔哩）— 高质量翻译模型（Apache-2.0）
- [OPUS-MT](https://huggingface.co/Helsinki-NLP/opus-mt-en-zh)（CC-BY-4.0）

本项目代码以 [MIT](LICENSE) 许可发布。**注意：模型有各自的许可** —— Index-Translate 为 Apache-2.0，OPUS-MT 为 CC-BY-4.0。
第三方组件、模型与再分发提醒的完整清单见 [NOTICE.md](NOTICE.md)。
