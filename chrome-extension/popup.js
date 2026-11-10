// popup.js —— 弹窗：模型选择/安装、服务启停、整页翻译、粘贴翻译
const $ = (id) => document.getElementById(id);
const DEFAULT_SERVER = "http://127.0.0.1:8765";

let lastTranslation = "";
let busy = false;
let modelsCache = [];
let pollTimer = null;

function setDot(cls) {
  document.querySelector("#status .dot").className = "dot" + (cls ? " " + cls : "");
}

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (resp) => {
      if (chrome.runtime.lastError) return resolve({ ok: false, error: chrome.runtime.lastError.message });
      resolve(resp || { ok: false, error: "no response" });
    });
  });
}

function mHint(text, isErr) {
  $("model-hint").textContent = text || "";
  $("model-hint").className = "hint" + (isErr ? " err" : "");
}

function hint(text, isErr) {
  $("server-hint").textContent = text || "";
  $("server-hint").className = "hint" + (isErr ? " err" : "");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------- 服务状态 / 启停 ----------------
async function refreshHealth() {
  const resp = await send({ type: "HEALTH" });
  if (!resp.ok) {
    setDot("bad");
    $("status-text").textContent = "未连接";
    return false;
  }
  const d = resp.data;
  setDot("ok");
  const link = d.browser_linked ? "· 随浏览器退出" : "";
  $("status-text").textContent =
    `已连接 · ${d.execution_devices || d.device} · ${d.model_label || d.model} ${link}`.trim();
  return true;
}

async function ensureServer(showHint = true) {
  if (busy) return false;
  busy = true;
  $("start-server").disabled = true;
  if (showHint) hint("正在启动本地服务（首次约 10 秒）…");
  const resp = await send({ type: "ENSURE_SERVER" });
  $("start-server").disabled = false;
  busy = false;

  if (resp.ok) {
    const dev = resp.health?.execution_devices || resp.health?.device || "";
    hint(`服务已就绪（${resp.state === "already-running" ? "原本就在运行" : "已启动"}` +
         `${resp.elapsed ? "，" + resp.elapsed + "s" : ""}）· ${dev}`);
    $("env-box").hidden = true;
    await refreshHealth();
    return true;
  }
  hint(resp.hint ? `${resp.error}\n${resp.hint}` : (resp.error || "启动失败"), true);
  // 环境没装好：直接把「检查/修复」摆出来，别让用户去猜
  if (resp.needSetup) {
    showEnvBox("运行环境还没装好。点下面的「检查运行环境」，缺什么会列出来，再点「自动修复」即可。", true);
    $("doctor-fix").hidden = true;
  }
  await refreshHealth();
  return false;
}

// ---------------- 模型列表 / 切换 / 安装 ----------------
async function loadModels() {
  const resp = await send({ type: "MODELS" });
  if (!resp.ok) {
    $("model").innerHTML = "<option>（服务未启动）</option>";
    mHint("本地服务未就绪，无法读取模型列表", true);
    $("install-model").hidden = true;
    return;
  }
  const { active, models } = resp.data;
  modelsCache = models;

  $("model").innerHTML = models.map((m) =>
    `<option value="${m.id}"${m.id === active ? " selected" : ""}>` +
    `${escapeHtml(m.label)}${m.installed ? "" : "（未安装）"}</option>`).join("");

  const cur = models.find((m) => m.id === active);
  if (cur) {
    const dev = $("device").value;
    const devWarn = cur.devices.length && !cur.devices.includes(dev)
      ? `\n注意：该模型暂无 ${dev} 变体，会回退到 ${cur.devices.join("/")}`
      : "";
    mHint(`${cur.desc}\n约 ${cur.size_mb}MB · 质量 ${cur.quality}/5 · 速度 ${cur.speed}/5` +
          ` · 可用设备 ${cur.devices.join("/") || "无"}${devWarn}`, !!devWarn);
  }
  updateInstallButton();
}

function updateInstallButton() {
  const m = modelsCache.find((x) => x.id === $("model").value);
  const need = m && !(m.has_cached && m.has_static);
  $("install-model").hidden = !need;
  if (need) $("install-model").textContent = `安装（约 ${m.size_mb}MB）`;
}

// 等待服务重新可用（切换模型会重启服务、重新编译 NPU 模型）
async function waitForServer(timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await new Promise((r) => setTimeout(r, 2000));
    if ((await send({ type: "HEALTH" })).ok) return true;
    mHint(`正在用新模型重启服务并编译…（已等待 ${Math.round((Date.now() - t0) / 1000)} 秒）`);
  }
  return false;
}

async function selectModel(id) {
  const m = modelsCache.find((x) => x.id === id);
  if (m && !m.installed) {
    mHint(`「${m.label}」尚未安装：点右侧「安装（约 ${m.size_mb}MB）」下载并导出`, true);
    updateInstallButton();
    return;
  }
  const resp = await send({ type: "SELECT_MODEL", id });
  if (!resp.ok) {
    mHint(`切换失败：${resp.error}`, true);
    return;
  }
  await chrome.storage.sync.set({ model: id });

  if (resp.data.restart) {
    // NPU 驱动无法在同一进程内换模型：服务会退出，由本地宿主/启动器重新拉起
    mHint(`正在用「${resp.data.label}」重启服务…\n${resp.data.note || ""}`);
    await new Promise((r) => setTimeout(r, 3000));      // 等服务退出
    const started = await ensureServer(true);           // 通过本地宿主重新拉起
    if (!started) {
      mHint("服务未自动拉起：请点「启动本地服务」，或双击 start_server.bat（它会自动用新模型启动）", true);
      return;
    }
    if (!(await waitForServer(90000))) {
      mHint("服务重启超时，请稍后在弹窗里刷新", true);
      return;
    }
  }
  mHint(`已切换到「${resp.data.label}」✅`);
  await refreshHealth();
  await loadModels();
}

async function installModel() {
  const id = $("model").value;
  const m = modelsCache.find((x) => x.id === id);
  $("install-model").disabled = true;
  mHint(`已开始下载并导出「${m ? m.label : id}」（约 ${m ? m.size_mb : "?"}MB）。\n` +
        "首次可能需要 10–30 分钟；**可以关掉这个弹窗**，安装会在后台继续。");
  const resp = await send({ type: "INSTALL_MODEL", id });
  if (!resp.ok) {
    mHint(`安装启动失败：${resp.error}`, true);
    $("install-model").disabled = false;
    return;
  }
  pollInstall(id);
}

function pollInstall(id) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const r = await send({ type: "INSTALL_STATUS", id });
    if (!r.ok) return;
    const d = r.data;
    const last = (d.log_tail || "").split("\n").map((s) => s.trim()).filter(Boolean).pop() || "";
    if (d.running) {
      mHint(`正在安装…\n${last}`);
    } else {
      clearInterval(pollTimer);
      $("install-model").disabled = false;
      mHint(d.installed ? "安装完成 ✅" : `安装结束但未完成（可查看 install_${id}.log）\n${last}`,
            !d.installed);
      await loadModels();
    }
  }, 3000);
}

// ---------------- 热更新 ----------------
let pendingVersion = "";

function uHint(text, isErr) {
  $("update-hint").textContent = text || "";
  $("update-hint").className = "hint" + (isErr ? " err" : "");
}

async function refreshVersionLine() {
  const cur = chrome.runtime.getManifest().version;
  $("ver-line").textContent = `当前版本 v${cur}` +
    (pendingVersion ? ` · 有新版本 v${pendingVersion}` : "");
}

async function checkUpdate(manual = true) {
  if (manual) uHint("正在检查更新…");
  const r = await send({ type: "CHECK_UPDATE" });
  if (!r.ok) {
    uHint(`检查失败：${r.error}\n（可把「更新地址」改成 raw.githubusercontent.com 或 jsDelivr 的地址）`, true);
    $("apply-update").hidden = true;
    return;
  }
  $("cur-ver").textContent = r.current;
  if (!r.hasUpdate) {
    pendingVersion = "";
    $("apply-update").hidden = true;
    uHint(`已是最新版 v${r.current}${r.latest !== r.current ? `（远端 v${r.latest}）` : ""}`);
    await send({ type: "CLEAR_UPDATE_BADGE" });
    await refreshVersionLine();
    return;
  }
  pendingVersion = r.latest;
  $("apply-update").hidden = false;
  $("apply-update").textContent = `立即更新到 v${r.latest}`;
  const notes = (r.notes || []).map((n) => "· " + n).join("\n");
  uHint(`发现新版本 v${r.latest}${r.date ? `（${r.date}）` : ""}\n${notes}`);
  await refreshVersionLine();
}

async function applyUpdate() {
  $("apply-update").disabled = true;
  uHint("正在下载并覆盖源码…（会先停掉本地服务，几十秒）");
  const r = await send({ type: "APPLY_UPDATE" });
  $("apply-update").disabled = false;

  if (!r.ok) {
    uHint(`更新失败：${r.error}\n可以改用分发包手动覆盖，或检查更新地址`, true);
    return;
  }
  uHint(`已更新 ${r.files || 0} 个文件${r.from && r.to ? `（v${r.from} → v${r.to}）` : ""}。\n` +
        "最后一步：打开 chrome://extensions ，点本插件的「重新加载」按钮，改动才会生效。");
  $("apply-update").hidden = true;
  pendingVersion = "";
  await send({ type: "CLEAR_UPDATE_BADGE" });
  await refreshVersionLine();
}

// ---------------- 运行环境体检 / 自动修复 ----------------
function showEnvBox(text, isErr = true) {
  $("env-box").hidden = false;
  $("env-text").textContent = text || "";
  $("env-text").className = "hint" + (isErr ? " err" : "");
}

function renderDoctor(r) {
  if (!r || r.error) {
    showEnvBox(`环境检查失败：${r?.error || "无返回"}`, true);
    return;
  }
  const items = r.items || [];
  const bad = items.filter((i) => i.status !== "ok");
  const lines = items.map((i) => {
    const mark = i.status === "ok" ? "✓" : i.status === "warn" ? "!" : "✗";
    return `${mark} ${i.title}：${i.detail}`;
  });
  const head = r.ok
    ? `环境就绪（${items.length} 项通过）`
    : `发现 ${bad.length} 项需要注意`;
  showEnvBox(head + "\n" + lines.join("\n"), !r.ok && bad.some((i) => i.status === "fail"));
  $("doctor-fix").hidden = !bad.some((i) => i.fixable);
}

async function runDoctor(fix = false) {
  const btns = [$("doctor-check"), $("doctor-fix")];
  btns.forEach((b) => (b.disabled = true));
  $("doctor-fix").textContent = fix ? "修复中…" : "自动修复";
  showEnvBox(fix
    ? "正在自动补齐缺失的运行环境…\n（首次可能要下载依赖或模型，最长几分钟）"
    : "正在检查运行环境…", false);
  const r = await send({ type: "DOCTOR", fix, withModel: false });
  btns.forEach((b) => (b.disabled = false));
  $("doctor-fix").textContent = "自动修复";
  renderDoctor(r);
  if (fix && r && r.ok) {
    hint("环境已补齐，点「启动本地服务」即可");
  }
}

// ---------------- 粘贴翻译 ----------------
async function doTranslate() {
  const text = $("input").value.trim();
  if (!text) return;
  $("translate").disabled = true;
  $("stat").textContent = "翻译中…";
  $("result").hidden = false;
  $("result").innerHTML = '<span class="err">翻译中…</span>';

  const resp = await send({ type: "TRANSLATE", text });
  $("translate").disabled = false;

  if (!resp.ok) {
    $("stat").textContent = "";
    $("result").innerHTML = `<span class="err">失败：${escapeHtml(resp.error)}</span>`;
    if (resp.hint) hint(resp.hint, true);
    await refreshHealth();
    return;
  }
  const d = resp.data;
  lastTranslation = d.translation;
  $("stat").textContent = `${d.total_ms} ms · ${d.sentences.length} 句 · ${d.execution_devices}`;
  $("result").textContent = d.translation;
}

// ---------------- 整页翻译 ----------------
async function pageAction(action) {
  if (!(await refreshHealth())) {
    hint("本地服务未启动，正在尝试自动启动…");
    if (!(await ensureServer())) return;
  }
  await send({ type: "TAB_ACTION", action });
  $("page-hint").className = "hint";
  $("page-hint").textContent = action === "restore"
    ? "已请求还原原文。"
    : "已开始整页翻译，进度见页面右下角；大页面耗时较久。";
}

// ---------------- 事件绑定 ----------------
$("translate").addEventListener("click", doTranslate);
$("input").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") doTranslate();
});
$("copy").addEventListener("click", async () => {
  if (!lastTranslation) return;
  await navigator.clipboard.writeText(lastTranslation);
  $("copy").textContent = "已复制";
  setTimeout(() => ($("copy").textContent = "复制结果"), 1200);
});

$("model").addEventListener("change", (e) => selectModel(e.target.value));
$("install-model").addEventListener("click", installModel);

$("start-server").addEventListener("click", () => ensureServer(true));
$("doctor-check").addEventListener("click", () => runDoctor(false));
$("doctor-fix").addEventListener("click", () => runDoctor(true));
$("stop-server").addEventListener("click", async () => {
  const resp = await send({ type: "STOP_SERVER" });
  hint(resp.ok ? "已停止本地服务。" : `停止失败：${resp.error}`, !resp.ok);
  await refreshHealth();
  await loadModels();
});
$("translate-page").addEventListener("click", () => pageAction("translate"));
$("restore-page").addEventListener("click", () => pageAction("restore"));

$("auto-new").addEventListener("change", async (e) => {
  await chrome.storage.sync.set({ autoNew: e.target.checked });
  await send({ type: "TAB_ACTION", action: "autoNew", value: e.target.checked });
});

$("device").addEventListener("change", async (e) => {
  await chrome.storage.sync.set({ device: e.target.value });
  hint(`设备已设为 ${e.target.value}（下次启动服务时生效；如需立即生效请先「停止服务」）`);
  loadModels();
});

$("save-url").addEventListener("click", async () => {
  const v = $("server-url").value.trim() || DEFAULT_SERVER;
  await chrome.storage.sync.set({ serverUrl: v.replace(/\/+$/, "") });
  $("save-url").textContent = "已保存";
  setTimeout(() => ($("save-url").textContent = "保存"), 1200);
  refreshHealth();
  loadModels();
});

// ---- 热更新相关 ----
$("check-update").addEventListener("click", () => checkUpdate(true));
$("apply-update").addEventListener("click", applyUpdate);

$("save-update-url").addEventListener("click", async () => {
  const v = $("update-url").value.trim();
  await chrome.storage.sync.set({ updateUrl: v });
  $("save-update-url").textContent = "已保存";
  setTimeout(() => ($("save-update-url").textContent = "保存"), 1200);
  uHint(v ? `已设置自定义更新地址：${v}` : "已清空，改用仓库内置地址");
});

$("auto-update").addEventListener("change", async (e) => {
  await chrome.storage.sync.set({ autoUpdate: e.target.checked });
  uHint(e.target.checked ? "已开启自动检查（每小时）" : "已关闭自动检查");
});

// ---------------- 初始化 ----------------
(async () => {
  const { serverUrl, autoNew, device, updateUrl, autoUpdate } =
    await chrome.storage.sync.get(["serverUrl", "autoNew", "device", "updateUrl", "autoUpdate"]);
  $("server-url").value = serverUrl || DEFAULT_SERVER;
  $("auto-new").checked = !!autoNew;
  $("device").value = device || "NPU";
  $("update-url").value = updateUrl || "";
  $("auto-update").checked = autoUpdate !== false;

  const pend = await send({ type: "PENDING_UPDATE" });
  pendingVersion = pend.ok ? pend.version : "";
  await refreshVersionLine();

  // 打开弹窗时顺手查一次（不阻塞启动流程）
  checkUpdate(false);

  let alive = await refreshHealth();
  if (!alive) alive = await ensureServer(true);
  if (alive) await loadModels();
  else await loadModels();   // 让模型区显示"服务未启动"的提示
})();
