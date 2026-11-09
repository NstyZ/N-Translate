// background.js —— service worker：本地服务通信 + 页面级动作 + 自动唤起本地服务 + 热更新
const DEFAULT_SERVER = "http://127.0.0.1:8765";
const NATIVE_HOST = "com.npu.translate";
const HOST_HINT = "未注册本地启动器：请先双击运行一次 install_native_host.bat（或用 install_autostart.bat 开机自启）";

// ---------------------------------------------------------------- 热更新
// 本项目的更新源。如果你是 fork 或自建镜像，把这行改成 <用户名>/<仓库名> 即可。
const REPO_SLUG = "NstyZ/N-Translate";
const RAW_UPDATE_URL = `https://raw.githubusercontent.com/${REPO_SLUG}/main/update.json`;
const JSDELIVR_URL = `https://cdn.jsdelivr.net/gh/${REPO_SLUG}@main/update.json`;
const GH_PROXY = "https://gh-proxy.com/";
// 国内直连 github.com 的 archive 下载基本必失败，所以镜像放第一位（实测 2.5s / 117KB）
const REPO_ZIP_URLS = [
  `${GH_PROXY}https://github.com/${REPO_SLUG}/archive/refs/heads/main.zip`,
  `https://github.com/${REPO_SLUG}/archive/refs/heads/main.zip`,
  `https://ghproxy.net/https://github.com/${REPO_SLUG}/archive/refs/heads/main.zip`,
];

function versionParts(v) {
  return String(v || "0").split(/[.\-+]/).map((x) => parseInt(x, 10) || 0);
}

// 返回 1 表示 a > b，-1 表示 a < b，0 表示相等
function cmpVersion(a, b) {
  const pa = versionParts(a), pb = versionParts(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d > 0 ? 1 : -1;
  }
  return 0;
}

async function updateSources() {
  const { updateUrl } = await chrome.storage.sync.get(["updateUrl"]);
  const custom = (updateUrl || "").trim().replace(/\/+$/, "");
  const list = [];
  if (custom) {
    list.push(custom);
    // 自定义地址是 raw.githubusercontent 时，额外挂两个镜像兜底（国内直连常被拦）
    if (custom.includes("raw.githubusercontent.com")) {
      list.push(GH_PROXY + custom, custom.replace("raw.githubusercontent.com",
        "raw.gitmirror.com").replace("https://raw.gitmirror.com", "https://raw.gitmirror.com"));
    }
  } else {
    list.push(RAW_UPDATE_URL, JSDELIVR_URL, GH_PROXY + RAW_UPDATE_URL);
  }
  return { list, custom, auto: (await chrome.storage.sync.get(["autoUpdate"])).autoUpdate !== false };
}

async function checkUpdate() {
  const { list, custom, auto } = await updateSources();
  const current = chrome.runtime.getManifest().version;
  if (!custom && REPO_SLUG.startsWith("CHANGE_ME")) {
    return {
      ok: false, current, needConfig: true,
      error: "还没配置更新地址。把 background.js 顶部的 REPO_SLUG 改成你的仓库"
        + "（例如 yourname/N-Translate），或直接在下面「更新地址」里填写 update.json 的完整地址。",
    };
  }
  let info = null, used = "", lastErr = "";
  for (const u of list) {
    try {
      const res = await fetch(u, { cache: "no-store" });
      if (!res.ok) { lastErr = "HTTP " + res.status; continue; }
      info = await res.json();
      used = u;
      break;
    } catch (e) { lastErr = String(e?.message || e); }
  }
  if (!info) {
    return { ok: false, current, error: lastErr || "无法读取更新信息", url: list[0], custom };
  }
  const latest = String(info.version || "0");
  return {
    ok: true, current, latest, notes: info.notes || [], date: info.date || "",
    url: used, custom, auto, hasUpdate: cmpVersion(latest, current) > 0,
  };
}

// 走本地宿主（Python）下载并覆盖源码 —— 扩展自己没权限写自己的目录
async function applyUpdate() {
  const target = REPO_ZIP_URLS[0];
  const res = await nativeRequest({ cmd: "update", urls: REPO_ZIP_URLS, url: target }, 900000);
  if (res?.ok) {
    chrome.action.setBadgeText({ text: "" });
  }
  return res;
}

// 每小时静默检查一次；有新版就在图标上点个角标
async function autoCheckUpdate() {
  try {
    const { auto } = await updateSources();
    if (!auto) return;
    const r = await checkUpdate();
    if (r.ok && r.hasUpdate) {
      await chrome.storage.local.set({ pendingUpdate: r.latest });
      chrome.action.setBadgeText({ text: "↑" });
      chrome.action.setBadgeBackgroundColor({ color: "#2e7d32" });
    }
  } catch (_) { /* 静默 */ }
}

chrome.alarms?.create("update-check", { periodInMinutes: 60 });
chrome.alarms?.onAlarm.addListener((a) => {
  if (a.name === "update-check") autoCheckUpdate();
});

// ---------------------------------------------------------------- 配置
async function cfg() {
  const { serverUrl, device } = await chrome.storage.sync.get(["serverUrl", "device"]);
  const base = (serverUrl || DEFAULT_SERVER).replace(/\/+$/, "");
  let port = 8765;
  try { port = Number(new URL(base).port || 8765); } catch (_) {}
  return { base, port, device: device || "NPU" };
}

// ---------------------------------------------------------------- HTTP
async function api(path, body) {
  const { base } = await cfg();
  const res = await fetch(base + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

// -------------------------------------------------- Native Messaging
// 通过已注册的本地宿主启动服务；宿主会在后台以无窗口方式拉起 server.py
function nativeRequest(payload, timeoutMs = 120000) {
  return new Promise((resolve) => {
    let port;
    try {
      port = chrome.runtime.connectNative(NATIVE_HOST);
    } catch (e) {
      return resolve({ ok: false, error: String(e?.message || e) });
    }
    let settled = false;
    const done = (v) => {
      if (settled) return;
      settled = true;
      try { port.disconnect(); } catch (_) {}
      resolve(v);
    };
    port.onMessage.addListener((m) => done(m || { ok: false, error: "empty reply" }));
    port.onDisconnect.addListener(() => {
      const err = chrome.runtime.lastError?.message || "native host disconnected";
      done({ ok: false, error: err });
    });
    try { port.postMessage(payload); } catch (e) { done({ ok: false, error: String(e?.message || e) }); }
    setTimeout(() => done({ ok: false, error: "native host timeout" }), timeoutMs);
  });
}

// 服务进程起来了但模型还在编译时，/health 会返回 ready:false（HTTP 200）。
// 这里轮询等它就绪，避免用户在"正在加载"的几秒里看到翻译失败。
async function waitReady(maxMs = 90000) {
  const t0 = Date.now();
  for (;;) {
    try {
      const h = await api("/health");
      if (h.ready !== false) return h;
    } catch (_) { /* 服务还没起来，继续等 */ }
    if (Date.now() - t0 > maxMs) return null;
    await new Promise((r) => setTimeout(r, 400));
  }
}

// 确保服务可用（已运行则直接返回；否则经本地宿主启动）
async function ensureServer() {
  try {
    await api("/health");
    return { ok: true, state: "already-running", health: await waitReady() };
  } catch (_) { /* 没在跑，继续启动 */ }

  const { port, device } = await cfg();
  const res = await nativeRequest({ cmd: "ensure", device, port });
  if (!res?.ok) {
    return {
      ok: false,
      error: res?.error || "启动失败",
      hint: res?.hint || HOST_HINT,
      needSetup: !!res?.needSetup,
    };
  }
  const health = await waitReady();
  return { ok: true, state: res.state, health, elapsed: res.elapsed };
}

// 带「自动启动并重试」的请求
async function apiAuto(path, body) {
  try {
    return await api(path, body);
  } catch (firstErr) {
    const started = await ensureServer();
    if (!started.ok) {
      const err = new Error(`${firstErr.message}；自动启动失败：${started.error}`);
      err.hint = started.hint;
      throw err;
    }
    if (started.health == null) {
      throw new Error("本地服务正在加载模型，请稍等几秒再试");
    }
    return await api(path, body);   // 启动成功，重试一次
  }
}

// ---------------------------------------------------------------- 菜单/快捷键
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "translate-selection",
      title: "N-Translate：翻译选中文本（英→中）",
      contexts: ["selection"],
    });
    chrome.contextMenus.create({
      id: "translate-page",
      title: "N-Translate：翻译整个页面",
      contexts: ["page", "frame"],
    });
    chrome.contextMenus.create({
      id: "restore-page",
      title: "还原本页原文",
      contexts: ["page", "frame"],
    });
  });
});

const MENU_TO_MSG = {
  "translate-selection": null,
  "translate-page": "TRANSLATE_PAGE",
  "restore-page": "RESTORE_PAGE",
};

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (!tab?.id) return;
  if (info.menuItemId === "translate-selection") {
    sendToTab(tab.id, { type: "SHOW_LOADING" }, false);
    return;
  }
  const type = MENU_TO_MSG[info.menuItemId];
  if (type) sendToTab(tab.id, { type }, false);
});

chrome.commands.onCommand.addListener(async (command) => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  if (command === "translate-selection") sendToTab(tab.id, { type: "SHOW_LOADING" }, false);
  else if (command === "translate-page") sendToTab(tab.id, { type: "TRANSLATE_PAGE" }, false);
});

// ---------------------------------------------------------------- 与页面通信
async function ensureContent(tabId) {
  try {
    const r = await chrome.tabs.sendMessage(tabId, { type: "PING" });
    if (r?.ok) return true;
  } catch (_) { /* 无接收方，尝试注入 */ }
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    return true;
  } catch (_) {
    return false;
  }
}

async function sendToTab(tabId, msg, awaitReply = true) {
  const ok = await ensureContent(tabId);
  if (!ok) return { ok: false, error: "无法在此页面运行（受限页面）" };
  try {
    if (awaitReply) return await chrome.tabs.sendMessage(tabId, msg);
    chrome.tabs.sendMessage(tabId, msg, () => void chrome.runtime.lastError);
    return { ok: true, started: true };
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
}

// ---------------------------------------------------------------- 消息路由
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  switch (msg?.type) {
    case "TAB_ACTION": {
      (async () => {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab?.id) return;
        if (msg.action === "autoNew") {
          sendToTab(tab.id, { type: "SET_AUTO_NEW", value: msg.value }, false);
          return;
        }
        const type = msg.action === "restore" ? "RESTORE_PAGE" : "TRANSLATE_PAGE";
        await sendToTab(tab.id, { type }, false);   // 可能耗时较久：即发即忘
      })();
      sendResponse({ ok: true, started: true });
      return;
    }

    case "ENSURE_SERVER":
      ensureServer()
        .then((r) => sendResponse(r))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    case "STOP_SERVER":
      api("/shutdown", {})
        .then(() => sendResponse({ ok: true, state: "stopped" }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    // ---- 模型列表 / 切换 / 安装（这几项不自动启服务，由弹窗先确保服务可用）----
    case "MODELS":
      api("/models")
        .then((data) => sendResponse({ ok: true, data }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    case "SELECT_MODEL":
      api("/models/select", { id: msg.id })
        .then((data) => sendResponse({ ok: true, data }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    case "INSTALL_MODEL":
      api("/models/install", { id: msg.id })
        .then((data) => sendResponse({ ok: true, data }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    case "INSTALL_STATUS":
      api("/models/install_status", { id: msg.id })
        .then((data) => sendResponse({ ok: true, data }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    case "TRANSLATE":
      apiAuto("/translate", { text: msg.text })
        .then((data) => sendResponse({ ok: true, data }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e), hint: e?.hint }));
      return true;

    case "TRANSLATE_BATCH":
      apiAuto("/translate_batch", { texts: msg.texts })
        .then((data) => sendResponse({ ok: true, data }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e), hint: e?.hint }));
      return true;

    case "HEALTH":
      // 只查询，不自动启动（由 popup 显式决定是否启动）
      api("/health")
        .then((data) => sendResponse({ ok: true, data }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    // ---- 运行环境体检 / 自动修复 ----
    case "DOCTOR":
      nativeRequest({ cmd: "doctor", fix: !!msg.fix, withModel: !!msg.withModel },
                    3600000)
        .then((r) => sendResponse(r || { ok: false, error: "环境检查没有返回结果" }))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    // ---- 热更新 ----
    case "CHECK_UPDATE":
      checkUpdate()
        .then((r) => sendResponse(r))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    case "APPLY_UPDATE":
      applyUpdate()
        .then((r) => sendResponse(r))
        .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
      return true;

    case "PENDING_UPDATE":
      chrome.storage.local.get(["pendingUpdate"])
        .then((d) => sendResponse({ ok: true, version: d.pendingUpdate || "" }))
        .catch(() => sendResponse({ ok: true, version: "" }));
      return true;

    case "CLEAR_UPDATE_BADGE":
      chrome.action.setBadgeText({ text: "" });
      chrome.storage.local.remove(["pendingUpdate"]);
      sendResponse({ ok: true });
      return;
  }
});
