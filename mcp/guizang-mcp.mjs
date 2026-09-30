#!/usr/bin/env node
// 归藏 (weread-exporter) MCP stdio adapter — wraps the local HTTP API as MCP tools.
// Zero dependencies; speaks newline-delimited JSON-RPC 2.0 on stdio.
//
// 设计要点：取书是分钟级甚至小时级的长任务，MCP 调用不能阻塞等它跑完。
// 所以 book_fetch 立即返回，进度用 app_status / task_log 轮询。
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const HERE = dirname(fileURLToPath(import.meta.url));

/** 项目目录：优先用环境变量，其次认「本文件就在项目 mcp/ 下」，最后才退回常见位置 */
function findRepo() {
  if (process.env.GUIZANG_REPO) return process.env.GUIZANG_REPO;
  const cands = [resolve(HERE, ".."), resolve(HERE, "../.."), HERE,
                 join(homedir(), "weread-exporter")];
  for (const c of cands) {
    if (existsSync(join(c, "ui_server.py"))) return c;
  }
  return cands[0];
}
const REPO = findRepo();
/** 项目虚拟环境里的解释器：Windows 在 Scripts/python.exe，其它平台在 bin/python。
 *  原来只认 POSIX 布局，Windows 上会退回到一个不存在的 python3 —— 服务与任务都起不来。 */
function venvPython(repo) {
  const override = process.env.GUIZANG_PYTHON;
  if (override && existsSync(override)) return override;
  for (const parts of [["Scripts", "python.exe"], ["bin", "python"]]) {
    const p = join(repo, ...parts);
    if (existsSync(p)) return p;
  }
  return process.platform === "win32" ? "python" : "python3";
}
const PY = venvPython(REPO);
const PORT = Number(process.env.GUIZANG_PORT || 8770);
const API = process.env.GUIZANG_API || `http://127.0.0.1:${PORT}`;

/* ── 与服务通信 ─────────────────────────── */
async function req(path, { method = "GET", body, ms = 15000 } = {}) {
  const r = await fetch(API + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(ms),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status} ${path}`);
  return path.startsWith("/api/") ? r.json() : r.text();
}

async function alive() {
  try {
    await req("/api/state", { ms: 1500 });
    return true;
  } catch {
    return false;
  }
}

let booting = null;
async function ensureServer() {
  if (await alive()) return true;
  if (!booting) {
    booting = (async () => {
      if (!existsSync(join(REPO, "ui_server.py"))) {
        throw new Error(`找不到归藏项目（在 ${REPO} 没看到 ui_server.py）。请把本文件放在项目的 mcp/ 目录下，或用环境变量 GUIZANG_REPO 指定项目目录。`);
      }
      spawn(PY, ["ui_server.py", "--port", String(PORT)], {
        cwd: REPO, detached: true, stdio: "ignore", windowsHide: true,
      }).unref();
      for (let i = 0; i < 30; i++) {
        await new Promise(r => setTimeout(r, 500));
        if (await alive()) return true;
      }
      throw new Error("归藏服务启动超时。请在项目目录里手动运行：" + PY + " ui_server.py --port " + PORT);
    })().finally(() => { booting = null; });
  }
  return booting;
}

async function getState() {
  await ensureServer();
  return req("/api/state");
}

function fmtChars(n) {
  if (!n) return "—";
  return n >= 10000 ? (n / 10000).toFixed(1) + " 万字" : n + " 字";
}
function fmtWhen(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtLog(lines) {
  return lines.map(l => l.s.replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]|\uFE0F/gu, "").replace(/\s+$/, ""))
    .filter(s => s.trim()).join("\n");
}

/** 允许用书名（含片段）代替 id */
function pick(books, key) {
  if (!key) return null;
  const k = String(key).trim();
  return books.find(b => b.id === k)
    || books.find(b => (b.title || "").toLowerCase() === k.toLowerCase())
    || books.find(b => (b.title || "").includes(k))
    || books.find(b => k.includes(b.title || "\u0000"));
}

function resolveFolder(folders, key) {
  if (key == null) return "";
  const k = String(key).trim();
  if (!k || k === "未归类" || k === "none" || k === "-") return "";
  const f = folders.find(x => x.id === k) || folders.find(x => x.name === k)
    || folders.find(x => x.name.includes(k));
  if (!f) throw new Error(`没有名为「${key}」的文件夹。现有：${folders.map(x => x.name).join("、") || "（无）"}`);
  return f.id;
}

/* ── 工具实现 ───────────────────────────── */
let shelfCache = {at: 0, books: []};
async function shelfBooks() {
  await ensureServer();
  if (shelfCache.books.length && Date.now() - shelfCache.at < 300000) return shelfCache.books;
  try {
    const d = await req("/api/weread", { method: "POST", body: { api_name: "/shelf/sync", params: {} }, ms: 40000 });
    shelfCache = {at: Date.now(), books: (d.data && d.data.books) || []};
  } catch (e) {
    shelfCache = {at: Date.now(), books: []};
  }
  return shelfCache.books;
}

/** 阅读器 id 与书架 bookId 是两套命名空间；书架的 deepLink 里 v= 就是阅读器 id */
function storeIdFor(readerId) {
  for (const b of shelfCache.books) {
    const m = String(b.deepLink || "").match(/[?&]v=([0-9A-Za-z]+)/);
    if (m && m[1] === readerId) return b.bookId;
  }
  return "";
}

/** 把书名/编号解析成微信读书的 store bookId（划线、卡包、简介都按这个 id 走） */
async function resolveStore(key) {
  const k = String(key || "").trim();
  if (!k) throw new Error("请给出书名或编号");
  await shelfBooks();
  if (/^\d{4,}$/.test(k)) {
    const b = shelfCache.books.find(x => x.bookId === k);
    return { store: k, title: (b && b.title) || k };
  }
  const hit = shelfCache.books.find(b => (b.title || "").toLowerCase() === k.toLowerCase())
    || shelfCache.books.find(b => (b.title || "").includes(k))
    || shelfCache.books.find(b => k.includes(b.title || "\u0000"));
  if (hit) return { store: hit.bookId, title: hit.title };
  const s = await getState();
  const lb = pick(s.books || [], k);
  if (lb) {
    const st = storeIdFor(lb.id);
    if (st) return { store: st, title: lb.title };
  }
  throw new Error(`没在微信书架上找到「${k}」。先用 search_books 或 shelf_list 确认书名。`);
}

const tools = {
  async shelf_list() {
    const s = await getState();
    const books = s.books || [], folders = s.folders || [];
    if (!books.length) return "书架是空的。用 book_fetch 取第一本书。";
    const byFolder = new Map();
    for (const b of books) {
      const key = b.folder || "";
      if (!byFolder.has(key)) byFolder.set(key, []);
      byFolder.get(key).push(b);
    }
    const name = f => (folders.find(x => x.id === f) || {}).name || "未归类";
    const out = [`共 ${books.length} 本，${folders.length} 个文件夹`];
    for (const [f, list] of byFolder) {
      out.push(`\n【${name(f)}】${list.length} 本`);
      for (const b of list) {
        out.push(`- ${b.title}${b.author ? ` · ${b.author}` : ""}  [id: ${b.id}]`);
        out.push(`  ${b.chapters} 章 · ${b.images} 幅图 · ${fmtChars(b.chars)} · ${b.done ? "已收齐" : "未收齐"} · ${fmtWhen(b.updated_at)}`);
      }
    }
    return out.join("\n");
  },

  async app_status() {
    const s = await getState();
    const t = s.task || {};
    const lines = [];
    lines.push(`运行组件：${s.chromium ? "就绪" : "待修复"}`);
    lines.push(s.login
      ? `微信读书：${s.login.logged_in ? "已连接" : "未连接"}（${fmtWhen(s.login.checked_at)} 检查）`
      : "微信读书：未检测，可用 account_connect 唤起扫码");
    if (t.running) {
      const mins = t.started_at ? Math.round(Date.now() / 1000 - t.started_at) : 0;
      lines.push(`当前任务：进行中（${t.kind}${t.book ? " · " + t.book : ""}，已跑 ${Math.floor(mins / 60)} 分 ${mins % 60} 秒）`);
      const lg = await req("/api/log?tail=8").catch(() => ({ lines: [] }));
      const tail = fmtLog(lg.lines || []);
      if (tail) lines.push(`最近输出：\n${tail}`);
      lines.push("提示：取书是长任务，别在这里等，隔一会儿再查，或让用户看界面。");
    } else {
      lines.push(t.exit_code ? `当前任务：空闲（上次退出码 ${t.exit_code}）` : "当前任务：空闲");
    }
    lines.push(`存放位置：${s.out}`);
    return lines.join("\n");
  },

  async task_log({ lines: n = 40 } = {}) {
    await ensureServer();
    const k = Math.max(1, Math.min(Number(n) || 40, 400));
    const d = await req(`/api/log?tail=${k}`);
    const text = fmtLog(d.lines || []);
    return text || "（还没有输出）";
  },

  async book_files({ book }) {
    const s = await getState();
    const b = pick(s.books || [], book);
    if (!b) throw new Error(`书架上没有匹配「${book}」的书，先用 shelf_list 看有哪些`);
    const dir = join(s.out, b.id);
    const out = [`《${b.title}》`, `目录：${dir}`, `章节：${dir}/chapters（${b.chapters} 个 md）`,
                 `图片：${dir}/images（${b.images} 幅）`];
    const merged = join(s.out, `${b.title.replace(/[<>:"/\\|?*]/g, "_")}.md`);
    if (existsSync(merged)) out.push(`合并正文：${merged}`);
    try {
      const r = await fetch(`${API}/api/zip?book=${encodeURIComponent(b.id)}`, { signal: AbortSignal.timeout(120000) });
      if (r.ok) {
        await r.arrayBuffer();
        out.push(`打包 ZIP：${join(REPO, "cache/downloads", b.id + ".zip")}`);
      }
    } catch { out.push("打包 ZIP：暂时失败，可在界面点「完整包」"); }
    out.push("", "说明：合并正文里的图片是相对路径 images/，单拿 md 会断图；ZIP 里已把图片平铺在 images/ 下。");
    return out.join("\n");
  },

  async book_fetch({ book }) {
    await ensureServer();
    const v = String(book || "").trim();
    if (!v) throw new Error("请给出书的链接或编号");
    const r = await req("/api/action", { method: "POST", body: { action: "export", book: v } });
    if (!r.ok) throw new Error(r.msg);
    const id = v.includes("weread.qq.com") ? v.replace(/\/+$/, "").split("/").pop() : v;
    return [`已开始取书（编号 ${id}）。`, "这是长任务，可能要几分钟到几十分钟，我不会在这里等。",
            "你可以：隔一会儿用 app_status 看进度，或用 task_log 看最近输出；用 task_stop 可以中止。"].join("\n");
  },

  async task_stop() {
    const r = await req("/api/action", { method: "POST", body: { action: "stop" } });
    return r.ok ? "已请求中止，已取回的章节会保留。" : r.msg;
  },

  async folder_create({ name }) {
    const r = await req("/api/action", { method: "POST", body: { action: "folder.new", name } });
    if (!r.ok) throw new Error(r.msg);
    return `文件夹「${String(name).trim()}」已建立。`;
  },

  async book_move({ book, folder }) {
    const s = await getState();
    const b = pick(s.books || [], book);
    if (!b) throw new Error(`书架上没有匹配「${book}」的书`);
    const fid = resolveFolder(s.folders || [], folder);
    const r = await req("/api/action", { method: "POST", body: { action: "book.move", book: b.id, folder: fid } });
    if (!r.ok) throw new Error(r.msg);
    const to = fid ? (s.folders.find(f => f.id === fid) || {}).name : "未归类";
    return `《${b.title}》已归入「${to}」。`;
  },

  async account_connect() {
    await ensureServer();
    const s = await req("/api/state");
    if (s.login && s.login.logged_in) return `已经连接着（${fmtWhen(s.login.checked_at)} 检查通过），不用重复扫码。`;
    const r = await req("/api/action", { method: "POST", body: { action: "login" } });
    if (!r.ok) throw new Error(r.msg);
    return "已唤起确认窗口，请让用户在弹出的浏览器里用微信扫码（5 分钟内有效）。扫完用 app_status 复核。";
  },

  async book_detail({ book }) {
    const s = await getState();
    const b = pick(s.books || [], book);
    const reader = b ? b.id : String(book || "").trim();
    let store = /^\d{4,}$/.test(reader) ? reader : "";
    if (!store) {
      await shelfBooks();                       // 用书架把阅读器 id 映射成 store bookId
      store = storeIdFor(reader);
      if (!store) {
        const hit = shelfCache.books.find(x => (x.title || "").includes(String(book || "")));
        if (hit) store = hit.bookId;
      }
    }
    const d = (await req(`/api/detail?book=${encodeURIComponent(store)}&reader=${encodeURIComponent(reader)}`)).data || {};
    const info = d.info || {}, pg = (d.progress || {}).book || {}, nb = d.notes || {};
    const out = [`《${info.title || reader}》`];
    if (info.author) out.push(`作者：${info.author}`);
    if (info.publisher) out.push(`出版社：${info.publisher}`);
    if (info.category) out.push(`分类：${info.category}`);
    if (typeof pg.progress === "number") out.push(`微信读书进度：${pg.progress}%`);
    if (pg.updateTime) out.push(`最近阅读：${fmtWhen(pg.updateTime)}`);
    if (Object.keys(nb).length) {
      out.push(`划线 ${nb.noteCount || 0} · 想法 ${nb.reviewCount || 0} · 书签 ${nb.bookmarkCount || 0}`);
    }
    out.push(d.local ? `本地 Markdown：已取回 ${d.local.chapters} 章${d.local.done ? "（完整）" : ""}`
                     : "本地 Markdown：尚未取回");
    if (info.intro) out.push("", "简介：" + String(info.intro).slice(0, 400));
    return out.join("\n");
  },

  async notes_index() {
    await ensureServer();
    const d = await req("/api/notes_index");
    const st = d.data || {};
    if (st.running) return `正在建立：${st.done || 0}/${st.total || "?"}，已收 ${st.count || 0} 条。稍后再查。`;
    if (!st.built_at) {
      await req("/api/notes_index", { method: "POST", body: { rebuild: false } });
      return "已开始建立笔记索引（把划线与想法读进本地）。这需要几分钟，完成后 notes_search / notes_random 才可用。";
    }
    return `索引就绪：${st.count} 条，建于 ${fmtWhen(st.built_at)}。`;
  },

  async notes_search({ q, limit }) {
    await ensureServer();
    const n = Math.max(1, Math.min(Number(limit) || 30, 100));
    const d = await req(`/api/notes_search?q=${encodeURIComponent(q || "")}&limit=${n}`);
    if (!d.total) {
      const st = (await req("/api/notes_index")).data || {};
      if (!st.count) return "还没有笔记索引。先调 notes_index 建一次（约几分钟），之后就能搜划线与想法。";
      return `没有匹配「${q}」的划线。`;
    }
    const lines = [`共命中 ${d.total} 条，显示前 ${d.data.length} 条：`];
    for (const x of d.data) {
      lines.push(`\n[${x.kind}] 《${x.title}》`);
      lines.push(x.text.slice(0, 220));
    }
    return lines.join("\n");
  },

  async notes_random({ count }) {
    await ensureServer();
    const n = Math.max(1, Math.min(Number(count) || 5, 20));
    const d = await req(`/api/notes_random?n=${n}`);
    if (!d.data || !d.data.length) {
      const st = (await req("/api/notes_index")).data || {};
      return st.count ? "暂时抽不到卡片。" : "还没有笔记索引。先调 notes_index 建一次。";
    }
    return d.data.map(x => `[${x.kind}] 《${x.title}》\n${x.text.slice(0, 260)}`).join("\n\n———\n\n");
  },

  async book_mark({ books, state }) {
    await ensureServer();
    const s = await getState();
    const ids = [];
    for (const key of (books || [])) {
      const b = pick(s.books || [], key);
      if (b) ids.push(b.id);
    }
    if (!ids.length) throw new Error("没认出这些书，先用 shelf_list 看有哪些");
    const r = await req("/api/book_state", { method: "POST", body: { books: ids, state: state || "" } });
    if (!r.ok) throw new Error(r.msg);
    return `已为 ${ids.length} 本标记「${state || "清除标记"}」。`;
  },
  async search_books({ q, scope, limit }) {
    await ensureServer();
    const kw = String(q || "").trim();
    if (!kw) throw new Error("请给出要搜的关键词");
    const want = String(scope || "all").toLowerCase();
    const n = Math.max(1, Math.min(Number(limit) || 8, 30));
    const low = kw.toLowerCase();
    const out = [];

    if (want === "all" || want === "local") {
      const s = await getState();
      const mine = (s.books || [])
        .filter(b => ((b.title || "") + (b.author || "")).toLowerCase().includes(low));
      out.push(`【本地已取回】${mine.length} 本`);
      for (const b of mine.slice(0, n)) {
        out.push(`- ${b.title} · ${b.chapters} 章 · ${fmtChars(b.chars)}${b.done ? " · 已收齐" : ""}  [id: ${b.id}]`);
      }
    }

    if (want === "all" || want === "notes") {
      try {
        const d = await req(`/api/notes_search?q=${encodeURIComponent(kw)}&limit=${n}`);
        out.push(`【我的划线】命中 ${d.total || 0} 条`);
        for (const x of (d.data || [])) out.push(`- [${x.kind}]《${x.title}》${x.text.slice(0, 80)}`);
        if (!d.total) {
          const st = (await req("/api/notes_index")).data || {};
          if (!st.count) out.push("  （还没建笔记索引，先调 notes_index）");
        }
      } catch { out.push("【我的划线】暂时读不到"); }
    }

    if (want === "all" || want === "store") {
      try {
        const d = await req("/api/weread", {
          method: "POST", ms: 40000,
          body: { api_name: "/store/search", params: { keyword: kw, scope: 10 } },
        });
        const seen = new Set(), books = [];
        for (const g of ((d.data || {}).results || [])) {
          for (const x of (g.books || [])) {
            const bi = x.bookInfo || x.book || x;
            if (!bi.bookId || seen.has(bi.bookId)) continue;
            seen.add(bi.bookId); books.push(bi);
          }
        }
        out.push(`【书城里搜】${books.length} 本`);
        for (const b of books.slice(0, n)) {
          out.push(`- ${b.title}${b.author ? " · " + b.author : ""}  [bookId: ${b.bookId}]`);
        }
      } catch (e) {
        out.push(`【书城里搜】不可用（${String(e.message || e).slice(0, 60)}）——需要在界面设置里填微信读书接口 Key`);
      }
    }
    return out.join("\n");
  },

  async apkg_export({ book }) {
    await ensureServer();
    const { store, title } = await resolveStore(book);
    const d = await req(`/api/apkg?book=${encodeURIComponent(store)}`
      + `&title=${encodeURIComponent(title)}&json=1`, { ms: 180000 });
    if (!d.ok) throw new Error(d.msg || "导出失败");
    return [`《${title}》的划线卡包已生成：${d.count} 张`, `文件：${d.path}`,
            "在 Anki 里「文件 → 导入」选这个 .apkg 即可；正面是划线原文，背面是出处与想法。"].join("\n");
  },

  async zip_export({ books }) {
    await ensureServer();
    const s = await getState();
    const ids = [], missing = [];
    for (const key of (books || [])) {
      const b = pick(s.books || [], key);
      if (b) ids.push(b.id); else missing.push(String(key));
    }
    if (!ids.length) throw new Error("这些书都还没取回本地，没有可打包的内容。先用 book_fetch 取书。");
    const d = await req(`/api/zip-multi?books=${encodeURIComponent(ids.join(","))}&json=1`, { ms: 300000 });
    if (!d.ok) throw new Error(d.msg || "打包失败");
    const out = [`已打包 ${d.count} 本（含逐章 Markdown 与图片）：${d.path}`];
    if (missing.length) out.push(`没认出的：${missing.join("、")}（要先取回本地才能打包）`);
    return out.join("\n");
  },

  async batch_fetch({ books }) {
    await ensureServer();
    const list = (books || []).map(x => String(x || "").trim()).filter(Boolean);
    if (!list.length) throw new Error("请给出要取的书（链接或编号），可以是多本");
    const s = await getState();
    if (s.task && s.task.running) {
      return `现在有任务在跑（${s.task.kind} · ${s.task.book}）。后端一次只取一本，等它跑完或用 task_stop 中止后再来。`;
    }
    const r = await req("/api/action", { method: "POST", body: { action: "export", book: list[0] } });
    if (!r.ok) throw new Error(r.msg);
    const out = [`已开始取第 1 本：${list[0]}`];
    if (list.length > 1) {
      out.push(`后面还有 ${list.length - 1} 本排队：${list.slice(1).join("、")}`);
      out.push("后端一次只跑一本，等 app_status 显示空闲后再调一次 batch_fetch 取下一本；"
        + "或者在界面里用「批量 → 抓取」，它会自动一本接一本。");
    } else {
      out.push("这是长任务，我不在这里等；用 app_status / task_log 看进度。");
    }
    return out.join("\n");
  },

  async cache_delete({ books, confirm }) {
    await ensureServer();
    const s = await getState();
    const ids = [], titles = [], missing = [];
    for (const key of (books || [])) {
      const b = pick(s.books || [], key);
      if (b) { ids.push(b.id); titles.push(b.title); } else missing.push(String(key));
    }
    if (!ids.length) return "这些书本地都没有缓存，不用删。";
    if (confirm !== true) {
      return [`将要删除 ${ids.length} 本的本地 Markdown 与图片：${titles.join("、")}`,
        "微信读书里的划线笔记不受影响，之后可以重新取。",
        "确认要删的话，再调一次并传 confirm: true。"].join("\n");
    }
    const r = await req("/api/delete_many", { method: "POST", body: { books: ids } });
    if (!r.ok) throw new Error(r.msg);
    const out = [`${r.msg || "已删除"}：${titles.join("、")}`];
    if (missing.length) out.push(`没认出的：${missing.join("、")}`);
    return out.join("\n");
  },

  async shelf_add({ book }) {
    await ensureServer();
    const k = String(book || "").trim();
    if (!k) throw new Error("请给出书名或书城书籍编号");
    shelfCache.at = 0;                      // 加完书架就变了，别让缓存骗人

    let ids = [], note = "";
    if (/^\d{5,20}$/.test(k)) {
      ids = [k];
    } else {
      const d = await req("/api/weread", {
        method: "POST", ms: 40000,
        body: { api_name: "/store/search", params: { keyword: k, scope: 10 } },
      });
      const seen = new Set(), hits = [];
      for (const g of ((d.data || {}).results || [])) {
        for (const x of (g.books || [])) {
          const bi = x.bookInfo || x.book || x;
          if (!bi.bookId || seen.has(bi.bookId)) continue;
          seen.add(bi.bookId); hits.push(bi);
        }
      }
      const low = k.toLowerCase();
      const exact = hits.filter(b => (b.title || "").toLowerCase() === low);
      const loose = hits.filter(b => (b.title || "").toLowerCase().includes(low));
      const pick = (exact.length ? exact : loose)[0];
      if (!pick) throw new Error(`书城里没搜到「${k}」，先调 search_books 确认书名或改用编号`);
      ids = [String(pick.bookId)];
      if (!exact.length) note = `按书名匹配到《${pick.title}》`;
    }

    const r = await req("/api/action", {
      method: "POST", ms: 130000, body: { action: "shelf.add", ids },
    });
    if (!r.ok) throw new Error(r.msg || "加书架没成功");
    const out = [r.msg || "已加入书架"];
    if (note) out.push(note);
    out.push("微信读书书架与「归藏本地书架」是两回事，取正文仍要用 book_fetch。");
    return out.join("\n");
  },
};

const TOOL_DEFS = [
  {
    name: "shelf_list",
    description: "列出归藏书架上的全部书与文件夹，含每本的章节数/图片数/字数/是否收齐。开始任何操作前先用它拿到书的 id。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "app_status",
    description: "查归藏的整体状态：运行组件是否就绪、微信读书是否已连接、当前是否有取书任务在跑及已跑多久，并附最近几行输出。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "task_log",
    description: "读取取书任务的最近输出。适合在任务进行中看它跑到哪一章了。",
    inputSchema: {
      type: "object",
      properties: { lines: { type: "number", description: "要多少行，默认 40，最多 400" } },
      additionalProperties: false,
    },
  },
  {
    name: "book_files",
    description: "拿到某本书已导出产物的本机绝对路径（目录、章节 md、图片、合并正文、打包 ZIP），供后续读取或引用。可用书名片段或 id 指定。",
    inputSchema: {
      type: "object",
      properties: { book: { type: "string", description: "书名（可片段）或 id" } },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "book_fetch",
    description: "开始把一本微信读书的书取回本地（导出为逐章 Markdown + 图片）。这是分钟到小时级的长任务，本工具立即返回不等待，进度用 app_status 或 task_log 查。参数可以是阅读器链接或书籍编号。",
    inputSchema: {
      type: "object",
      properties: { book: { type: "string", description: "阅读器链接 https://weread.qq.com/web/reader/xxxx 或其末尾编号" } },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "task_stop",
    description: "中止当前正在进行的取书任务。已取回的章节会保留，之后可以再发起同一本书续取。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "folder_create",
    description: "在书架上新建一个文件夹，用于给书归类。",
    inputSchema: {
      type: "object",
      properties: { name: { type: "string", description: "文件夹名" } },
      required: ["name"],
      additionalProperties: false,
    },
  },
  {
    name: "book_move",
    description: "把一本书移进某个文件夹（按名字），folder 传「未归类」或空则移出文件夹。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名（可片段）或 id" },
        folder: { type: "string", description: "目标文件夹名；留空或传「未归类」表示移出" },
      },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "account_connect",
    description: "唤起微信读书扫码登录窗口（需真人在弹出的浏览器里扫码）。已连接时不会重复弹窗。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "book_detail",
    description: "看一本书的完整信息：作者、出版社、分类、简介、微信读书阅读进度与最近阅读时间、划线与想法数量、本地是否已取回 Markdown。书名可片段。",
    inputSchema: {
      type: "object",
      properties: { book: { type: "string", description: "书名（可片段）或 bookId" } },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "notes_index",
    description: "查笔记索引状态；若还没建过就顺手开始建立。索引把你的划线与想法读进本地（约几分钟），是 notes_search 与 notes_random 的前置。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "notes_search",
    description: "在自己的划线与想法正文里搜关键词，返回命中条目（含出自哪本书）。需要先建过笔记索引。",
    inputSchema: {
      type: "object",
      properties: {
        q: { type: "string", description: "关键词" },
        limit: { type: "number", description: "最多返回多少条，默认 30" },
      },
      required: ["q"],
      additionalProperties: false,
    },
  },
  {
    name: "notes_random",
    description: "从全部划线与想法里随机抽几条（随机漫步），适合做回顾或给我举例。需要先建过笔记索引。",
    inputSchema: {
      type: "object",
      properties: { count: { type: "number", description: "抽几条，默认 5，最多 20" } },
      additionalProperties: false,
    },
  },
  {
    name: "book_mark",
    description: "给一本或多本书打本地标记（待读 / 在读 / 已读），传空字符串清除标记。只影响本机的归类，不动微信读书。",
    inputSchema: {
      type: "object",
      properties: {
        books: { type: "array", items: { type: "string" }, description: "书名（可片段）或 id 的数组" },
        state: { type: "string", description: "待读 / 在读 / 已读；留空表示清除" },
      },
      required: ["books"],
      additionalProperties: false,
    },
  },  {
    name: "search_books",
    description: "一处搜三个范围：本地已取回的书、我的划线内容、微信读书书城。scope 可传 all（默认）/ local / notes / store。找书先用它；微信自己的书架用 shelf_list 看。",
    inputSchema: {
      type: "object",
      properties: {
        q: { type: "string", description: "关键词（书名、作者、分类或划线里的词）" },
        scope: { type: "string", description: "all / local / notes / store，默认 all" },
        limit: { type: "number", description: "每个范围最多列几条，默认 8，最多 30" },
      },
      required: ["q"],
      additionalProperties: false,
    },
  },
  {
    name: "shelf_add",
    description: "把书城里搜到的书加进用户自己的微信读书书架（写操作，需要本机已登录微信读书）。book 传书名或书城编号；加完用户就能在微信读书 App 里读到它。返回的编号可直接用于 book_fetch。",
    inputSchema: {
      type: "object",
      properties: { book: { type: "string", description: "书名（可片段）或书城书籍编号" } },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "apkg_export",
    description: "把一本书的划线与想法导出成 Anki 卡包（.apkg），返回本机文件路径与卡片数。需要先建过笔记索引。",
    inputSchema: {
      type: "object",
      properties: { book: { type: "string", description: "书名（可片段）或 bookId" } },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "zip_export",
    description: "批量把已取回本地的书打包成一个 ZIP（每本一个目录，含逐章 Markdown 与图片），返回文件路径。",
    inputSchema: {
      type: "object",
      properties: { books: { type: "array", items: { type: "string" }, description: "书名（可片段）或 id 的数组" } },
      required: ["books"],
      additionalProperties: false,
    },
  },
  {
    name: "batch_fetch",
    description: "批量取书：给出多本，先开始第一本并列出其余排队项。后端一次只跑一本，跑完再调一次取下一本；进度用 app_status / task_log 看。",
    inputSchema: {
      type: "object",
      properties: { books: { type: "array", items: { type: "string" }, description: "阅读器链接或书籍编号的数组" } },
      required: ["books"],
      additionalProperties: false,
    },
  },
  {
    name: "cache_delete",
    description: "删除本地缓存（已取回的 Markdown 与图片）。不传 confirm 时只列出将要删的书让你复核；确认后再传 confirm: true 执行。微信读书里的划线笔记不受影响。",
    inputSchema: {
      type: "object",
      properties: {
        books: { type: "array", items: { type: "string" }, description: "书名（可片段）或 id 的数组" },
        confirm: { type: "boolean", description: "true 才真正删除" },
      },
      required: ["books"],
      additionalProperties: false,
    },
  },
];

/* ── 协议 ───────────────────────────────── */
function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

const serverInfo = { name: "guizang", version: "1.1.0" };

const rl = createInterface({ input: process.stdin });
rl.on("line", async line => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch {
    return;
  }
  const { id, method, params } = msg;

  if (method === "initialize") {
    send({
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: params?.protocolVersion || "2024-11-05",
        capabilities: { tools: {} },
        serverInfo,
      },
    });
  } else if (method === "tools/list") {
    send({ jsonrpc: "2.0", id, result: { tools: TOOL_DEFS } });
  } else if (method === "tools/call") {
    const { name, arguments: input } = params || {};
    const fn = tools[name];
    if (!fn) {
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: `unknown tool: ${name}` }], isError: true } });
      return;
    }
    try {
      const text = await fn(input || {});
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: String(text) }] } });
    } catch (err) {
      send({
        jsonrpc: "2.0",
        id,
        result: { content: [{ type: "text", text: `归藏：${err?.message || err}` }], isError: true },
      });
    }
  } else if (method === "ping") {
    send({ jsonrpc: "2.0", id, result: {} });
  }
  // notifications (initialized, cancelled...) need no reply
});
rl.on("close", () => process.exit(0));
