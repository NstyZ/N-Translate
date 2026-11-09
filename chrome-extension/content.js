// content.js —— 页面内功能：
//   1) 划词翻译气泡
//   2) 整页翻译（收集文本节点 → 去重 → 分块请求 → 写回 → 可一键还原）
//   3) 增量翻译（页面后续新增内容自动补译，可在弹窗里开关）
(() => {
  if (window.__npuTranslateLoaded) return;
  window.__npuTranslateLoaded = true;

  const HOST_ID = "__npu-translate-host";
  const CHUNK = 20;                 // 每个批量请求的条目数
  const MAX_ITEMS = 1200;           // 单次整页翻译最多处理多少处，防止超长页面卡死
  const isTop = window.top === window;

  // 不翻译的区域
  //  注意：这个字符串会直接交给 Element.closest()，必须是**合法 CSS 选择器**。
  //  曾经写成 .language-*（星号不能那样用），Chrome 抛 "is not a valid selector"，
  //  整页翻译直接中断。离线校验不可靠（node --check、jsdom、css-tree 都放过了它），
  //  所以改成运行时逐片段自检：只保留浏览器认可的片段，非法片段丢弃而不是炸掉整页。
  const SKIP_SELECTOR_RAW =
    'script,style,noscript,textarea,code,pre,kbd,samp,svg,canvas,iframe,math,time,' +
    '[contenteditable="true"],[translate="no"],.notranslate,' +
    // 代码与命令行片段（很多站点用 span/div 标出来，不带 code 标签）
    '.highlight,.hljs,.CodeMirror,.monaco-editor,.cm-editor,.blob-code,.blob-wrapper,' +
    '.pl-c,.pl-s,.pl-k,.pl-en,[class*="language-"],code[class*="language-"],' +
    '[class*="code-block"],[class*="codeblock"],[class*="snippet"],[class*="highlight"],' +
    // 版本号 / 元信息 / 快捷键 / 徽标 / 头像与图标
    '.version,.commit-sha,.sha,.hash,.oid,.commit-id,.file-path,.path,.filename,.badge,' +
    '.counter,.kbd,.key,.shortcut,.avatar,.icon,.logo,.tooltip';

  // 逐片段探测：用浏览器自己的解析器判定合法性（这是唯一权威的判定）
  function validatedSelector(raw, fallback) {
    const keep = [];
    const dropped = [];
    for (const part of String(raw).split(",")) {
      const s = part.trim();
      if (!s) continue;
      try {
        document.querySelector(s);
        keep.push(s);
      } catch (_) {
        dropped.push(s);
      }
    }
    if (dropped.length) {
      // 不静默吞掉：留痕便于排查（扩展的 console 可在扩展页面的检查器里看到）
      console.warn("[NPU翻译] 已忽略非法跳过选择器片段:", dropped.join(", "));
    }
    return keep.length ? keep.join(",") : fallback;
  }

  const SKIP_SELECTOR = validatedSelector(
    SKIP_SELECTOR_RAW, "script,style,noscript,textarea,code,pre");
  const ATTRS = ["placeholder", "title", "alt", "aria-label"];

  // 这些元素里的文字按「不该整体翻译」处理（时间戳、计数等）
  const META_TAGS = new Set(["TIME", "VAR", "OUTPUT", "PROGRESS", "METER", "DIALOG"]);

  // ---------------------------------------------------------------- UI
  function ensureHost() {
    let host = document.getElementById(HOST_ID);
    if (host && host.shadowRoot) return host.shadowRoot;
    host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = "all:initial";
    (document.body || document.documentElement).appendChild(host);
    const root = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = `
      .bubble {
        position: absolute; z-index: 2147483647; max-width: 460px;
        background: #fff; color: #1f2328; border: 1px solid #d0d7de;
        border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.18);
        font: 13px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        padding: 10px 12px; display: none;
      }
      .bubble .src { color: #57606a; font-size: 12px; margin-bottom: 4px;
                     word-break: break-word; max-height: 3.2em; overflow: hidden; }
      .bubble .dst { font-size: 14px; white-space: pre-wrap; word-break: break-word; }
      .bubble .meta { color: #8b949e; font-size: 11px; margin-top: 6px;
                      display: flex; align-items: center; gap: 6px; }
      .dot { width: 8px; height: 8px; border-radius: 50%;
             background: #1a7f37; display: inline-block; }
      .bubble.err .dot { background: #cf222e; }
      .bubble .close { margin-left: auto; cursor: pointer; color: #8b949e;
                       border: 0; background: none; font-size: 14px; padding: 0 2px; }
      .bubble .close:hover { color: #1f2328; }
      .loading { display: inline-block; width: 12px; height: 12px;
                 border: 2px solid #d0d7de; border-top-color: #1a73e8;
                 border-radius: 50%; animation: npn-spin .8s linear infinite; }
      @keyframes npn-spin { to { transform: rotate(360deg); } }

      .toast {
        position: fixed; right: 18px; bottom: 18px; z-index: 2147483647;
        background: #1f2328; color: #fff; padding: 9px 14px; border-radius: 9px;
        box-shadow: 0 6px 20px rgba(0,0,0,.28); display: none; align-items: center; gap: 8px;
        font: 12px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
      }
      .toast.show { display: flex; }
      .toast .spin { width: 11px; height: 11px; border: 2px solid rgba(255,255,255,.35);
                     border-top-color: #fff; border-radius: 50%;
                     animation: npn-spin .8s linear infinite; }
    `;
    root.appendChild(style);
    return root;
  }

  function toast(text, spinning, autoHide, isError) {
    const root = ensureHost();
    let el = root.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      root.appendChild(el);
    }
    el.innerHTML = (spinning ? '<span class="spin"></span>' : "") +
      `<span>${esc(text)}</span>` +
      (isError ? "" : '<button class="close" title="关闭">✕</button>');
    el.style.background = isError ? "#8b1a1a" : "#1f2328";
    el.classList.add("show");
    el.querySelector(".close")?.addEventListener("click", () => el.classList.remove("show"));
    if (autoHide) setTimeout(() => el.classList.remove("show"), 2600);
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // -------------------------------------------------- 划词翻译气泡
  function getSelectionInfo() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    const r = sel.getRangeAt(0).getBoundingClientRect();
    return {
      text,
      rect: { left: r.left, top: r.top, bottom: r.bottom },
    };
  }

  function render(html, rect) {
    const root = ensureHost();
    let bubble = root.querySelector(".bubble");
    if (!bubble) {
      bubble = document.createElement("div");
      bubble.className = "bubble";
      root.appendChild(bubble);
    }
    bubble.innerHTML = html;
    bubble.style.display = "block";

    bubble.style.left = Math.max(8, Math.min(
      rect.left + window.scrollX, window.scrollX + window.innerWidth - 480)) + "px";
    let y = rect.bottom + window.scrollY + 10;
    bubble.style.top = y + "px";
    const h = bubble.offsetHeight || 80;
    if (rect.bottom + window.scrollY + h + 20 > window.scrollY + window.innerHeight) {
      y = Math.max(window.scrollY + 8, rect.top + window.scrollY - h - 10);
      bubble.style.top = y + "px";
    }
    bubble.querySelector(".close")?.addEventListener("click", hide);
  }

  function hide() {
    ensureHost().querySelector(".bubble")?.style.setProperty("display", "none");
  }

  document.addEventListener("mousedown", (e) => {
    const host = document.getElementById(HOST_ID);
    if (host && !host.contains(e.target)) hide();
  }, true);

  // -------------------------------------------------- 整页翻译
  const state = {
    translated: false,
    running: false,
    nodes: [],            // [{kind, node|el, attr, original, lead, trail}]
    seen: new WeakSet(),  // 已处理的文本节点 / 元素，用于跳过与增量的去重
    observer: null,
    autoNew: false,
  };

  function send(msg) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(msg, (resp) => {
        if (chrome.runtime.lastError) {
          return resolve({ ok: false, error: chrome.runtime.lastError.message });
        }
        resolve(resp || { ok: false, error: "no response" });
      });
    });
  }

  // 是否值得翻译：判定顺序很重要，先排除「不该翻」的，再判断像不像英文
  const HAN_RE = /[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]/g;
  const URL_SCHEMES = ["http://", "https://", "ftp://", "file://", "chrome://", "edge://",
    "about:", "view-source:", "ws://", "wss://"];
  const FILE_EXT = new Set(("py js mjs cjs ts tsx jsx json yaml yml toml ini cfg md txt html htm css " +
    "scss xml csv log bat ps1 sh exe dll zip tar gz png jpg jpeg gif svg ico mp3 mp4 wav pdf ipynb " +
    "lock whl pem crx db sqlite so dylib bin onnx").split(" "));

  // 技术标识用显式判断：拼接巨型正则在字符串转义上极易出错（实测踩到 URL/邮箱两个分支失效），
  // 而且没法单独测试；拆成小函数后每一类都能独立断言。
  function isTechnical(t) {
    const low = t.toLowerCase();

    // URL
    for (const s of URL_SCHEMES) if (low.startsWith(s)) return true;
    // 邮箱
    if (/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(t)) return true;
    // 版本号：3.2.1 / v1.3.2 / 2.0.0-beta.1
    if (/^v?\d+(\.\d+){1,3}([-+][\w.]+)?$/i.test(t)) return true;
    // 文件名：xxx.ext
    const dot = t.lastIndexOf(".");
    if (dot > 0 && dot < t.length - 1 && FILE_EXT.has(low.slice(dot + 1))) return true;
    // 路径：C:\... / /usr/... / ./x/
    if (/^[A-Za-z]:[\\/]/.test(t)) return true;
    if (/^[/~.][\w.\-]+[\\/]/.test(t)) return true;
    // 快捷键：Ctrl+Shift+T
    if (/^(Ctrl|Alt|Shift|Cmd|Meta|Win|Option)(\s*\+\s*[A-Za-z0-9]+)+$/.test(t)) return true;
    // 纯符号 / 纯数字
    if (/^[\W\d_]+$/.test(t)) return true;
    return false;
  }

  function looksEnglish(s) {
    const t = s.trim();
    if (t.length < 2) return false;

    // 1) 技术标识：不翻
    if (isTechnical(t)) return false;

    // 2) 含中文：不翻
    //    短串里只要有 2 个以上汉字（"登录 Sign in"、"已更新 Updated"）就当已中文化；
    //    长串按比例判断（英文句子里偶尔出现一个汉字不该整句跳过）。
    const han = (t.match(HAN_RE) || []).length;
    if (han) {
      if (t.length <= 24 && han >= 2) return false;
      if (han / t.length >= 0.25) return false;
    }

    // 3) 英文字母占比
    const letters = (t.match(/[A-Za-z]/g) || []).length;
    if (letters < 2) return false;
    const compact = t.replace(/\s+/g, "").length || 1;
    if (letters / compact < 0.3) return false;

    // 注意：这里**不要**再过滤"没有空格的孤立短串"。
    // 曾加过这条规则，导致 Home / Search / Settings 这类单字 UI 标签不再翻译
    // （端到端测试抓到：nav 项 Home 保持英文，而它本该由服务端词表译成"首页"）。
    // 单字专名（GitHub/Chrome/OpenVINO）与技术标识（SHA256/index.html）都由服务端
    // 的 PROTECTED 词表与 isTechnical 判定处理，客户端只负责粗筛。
    return true;
  }
  // 可访问的文档列表（含同源 iframe）
  function docs() {
    const list = [document];
    if (!isTop) return list;
    document.querySelectorAll("iframe,frame").forEach((f) => {
      try {
        const d = f.contentDocument;
        if (d && d.body) list.push(d);
      } catch (_) { /* 跨源，忽略 */ }
    });
    return list;
  }

  function collect() {
    const items = [];
    for (const doc of docs()) {
      const root = doc.body || doc.documentElement;
      if (!root) continue;

      // 1) 文本节点
      const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          const t = node.nodeValue;
          if (!t || t.trim().length < 2) return NodeFilter.FILTER_REJECT;
          if (!looksEnglish(t)) return NodeFilter.FILTER_REJECT;
          const p = node.parentElement;
          if (!p || p.closest(SKIP_SELECTOR)) return NodeFilter.FILTER_REJECT;
          // 时间/元信息类元素内的文字不翻（"Updated 2 hours ago"、"5 min read" 等）
          if (p.closest("time,var,output,progress,meter")) return NodeFilter.FILTER_REJECT;
          if (state.seen.has(node)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      while (walker.nextNode()) {
        const node = walker.currentNode;
        const raw = node.nodeValue;
        items.push({
          kind: "text", node, original: raw.trim(),
          lead: raw.match(/^\s*/)[0], trail: raw.match(/\s*$/)[0],
        });
        if (items.length >= MAX_ITEMS) return items;
      }

      // 2) 可见属性（placeholder / title / alt / aria-label）
      root.querySelectorAll("[placeholder],[title],[alt],[aria-label]").forEach((el) => {
        if (state.seen.has(el) || el.closest(SKIP_SELECTOR)) return;
        for (const attr of ATTRS) {
          const v = el.getAttribute(attr);
          if (v && v.trim().length >= 2 && looksEnglish(v)) {
            items.push({ kind: "attr", el, attr, original: v.trim(), lead: "", trail: "" });
          }
        }
        if (items.length >= MAX_ITEMS) return;
      });
      if (items.length >= MAX_ITEMS) return items;
    }
    return items;
  }

  async function run(onlyNew) {
    if (state.running) return { ok: false, error: "正在翻译中" };
    state.running = true;
    const t0 = performance.now();
    let applied = 0;
    try {
      const items = collect();
      if (!items.length) {
        if (!onlyNew) toast("没有找到需要翻译的英文内容");
        return { ok: true, count: 0 };
      }

      // 去重：相同字符串只翻一次（导航/页脚有大量重复）
      const unique = [], idxOf = new Map(), byIdx = new Map();
      for (const it of items) {
        if (!idxOf.has(it.original)) {
          idxOf.set(it.original, unique.length);
          unique.push(it.original);
        }
        it.idx = idxOf.get(it.original);
        if (!byIdx.has(it.idx)) byIdx.set(it.idx, []);
        byIdx.get(it.idx).push(it);
      }

      // 写回单条（先登记 seen，避免自己的改动触发增量重译）
      const applyItem = (it, t) => {
        if (it.kind === "text") {
          if (!it.node.isConnected) return false;
          state.seen.add(it.node);
          it.node.nodeValue = it.lead + t + it.trail;
        } else {
          if (!it.el.isConnected) return false;
          state.seen.add(it.el);
          it.el.setAttribute(it.attr, t);
        }
        state.nodes.push(it);
        return true;
      };

      // 分块请求 + 逐块写回：大页面也能立刻看到译文
      for (let i = 0; i < unique.length; i += CHUNK) {
        const resp = await send({ type: "TRANSLATE_BATCH", texts: unique.slice(i, i + CHUNK) });
        if (!resp.ok) throw new Error(resp.error || "请求失败");
        (resp.data.translations || []).forEach((t, k) => {
          if (!t) return;
          for (const it of (byIdx.get(i + k) || [])) {
            if (applyItem(it, t)) applied++;
          }
        });
        const done = Math.min(i + CHUNK, unique.length);
        toast(`翻译中 ${done}/${unique.length}（已写入 ${applied} 处）…`, true);
      }

      const ms = Math.round(performance.now() - t0);
      state.translated = true;
      startObserver();
      toast(`已翻译 ${applied} 处（去重后 ${unique.length} 段）· ${ms} ms`, false, true);
      return { ok: true, count: applied, ms };
    } catch (e) {
      // 部分成功也要允许还原
      if (applied > 0) { state.translated = true; startObserver(); }
      toast(`翻译中断（已写入 ${applied} 处）：${e.message || e}`, false, false, true);
      return { ok: false, error: String(e.message || e), count: applied };
    } finally {
      state.running = false;
    }
  }

  function restore() {
    stopObserver();
    let n = 0;
    for (const it of state.nodes) {
      try {
        if (it.kind === "text") {
          if (it.node.isConnected) { it.node.nodeValue = it.lead + it.original + it.trail; n++; }
        } else if (it.el.isConnected) {
          it.el.setAttribute(it.attr, it.original); n++;
        }
      } catch (_) { /* 忽略已失效节点 */ }
    }
    state.nodes = [];
    state.seen = new WeakSet();
    state.translated = false;
    toast(`已还原 ${n} 处原文`, false, true);
    return { ok: true, count: n };
  }

  // -------------------------------------------------- 增量翻译
  let timer = null;

  function startObserver() {
    if (state.observer || !state.autoNew) return;
    state.observer = new MutationObserver((muts) => {
      if (!state.translated || state.running) return;
      const relevant = muts.some((m) =>
        (m.type === "childList" && m.addedNodes.length) || m.type === "characterData");
      if (!relevant) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (state.translated && !state.running) run(true);
      }, 700);
    });
    state.observer.observe(document.body || document.documentElement, {
      childList: true, subtree: true, characterData: true,
    });
  }

  function stopObserver() {
    clearTimeout(timer);
    state.observer?.disconnect();
    state.observer = null;
  }

  // -------------------------------------------------- 消息入口
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    switch (msg?.type) {
      case "PING":
        sendResponse({ ok: true, translated: state.translated });
        return;

      case "SHOW_LOADING": {
        const info = getSelectionInfo();
        if (!info) { sendResponse({ ok: false }); return; }
        render(`<div class="meta"><span class="loading"></span> N-Translate 翻译中…</div>`, info.rect);
        chrome.runtime.sendMessage({ type: "TRANSLATE", text: info.text }, (resp) => {
          if (chrome.runtime.lastError || !resp?.ok) {
            render(`
              <div class="meta"><span class="dot"></span> 本地翻译服务未连接</div>
              <div class="dst">请先双击运行 E:\\ai\\dsh\\start_server.bat，再重试。<br>
              <span style="color:#8b949e;font-size:11px">${esc(resp?.error || chrome.runtime.lastError?.message || "")}</span></div>`,
              info.rect);
            return;
          }
          const d = resp.data;
          render(`
            <div class="src">${esc(info.text)}</div>
            <div class="dst">${esc(d.translation)}</div>
            <div class="meta"><span class="dot"></span> N-Translate · ${esc(d.execution_devices || d.device)}
              · ${esc(String(d.total_ms))} ms · ${esc(String(d.sentences?.length || 0))} 句
              <button class="close" title="关闭">✕</button></div>`,
            info.rect);
        });
        sendResponse({ ok: true });
        return;
      }

      case "TRANSLATE_PAGE":
        // 只由顶层框架执行（content script 在每个 frame 都会注入）
        if (!isTop) { sendResponse({ ok: true, skipped: true }); return; }
        chrome.storage.sync.get("autoNew").then(({ autoNew }) => {
          state.autoNew = !!autoNew;
          return run(false);
        }).then(sendResponse);
        return true;   // 异步响应

      case "RESTORE_PAGE":
        if (!isTop) { sendResponse({ ok: true, skipped: true }); return; }
        sendResponse(restore());
        return;

      case "PAGE_STATE":
        sendResponse({ ok: true, translated: state.translated, running: state.running });
        return;

      case "SET_AUTO_NEW":
        state.autoNew = !!msg.value;
        if (state.autoNew && state.translated) startObserver();
        if (!state.autoNew) stopObserver();
        sendResponse({ ok: true, autoNew: state.autoNew });
        return;
    }
  });
})();
