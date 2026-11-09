# Chrome 插件：N-Translate（英→中）

**划词翻译 · 整页翻译 · 一键还原**，推理全部在本机 Intel NPU 上完成，不联网、不上传任何文本。

## 架构

```
Chrome 插件（MV3）  ──HTTP──▶  server.py（127.0.0.1:8765）  ──▶  OpenVINO NPU 管线
 划词/整页/快捷键/弹窗        批量接口 + 逐句切分              全静态形状，约 70 ms/条
        │
        └──Native Messaging──▶  npu_host.bat ─▶ server_daemon.py ─▶ 无窗口拉起 server.py
```

浏览器插件无法直接运行 OpenVINO，也不能直接启动外部程序，所以分成两块：
一个**本地服务**（只绑定 `127.0.0.1`）和一个**本地消息宿主**（让插件能自行唤起服务）。

## 一、启动本地服务（三种方式，任选其一）

### 方式 A：插件自动唤起（最省事，无需手动点 bat）

一次性注册本地启动器，之后插件会在需要时自己把服务拉起来：

```
双击运行一次：<项目目录>\install_native_host.bat
```

之后：打开插件弹窗 → 若显示「未连接」，它会**自动启动服务**并显示进度；
翻译请求失败时也会自动唤起并重试。冷启动约 **9–11 秒**，之后一直很快（已运行 0.15 秒响应）。

### 方式 B：开机自启（一次设置，永远零操作）

```
双击运行一次：<项目目录>\install_autostart.bat
```

会在「启动」文件夹创建快捷方式，每次登录时**无窗口**后台启动服务（日志：`server.log`）。
取消：`uninstall_autostart.bat`。

### 方式 C：手动启动（用于排查）

双击 **`start_server.bat`**（保持窗口开着），看到 `listening on http://127.0.0.1:8765` 即就绪。

> 弹窗里可切换设备（**NPU** / CPU / GPU，默认 NPU）与端口；改端口后点「启动本地服务」生效。
> 停止服务：弹窗里的「停止服务」按钮，或双击 `stop_server.bat`。
> 状态查询：`python server_daemon.py --status --json`

## 二、安装插件（加载已解压的扩展程序）

1. 打开 Chrome，地址栏输入 `chrome://extensions`；
2. 打开右上角 **开发者模式**；
3. 点 **加载已解压的扩展程序**，选择目录 **`<项目目录>\chrome-extension`**；
4. （建议）在工具栏固定插件图标。

> 从旧版升级的话：改完代码后在 `chrome://extensions` 点该插件的 **重新加载**，
> 然后**刷新一下要翻译的网页**（旧标签页里可能还是旧版内容脚本）。
>
> 若方式 A 报「未注册本地启动器」：重新运行 `install_native_host.bat`。若仍失败，
> 到 `chrome://extensions` 复制本插件的 ID，然后执行
> `install_native_host.bat <你的扩展ID>`（或把 ID 存成 `chrome-extension\extension_id.txt` 再运行一次）。

## 三、使用

### 整页翻译（本次新增）

| 入口 | 操作 |
|---|---|
| 右键菜单 | 页面空白处右键 →「N-Translate：翻译整个页面」 |
| 快捷键 | `Alt+Shift+T` |
| 弹窗 | 点插件图标 → 「翻译整个页面」 |

翻译过程中页面**右下角有进度提示**（`翻译中 60/214（已写入 180 处）…`），
并且**逐块写回** —— 不用等整页翻完，译文会边翻边出现。

**还原本页原文**：右键菜单「还原本页原文」，或弹窗里的「还原原文」。

**自动翻译新增内容**：弹窗里勾选后，页面后续动态加载的内容（无限滚动、SPA 切页）会自动补译。
默认关闭。

### 选择翻译模型

弹窗顶部有**模型下拉框**，可在不同翻译模型之间切换：

| 模型 | 体积 | 质量 | 速度 | 说明 |
|---|---|---|---|---|
| **OPUS-MT 英→中**（默认） | 约 112MB | 2/5 | 5/5 | 随包提供，开箱即用，速度最快 |
| **NLLB-200 600M** | 约 620MB | 4/5 | 2/5 | 中文表达更自然；未随包提供，点「安装」按钮即可下载导出 |

要点：

- **切换模型会重启本地服务**：Intel NPU 驱动在同一进程内无法换用第二个模型
  （实测报 `ZE_RESULT_ERROR_INVALID_NATIVE_BINARY`），因此切换时服务会退出、
  由启动器用新模型重新拉起：
  - 用 `start_server.bat` 启动的：它检测到 `restart.flag` 后会**自动重启**；
  - 由插件启动的：插件通过 Native Messaging **自动重新拉起**。
  过程约 **5–30 秒**（取决于模型大小与 NPU 编译），弹窗里会显示进度。
- **新模型首次翻译前需要编译**（NPU 上 OPUS 约 5 秒、NLLB 约 30 秒），之后一直很快。
- 未安装的模型在下拉框里标注「（未安装）」，右侧会出现 **「安装（约 620MB）」** 按钮；
  点击后开始下载并导出，**可以关掉弹窗**，安装会在后台继续（进度写进 `install_<模型>.log`）。
  也可以命令行安装：双击 `install_model.bat nllb-200-600m`。
- 选择会记录在服务端 `settings.json`，重启服务后仍然生效。
- 模型能力与设备相关：若某模型缺少当前设备所需的变体，提示里会说明会回退到哪个设备。
- **许可提醒**：OPUS-MT 为 CC-BY-4.0；NLLB-200 为 **CC-BY-NC-4.0（仅限非商业用途）**。

### 其他

| 方式 | 操作 |
|---|---|
| 网页划词 | 选中英文 → 右键 →「用N-Translate选中文本」→ 选区下方弹出译文 |
| 快捷键 | 选中英文 → `Alt+T` |
| 弹窗翻译 | 粘贴英文 → 「翻 译」（或 Ctrl+Enter），可复制结果 |
| 服务状态 | 弹窗顶部显示连接状态与实际执行设备（应为 `NPU`） |

## 四、性能参考（本机 NPU 实测）

| 场景 | 耗时 |
|---|---|
| 单词/短语（如 `Sign in`） | 约 60–90 ms |
| 普通句子 | 约 70–140 ms |
| 多句长段落（4 句） | 约 550 ms |
| **40 条不同文本（去重后）** | **约 3.6 s** |
| 典型文章页（约 150–250 条去重文本） | 约 15–25 s（逐块写回，边翻边看） |

**加速要点**：插件会先**去重**（导航、页脚里大量重复文本只翻一次），再按 20 条一块请求。
页面越长，去重收益越大。

## 五、翻译质量说明（含内置 UI 词表）

超短字符串（导航、按钮）是小模型的弱项，早期实测 `Home` 会被译成复读乱码「內容內容內容」。
服务端因此内置了两层保护：

1. **常见 UI 词表**（约 100 条，精确匹配即返回固定译文，不走模型）——
   例如 `Home`→首页、`Sign in`→登录、`Search documentation`→搜索文档、`Getting started`→快速开始。
   附带好处：命中词表不消耗 NPU，整页翻译更快（实测 91 ms/条 → 70 ms/条）。
2. **退化输出兜底**：≤2 个单词的输入若译出复读型乱码，宁可保留英文原文，也不给出明显错误的中文。

词表在 `server.py` 的 `GLOSSARY` 里，直接按需增删即可（改完重启服务生效）。

## 六、已知限制

- **只做英→中**（由 OPUS-MT en→zh 模型决定）。想加中→英或多语种，需要换 NLLB 模型重建服务。
- **小模型质量有限**：短词组偶尔不准（例如 `Sign in` 译成「签署日期」而不是「登录」）。
  长句质量明显更好。追求质量可换 NLLB-200-distilled-600M（同流程，速度会下降）。
- **跨域 iframe 内的文字不会翻译**（同源 iframe 会）。受浏览器同源策略限制。
- **代码块、输入框、`contenteditable` 区域会跳过**（避免破坏代码和编辑内容）。
- 超过 128 个 token 的**单句**会被截断（模型输入上限）；正常句子远低于此。
- 整页翻译上限 1200 处文本，避免超长页面卡死。

## 七、常见问题

- **弹窗显示「未连接」**：点弹窗里的「启动本地服务」；若提示「未注册本地启动器」，
  先运行一次 `install_native_host.bat`（或改用 `install_autostart.bat` 一次性开机自启）。
- **提示 `Access to the specified native messaging host is forbidden`**：宿主已注册，
  但浏览器认为当前插件的 ID 不在允许列表里。双击 `fix_extension.py`（或
  `install_native_host.bat`）即可——它会**自动从浏览器配置里探测"已加载实例"的真实 ID**
  并一并允许，因此**无需移除重装**。
  本插件的 ID 由**安装目录**决定，当前目录对应
  `mfgnaekpafpefokdgdaemhhjddfhgoga`；把插件目录换到别处，ID 会随之改变，
  重跑一次 `fix_extension.py` 即可（它会按新路径重算）。
- **每次打开 Chrome 都要重新「加载已解压的扩展程序」**（已修复）：根因是 `manifest.json`
  里带了 `key` 字段。Chrome 对**已解压加载**的扩展有一条行为——manifest 带 `key` 时，
  重启浏览器会把它卸载掉；而且若同时存在两个"同路径不同 ID"的实例，Chrome 会因为
  **重复 key 而静默忽略**其中一个（不报错），看起来就像插件莫名其妙消失。
  **所以分发包里的 `manifest.json` 绝不能加 `key`**（`_verify_pkg.py` 会检查这一点）。
  修复脚本：`python fix_extension.py`。
- **用 Edge / Chromium 也可以**：注册脚本会同时注册
  Chrome、Edge、Chromium 三个浏览器的本地消息宿主。
- **右键菜单里没有「翻译整个页面」**：在 `chrome://extensions` 重新加载插件一次
  （菜单是在插件安装/更新时注册的）。
- **点整页翻译没反应**：整页翻译只在**顶层框架**执行；且 `chrome://`、扩展商店等受限页面无法注入。
- **首次翻译偏慢**：冷启动约 9–11 秒（加载 OpenVINO + NPU 编译模型）；用方式 B 开机自启可完全避免等待。
- **译文没出现/仍是英文**：看图区右下角进度提示；若报错会显示原因。
- **修改了 JS 文件**：`chrome://extensions` 点「重新加载」+ 刷新网页。
- **快捷键冲突**：`chrome://extensions/shortcuts` 里重新指定。
- **想看服务日志**：`<项目目录>\server.log`；本地宿主的日志在 `<项目目录>\npu_host.log`。

## 八、文件说明

```
<项目目录>\
├─ server.py                    本地翻译 HTTP 服务（/translate、/translate_batch、/models、/shutdown）
├─ setup.bat / setup.ps1        一次性安装（找 Python、建 venv、装依赖、自检、注册）——分发包首选用它
├─ pipelines.py                 翻译管线：静态形状（NPU）与 KV-cache（CPU/GPU）两种实现
├─ model_registry.py            模型注册表（模型清单、安装检测、设备可用性、当前选择）
├─ install_model.py / .bat      下载并导出指定模型为 OpenVINO IR（插件里也能一键触发）
├─ server_daemon.py             按需启动/停止/查询服务（无窗口，供插件与自启复用）
├─ npu_host.py / npu_host.bat   Chrome Native Messaging 宿主（插件借此唤起服务）
├─ install_native_host.py/.bat  一次性注册本地消息宿主（写 HKCU 注册表）
├─ fix_extension.py             修复「重启后被卸载」并注册正确扩展 ID（推荐用它）
├─ uninstall_native_host.bat    卸载注册
├─ install_autostart.ps1/.bat   一次性设置开机自启（启动文件夹快捷方式）
├─ uninstall_autostart.bat      取消开机自启
├─ start_server.bat             手动启动（前台窗口，排查用）
├─ stop_server.bat              停止服务
├─ native_host_manifest.json    由注册脚本生成；另有 .reg 作为备用导入方式
├─ tools\_api_test.py                 单段接口联调测试
├─ tools\_api_test_batch.py           批量接口测试（保序、长段落、整页规模耗时）
├─ _e2e\                        整页翻译端到端测试
│  ├─ test.html                 英文测试页（含代码块、重复导航项、placeholder）
│  ├─ jsdom_test.mjs            在真实 DOM(jsdom) 中跑 content.js，13 项断言
│  ├─ cdp_test.mjs              真实 Chrome + CDP 版（需在不受限环境运行）
│  └─ README.md                 测试运行说明
├─ chrome-extension\            Chrome 插件（MV3）
   ├─ manifest.json             权限：contextMenus/storage/scripting/activeTab/nativeMessaging
   ├─ background.js             HTTP 转发 + 本地宿主唤起 + 右键菜单 + 快捷键 + 注入兜底
   ├─ content.js                内容脚本：划词气泡 + 整页翻译 + 原文还原 + 增量翻译
   ├─ popup.html/css/js         弹窗：服务启停（自动唤起）、设备选择、整页翻译、粘贴翻译
   └─ icons\                    图标
└─ dist\                        分发包产物（用 _make_zips.ps1 重新生成）
   ├─ N-Translate\            免安装便携目录（路径全相对，可直接复制到其它电脑）
   ├─ N-Translate-Portable-v1.3.2.zip   完整包（含量化模型，约 193MB）
   └─ N-Translate-Extension-v1.3.2.zip  仅扩展（约 20KB）
```

## 九、接口（供扩展或自用脚本调用）

- `GET /health` →
  `{"ok":true,"device":"NPU","execution_devices":"NPU","openvino":"...","max_batch":200}`
- `POST /translate` `{"text":"..."}` →
  `{"ok":true,"sentences":[...],"translations":[...],"latency_ms":[...],"total_ms":...,"translation":"..."}`
- `POST /translate_batch` `{"texts":["...","..."]}` →
  `{"ok":true,"count":N,"translations":["...",...],"total_ms":...}`（**保序**，整页翻译用）
- `POST /shutdown` → 优雅停止服务

### Native Messaging 协议（宿主 `com.npu.translate`）

Chrome ↔ 宿主：4 字节小端长度 + UTF-8 JSON。
请求 `{"cmd":"ensure"|"status"|"ping"}`，应答 `{"ok":true,"state":"started|already-running|running|stopped",...}`。

测试：`python tools\_api_test.py`、`python tools\_api_test_batch.py`、`node _e2e\jsdom_test.mjs`
