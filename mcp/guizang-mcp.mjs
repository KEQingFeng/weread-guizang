#!/usr/bin/env node
// 归藏 (weread-exporter) MCP stdio adapter — wraps the local HTTP API as MCP tools.
// Zero dependencies; speaks newline-delimited JSON-RPC 2.0 on stdio.
//
// 设计要点：取书是分钟级甚至小时级的长任务，MCP 调用不能阻塞等它跑完。
// 所以 book_fetch 立即返回，进度用 app_status / task_log 轮询。
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, openSync, readFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
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
/** 数据目录：与 platform_compat.data_dir 同一套规则（GUIZANG_DATA 优先，~ 要展开）。
 *  虚拟环境和端口回执文件都在这儿，不在源码目录 —— 装成 app 之后源码是只读的。 */
function dataDir() {
  const d = (process.env.GUIZANG_DATA || "").trim();
  if (!d) return REPO;
  return d.startsWith("~") ? join(homedir(), d.slice(1)) : d;
}
/** 该用哪个解释器起服务：虚拟环境优先，和 platform_compat.venv_python 一个口径。
 *  原来只认 <数据目录>/Scripts 与 <数据目录>/bin 这两个位置 —— 本项目建环境
 *  建的是 .venv/，两个都不存在，于是每次退回 PATH 上的 python3：那台机器上
 *  的 python3 没有本项目的依赖（feedparser、playwright…），服务进程起来就死，
 *  适配器只等来一句「启动超时」。 */
function venvPython(repo) {
  const override = process.env.GUIZANG_PYTHON;
  if (override && existsSync(override)) return override;
  const exe = process.platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"];
  const bases = [join(dataDir(), ".venv"), join(repo, ".venv"), join(dataDir(), "venv"),
                 join(repo, "venv"), dataDir(), repo];
  for (const base of bases) {
    const p = join(base, ...exe);
    if (existsSync(p)) return p;
  }
  return process.platform === "win32" ? "python" : "python3";
}
const PY = venvPython(REPO);
const PORT = Number(process.env.GUIZANG_PORT || 8770);
const RUNTIME = join(dataDir(), "runtime.json");

/** 服务实际绑在哪个端口。
 *  8770 被占时 ui_server 会自己往后挪最多 20 个，挪完只有它自己知道。
 *  GUIZANG_API 是显式指定，优先级最高；否则读回执文件，最后才用请它用的那个数。 */
function apiBase() {
  const explicit = (process.env.GUIZANG_API || "").trim();
  if (explicit) return explicit.replace(/\/+$/, "");
  try {
    const p = Number(JSON.parse(readFileSync(RUNTIME, "utf8")).port);
    if (p > 0 && p < 65536) return `http://127.0.0.1:${p}`;
  } catch { /* 还没写过：退回默认端口 */ }
  return `http://127.0.0.1:${PORT}`;
}

/* ── 与服务通信 ─────────────────────────── */
/** 参数对象允许拆成两段传：历史上有一批调用点写成 req(path, {method, body}, {ms})，
 *  而 req 只收两个参数 —— 第三段（超时）被静默丢掉，于是「给 3 分钟」的画板存盘
 *  其实只等到 15 秒就报超时。这里并起来，两处都算数。 */
async function req(path, opts = {}, more = {}) {
  const { method = "GET", body, ms = 15000 } = { ...opts, ...more };
  const r = await fetch(apiBase() + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(ms),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status} ${path}`);
  return path.startsWith("/api/") ? r.json() : r.text();
}

/** 端口上应答的那个后端是谁：{version, code, running}；没人答腔回 null。
 *  以前这里只有 alive()「有没有答腔」—— 旧后端照样答，于是被当成「服务在跑」，
 *  新功能一律 HTTP 404。认人要比认「在不在」更有用。 */
async function identity() {
  try {
    const s = await req("/api/state", { ms: 1500 });
    return {
      version: String(s.version || ""),
      code: String(s.code || ""),
      running: !!(s.task && s.task.running),
    };
  } catch {
    return null;
  }
}

/** 磁盘上这份 ui_server.py 的指纹：sha1(整个文件字节) 前 12 位。
 *  算法必须和 platform_compat.code_fingerprint 完全一致，两边各算各的比对才有意义 ——
 *  `node mcp/guizang-mcp.mjs --identity` 就是给门禁跑这一句的（真机套件里逐项对）。
 *  为什么非要指纹而不是版本号：源码直接跑的人改完代码常不跟着改版本号，
 *  旧进程和新代码写着同一个号，谁也不认谁是旧的。 */
function diskCode() {
  try {
    return createHash("sha1").update(readFileSync(join(REPO, "ui_server.py"))).digest("hex").slice(0, 12);
  } catch {
    return "";
  }
}

/** 一句人话：端口上那个后端比适配器旧时该干什么。
 *  原来这里只看「有没有答腔」，于是旧后端把 8770 供着、适配器认下它，
 *  新功能一律 HTTP 404 —— 用户报的「画板 / 导图 / flomo 导入后端没启动」是这一条。 */
function staleNote(who) {
  return `本机后端是 ${who.version || "更老的一版"}，比归藏界面和适配器（读的是磁盘上这份代码）旧：`
    + "在界面顶部点「换新后端」，或退出归藏再打开一次。"
    + (who.running ? "它手上还有任务在跑，等跑完再换，别打断。" : "");
}

/** 只把日志里的「出事那几行」挑出来。
 *  整段回传会把用户的路径（解释器、浏览器、书库三行启动横幅）带进对话里，
 *  那是个人信息；异常行本身才是要看的。主目录一律折成 ~。 */
function errorTail(file) {
  let text = "";
  try { text = readFileSync(file, "utf8"); } catch { return ""; }
  const home = homedir();
  const hits = text.split("\n")
    .filter(l => /Error|error|Traceback|Exception|No module named|找不到可用端口|Address already/.test(l))
    .map(l => (home ? l.split(home).join("~") : l).trim())
    .filter(Boolean);
  return hits.slice(-3).join(" ／ ");
}

let booting = null;
async function ensureServer() {
  const mine = diskCode();
  const who = await identity();
  // 答腔了、而且就是磁盘上这份代码：直接用。读不到自己那份（源码目录被挪了）也别多事。
  if (who && (!mine || who.code === mine)) return true;
  // 答腔了、但是旧的、还正在跑任务：绝不打断，把「等跑完再换新后端」说清。
  // 这里要是照样起第二个，就变成两个进程写同一个数据目录 —— 用户要的是单实例。
  if (who && who.running) throw new Error(staleNote(who));
  if (!booting) {
    booting = (async () => {
      if (!existsSync(join(REPO, "ui_server.py"))) {
        throw new Error(`找不到归藏项目（在 ${REPO} 没看到 ui_server.py）。请把本文件放在项目的 mcp/ 目录下，或用环境变量 GUIZANG_REPO 指定项目目录。`);
      }
      const log = join(tmpdir(), "guizang-mcp-server.log");
      const fd = openSync(log, "w");
      // who 非空 = 端口上挂着一个闲着的旧后端：带 --takeover，让新进程把那个端口接过来，
      // 而不是往后挪到 8771 —— 挪了以后浏览器和别的适配器还是只认 8770，又回到老问题。
      const args = ["ui_server.py", "--port", String(PORT)];
      if (who) args.push("--takeover");
      const child = spawn(PY, args, {
        cwd: REPO, detached: true, stdio: ["ignore", fd, fd], windowsHide: true,
      });
      child.unref();
      let died = null;
      child.on("exit", code => { died = code; });
      for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 500));
        const now = await identity();
        if (now && (!mine || now.code === mine)) return true;
        // 进程都退了还探什么：早点说清是哪儿不行，别让人干等 15 秒
        if (died !== null) {
          const why = errorTail(log);
          throw new Error("归藏服务起不来（解释器 " + PY + "）"
            + (why ? "：" + why : "，详见 " + log)
            + "。可在项目目录里跑一次 " + PY + " ui_server.py 看完整输出。");
        }
      }
      // 转一圈还没换成新代码：旧进程多半停不掉（或者被人手动守着），说清是哪一个
      const now = await identity();
      if (now) throw new Error(staleNote(now));
      throw new Error("归藏服务启动超时。请在项目目录里手动运行：" + PY + " ui_server.py --port " + PORT);
    })().finally(() => { booting = null; });
  }
  return booting;
}

/** 服务多久之内算「刚确认活着」：一次工具调用常常连着打十几个请求，
 *  每个都先探一遍 /api/state 纯属白花时间为。 */
const ALIVE_MS = 3000;
let lastAliveAt = 0;

/** 所有工具走的正门：先确保服务在，再发请求。
 *  直接 req() 的那些年，服务没起时用户看到的是 "fetch failed" —— 那是 Node
 *  的连接错误，不是归藏说的话，谁都不知道该干什么。 */
async function api(path, opts = {}, more = {}) {
  if (Date.now() - lastAliveAt > ALIVE_MS) {
    await ensureServer();
    lastAliveAt = Date.now();
  }
  try {
    return await req(path, opts, more);
  } catch (e) {
    // 半路服务被关了（用户退了 app）：让它重来一次，只补一次，不循环自救。
    // 补的时候 ensureServer 可能抛出一句有内容的话（端口上是个闲着的旧后端、
    // 或者它手上还有任务在跑所以没敢动）。那句得原样传出去 —— 拿「服务没应答」
    // 盖住它，用户就会去重启一个本来就好的服务，真正的问题反而没人说。
    lastAliveAt = 0;
    const why = e instanceof Error ? e.message : String(e);
    let back = false;
    try {
      back = await ensureServer();
    } catch (re) {
      throw new Error(`${re instanceof Error ? re.message : String(re)}（本次请求 ${path}：${why}）`);
    }
    if (!back) {
      throw new Error(`归藏服务没应答（${apiBase()}）：${why}`);
    }
    return req(path, opts, more);
  }
}

async function getState() {
  return api("/api/state");
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

/** 视频时长的口语写法：秒 → 「1 分 20 秒」/「12 分」。 */
function hrs(sec) {
  const s = Math.round(Number(sec) || 0);
  if (!s) return "—";
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m} 分 ${r} 秒` : `${m} 分`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

/** 认链接属于哪个平台。只为在回复里点名来源，不参与任何解析决策。 */
function platformName(u) {
  const s = String(u || "").toLowerCase();
  if (s.includes("zhihu.com")) return "知乎";
  if (s.includes("xiaohongshu.com") || s.includes("xhslink.com")) return "小红书";
  if (/(^|\.)x\.com|twitter\.com/.test(s)) return "X（推特）";
  if (s.includes("mp.weixin.qq.com")) return "微信公众号";
  return "";
}

/** 读订阅：参数里空值一律不带上，免得把「没筛」和「筛空串」混成一样。 */
async function readFeed(params = {}) {
  await ensureServer();
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    qs.set(k, String(v));
  }
  const d = await api(`/api/feed?${qs.toString()}`, { ms: 60000 });
  if (!d.ok) throw new Error(d.msg || "读订阅出错");
  return d;
}

async function postFeed(body, { ms = 120000 } = {}) {
  await ensureServer();
  return api("/api/feed", { method: "POST", body, ms });
}

/** 书名 / 编号 → 本地书库里的一本书。
 *  画板、思维导图、视频转写这三条线全都按本地目录名走（后端 safe_book_dir 只认
 *  [A-Za-z0-9_-]+），所以这里必须先把书名换成 id，绝不能把书名原样发上去 ——
 *  发「《原则》」后端只会回「没找到这本书」，用户看到的是工具报错而不是拼写问题。 */
async function localBook(key) {
  const s = await getState();
  const b = pick(s.books || [], key);
  if (!b) {
    throw new Error(`本地书架上没有匹配「${String(key || "").trim() || "（没给书名）"}」的书。先用 shelf_list 看有哪些书。`);
  }
  return b;
}

/** 秒 → 字幕式时间码。给 agent 看的，用来知道这一句在视频的哪一秒。 */
function cue(sec) {
  const n = Number(sec);
  if (!Number.isFinite(n) || n < 0) return "--:--";
  const s = Math.floor(n), p = x => String(x).padStart(2, "0");
  return [p(Math.floor(s / 3600)), p(Math.floor(s % 3600 / 60)), p(s % 60)].join(":");
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1024 / 1024).toFixed(1)} MB`;
}

/** 读一份 JSON，但把「404 也带着人话」那种响应收下。
 *  req() 见到非 2xx 直接抛「HTTP 404 /api/board?…」，而后端在 404 的信封里写的
 *  是「没有这块画板」—— 那才是用户该看见的一句，别让状态码把它盖掉。 */
async function reqSoft(path, { ms = 30000 } = {}) {
  const r = await fetch(apiBase() + path, { signal: AbortSignal.timeout(ms) }).catch(() => null);
  if (!r) return { ok: false, msg: "本机服务没应答（归藏是不是没开着？）" };
  try {
    return await r.json();
  } catch {
    return { ok: false, msg: `服务回了个看不懂的响应（HTTP ${r.status}）` };
  }
}

function countNodes(nodes) {
  let n = 0;
  (function walk(list) {
    for (const x of list || []) { n++; walk(x.children); }
  })(nodes || []);
  return n;
}

/** 导图树 → 缩进大纲。一张图最多 400 个节点，全列也就几百行，但一次工具回复塞进
 *  上千行会把别的内容挤出上下文，所以留个天花板：超了只给前若干行并说清还剩多少。 */
function outlineTree(nodes, maxLines = 220) {
  const lines = [];
  let total = 0;
  (function walk(list, depth) {
    for (const x of list || []) {
      total++;
      if (lines.length >= maxLines) return;
      const t = String(x.text || "").trim() || "（空框）";
      lines.push(`${"  ".repeat(depth)}- ${t}${x.fold ? "（折着）" : ""}${x.note ? "　※有备注" : ""}`);
      walk(x.children, depth + 1);
    }
  })(nodes || [], 0);
  if (total > lines.length) lines.push(`……还有 ${total - lines.length} 个框没列出来（用 as=json 能拿到全部）。`);
  return lines;
}

const CUT_NAME = { tag: "按标签分叉", chapter: "按章节分叉", entry: "按笔记条目分叉" };

/** 只改几段的存法：把整本读回来、在内存里改、再整本发回去。
 *
 *  后端 save_transcript 的语义是「整本覆盖」—— 那对界面是对的（编辑器手上就拿着整本），
 *  对 agent 却是个坑：它手上只有自己改的那两句，照语义发回去，整本几千段就抹成两段了。
 *  所以这条工具把读-改-写放在这里做完，调用方只描述改动。
 *  对不上的 id 只报不猜：改错一段比一段都不改更糟。 */
async function patchTranscript(bookId, { edits, drop, add }, notes) {
  // 一屏 1000 段、最多 25 屏：后端的单页上限是 1200，而一本转写最多 2 万段 ——
  // 取这两个数的下界附近，保证「读不全就写坏」这件事在这里不可能发生。
  const PAGE = 1000, MAX_TRIPS = 25;
  const rows = [];
  for (let guard = 0; guard < MAX_TRIPS; guard++) {
    const qs = new URLSearchParams({ mode: "transcript", book: bookId,
      offset: String(rows.length), limit: String(PAGE) });
    const d = await api(`/api/video?${qs}`, { ms: 60000 });
    if (!d.ok) throw new Error(d.msg || "读不出整本转写，这次没动盘上那一份");
    const t = d.transcript || {};
    const page = t.segments || [];
    rows.push(...page);
    if (!t.has_more || !page.length) break;      // 空页再要一次还是空，原地打转没有意义
  }
  if (!rows.length) throw new Error("这本书一段转写都读不出来：先确认它有转写（video_books 里会写「没有转写文件」）");

  const killed = new Set((drop || []).map(x => String(x && x.id !== undefined && x.id !== null ? x.id : x)));
  let left = rows.filter(x => !killed.has(String(x.id)));
  const gone = rows.length - left.length;
  if (killed.size && !gone) notes.push("要删的那些 id 在这本里都没找到，一段也没删");

  const byId = new Map(left.map(x => [String(x.id), x]));
  const miss = [];
  let changed = 0;
  for (const e of (edits || [])) {
    const hit = e ? byId.get(String(e.id)) : null;
    if (!hit) { miss.push(String(e && e.id)); continue; }
    if (e.text !== undefined && e.text !== null) hit.text = String(e.text);
    if (e.speaker !== undefined && e.speaker !== null) hit.speaker = String(e.speaker);
    changed++;
  }
  for (const a of (add || [])) {
    if (!a || !String(a.text || "").trim()) continue;
    const seg = { id: "n" + Math.random().toString(36).slice(2, 9),
                  text: String(a.text), speaker: String(a.speaker || "").trim() };
    const st = Number(a.start), en = Number(a.end);
    if (Number.isFinite(st) && st >= 0) seg.start = st;
    if (Number.isFinite(en) && en >= 0) seg.end = en;
    const anchor = a.after ? left.findIndex(x => String(x.id) === String(a.after)) : -1;
    if (a.after && anchor < 0) miss.push(`插入锚点 ${String(a.after)}`);
    const at = anchor >= 0 ? anchor + 1 : left.length;
    left.splice(at, 0, seg);
    if (seg.start === undefined) {
      // 没给时间就地从邻居推一个（界面「拆段」用的也是这个算法）：空着的话这一句没有时间戳，
      // 导出字幕会被挤到 00:00，按时间筛段也筛不到它 —— 有错的数比没有数更难看。
      // 两头贴死（前一段的结束 == 后一段的开始，中间没有缝）时就落在边界那一秒上，
      // 不要 +1：加了就会越过右边那段，屏幕上会出现「插进来的那句排在后面」的顺序错乱。
      const pe = Number(left[at - 1] && left[at - 1].end);
      const ns = Number(left[at + 1] && left[at + 1].start);
      if (Number.isFinite(pe) && Number.isFinite(ns) && ns > pe) seg.start = Math.round((pe + ns) / 2);
      else if (Number.isFinite(pe)) seg.start = pe;
      else if (Number.isFinite(ns)) seg.start = ns;
      else notes.push(`新加的「${seg.text.slice(0, 12)}」没有时间戳（前后两段也没有）`);
    }
  }
  if (changed) notes.push(`改了 ${changed} 段`);
  if (gone) notes.push(`删了 ${gone} 段`);
  const added = (add || []).filter(a => a && String(a.text || "").trim()).length;
  if (added) notes.push(`加了 ${added} 段`);
  if (miss.length) notes.push(`这些 id 没找到，已跳过：${miss.slice(0, 8).join("、")}${miss.length > 8 ? ` 等 ${miss.length} 处` : ""}`);
  if (!left.length) {
    throw new Error("改完整本就一段不剩了，这种情况不写盘：把转写清空请重转一次，或在界面里逐段删。");
  }
  return left;
}

/* ── 工具实现 ───────────────────────────── */
let shelfCache = {at: 0, books: []};
async function shelfBooks() {
  await ensureServer();
  if (shelfCache.books.length && Date.now() - shelfCache.at < 300000) return shelfCache.books;
  try {
    const d = await api("/api/weread", { method: "POST", body: { api_name: "/shelf/sync", params: {} }, ms: 40000 });
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

/** 一条便签 → 给 Agent 读的那几行。
 *  原文里那些 `![](files/xxx.png)` 在界面上走 /api/flomo/att/<哈希名> 那个口子才打得开，
 *  Agent 那边没有这个口子 —— 抄给它只是一段读不了的路径，换成「几张图」这句话更有用。
 *  正文读 plain 不读 md：后端已经把「跟在 tags 里单列过的那几个 #标签」从 plain 里摘干净了
 *  （见 flomo_notes.public）。这里要是还去抓 md，Agent 看到的就是头一行 #读书、
 *  正文末尾又一个 #读书 —— 用户在屏幕上从来看不到第二遍，上一轮刚把这个毛病按掉。
 *  留着 md 兜底：万一连的是没升级的老服务，宁可多一排标签，也不能吐空正文。 */
function fmtFlomoMemo(m, full = false) {
  const atts = (m.atts || []).length;
  const tags = (m.tags || []).length ? "　" + m.tags.map(t => "#" + t).join(" ") : "";
  const head = `${m.date || "?"} ${(m.clock || "").slice(0, 5) || "--:--"} · ${m.words || 0} 字`
    + `${atts ? ` · ${atts} 张图` : ""}${tags}  [id: ${m.id}]`;
  const text = String(m.plain != null ? m.plain : (m.md || "")).replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/[ \t]{2,}/g, " ").trim();
  if (!text) return head + "\n  （这一条只有图，没有字）";
  if (!full && text.length > 400) {
    return head + "\n  " + text.slice(0, 400).replace(/\n/g, "\n  ")
      + `…（还差 ${text.length - 400} 字，要全文就带着这个 id 再调一次 flomo_notes）`;
  }
  return head + "\n  " + text.replace(/\n/g, "\n  ");
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
      const lg = await api("/api/log?tail=8").catch(() => ({ lines: [] }));
      const tail = fmtLog(lg.lines || []);
      if (tail) lines.push(`最近输出：\n${tail}`);
      lines.push("提示：取书是长任务，别在这里等，隔一会儿再查，或让用户看界面。");
    } else {
      lines.push(t.exit_code ? `当前任务：空闲（上次退出码 ${t.exit_code}）` : "当前任务：空闲");
    }
    if (s.feed) lines.push(`订阅：${s.feed.subs || 0} 个源，未读 ${s.feed.unread || 0} 条`);
    const v = s.video || {};
    if (v.available) {
      const a = v.available;
      const asr = (a.asr || {}).local ? "本地语音识别就绪" : ((a.asr || {}).cloud ? "只能走云端语音识别" : "没有可用语音识别");
      lines.push(`视频转笔记：${a.ytdlp ? "下载器就绪" : "缺下载器"} · ${a.ffmpeg ? "ffmpeg 就绪" : "缺 ffmpeg"} · ${asr} · 大模型${a.llm ? "已配置" : "未配置（可只转写）"}`);
    }
    lines.push(`存放位置：${s.out}`);
    return lines.join("\n");
  },

  async task_log({ lines: n = 40 } = {}) {
    await ensureServer();
    const k = Math.max(1, Math.min(Number(n) || 40, 400));
    const d = await api(`/api/log?tail=${k}`);
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
      const r = await fetch(`${apiBase()}/api/zip?book=${encodeURIComponent(b.id)}`, { signal: AbortSignal.timeout(120000) });
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
    const r = await api("/api/action", { method: "POST", body: { action: "export", book: v } });
    if (!r.ok) throw new Error(r.msg);
    const id = v.includes("weread.qq.com") ? v.replace(/\/+$/, "").split("/").pop() : v;
    return [`已开始取书（编号 ${id}）。`, "这是长任务，可能要几分钟到几十分钟，我不会在这里等。",
            "你可以：隔一会儿用 app_status 看进度，或用 task_log 看最近输出；用 task_stop 可以中止。"].join("\n");
  },

  async task_stop() {
    const r = await api("/api/action", { method: "POST", body: { action: "stop" } });
    return r.ok ? "已请求中止，已取回的章节会保留。" : r.msg;
  },

  async folder_create({ name }) {
    const r = await api("/api/action", { method: "POST", body: { action: "folder.new", name } });
    if (!r.ok) throw new Error(r.msg);
    return `文件夹「${String(name).trim()}」已建立。`;
  },

  async book_move({ book, folder }) {
    const s = await getState();
    const b = pick(s.books || [], book);
    if (!b) throw new Error(`书架上没有匹配「${book}」的书`);
    const fid = resolveFolder(s.folders || [], folder);
    const r = await api("/api/action", { method: "POST", body: { action: "book.move", book: b.id, folder: fid } });
    if (!r.ok) throw new Error(r.msg);
    const to = fid ? (s.folders.find(f => f.id === fid) || {}).name : "未归类";
    return `《${b.title}》已归入「${to}」。`;
  },

  async account_connect() {
    await ensureServer();
    const s = await api("/api/state");
    if (s.login && s.login.logged_in) return `已经连接着（${fmtWhen(s.login.checked_at)} 检查通过），不用重复扫码。`;
    const r = await api("/api/action", { method: "POST", body: { action: "login" } });
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
    const d = (await api(`/api/detail?book=${encodeURIComponent(store)}&reader=${encodeURIComponent(reader)}`)).data || {};
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
    const d = await api("/api/notes_index");
    const st = d.data || {};
    if (st.running) return `正在建立：${st.done || 0}/${st.total || "?"}，已收 ${st.count || 0} 条。稍后再查。`;
    if (!st.built_at) {
      await api("/api/notes_index", { method: "POST", body: { rebuild: false } });
      return "已开始建立笔记索引（把划线与想法读进本地）。这需要几分钟，完成后 notes_search / notes_random 才可用。";
    }
    return `索引就绪：${st.count} 条，建于 ${fmtWhen(st.built_at)}。`;
  },

  async notes_search({ q, limit }) {
    await ensureServer();
    const n = Math.max(1, Math.min(Number(limit) || 30, 100));
    const d = await api(`/api/notes_search?q=${encodeURIComponent(q || "")}&limit=${n}`);
    if (!d.total) {
      const st = (await api("/api/notes_index")).data || {};
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
    const d = await api(`/api/notes_random?n=${n}`);
    if (!d.data || !d.data.length) {
      const st = (await api("/api/notes_index")).data || {};
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
    const r = await api("/api/book_state", { method: "POST", body: { books: ids, state: state || "" } });
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
        const d = await api(`/api/notes_search?q=${encodeURIComponent(kw)}&limit=${n}`);
        out.push(`【我的划线】命中 ${d.total || 0} 条`);
        for (const x of (d.data || [])) out.push(`- [${x.kind}]《${x.title}》${x.text.slice(0, 80)}`);
        if (!d.total) {
          const st = (await api("/api/notes_index")).data || {};
          if (!st.count) out.push("  （还没建笔记索引，先调 notes_index）");
        }
      } catch { out.push("【我的划线】暂时读不到"); }
    }

    if (want === "all" || want === "store") {
      try {
        const d = await api("/api/weread", {
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
    const d = await api(`/api/apkg?book=${encodeURIComponent(store)}`
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
    const d = await api(`/api/zip-multi?books=${encodeURIComponent(ids.join(","))}&json=1`, { ms: 300000 });
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
    const r = await api("/api/action", { method: "POST", body: { action: "export", book: list[0] } });
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
    const r = await api("/api/delete_many", { method: "POST", body: { books: ids } });
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
      const d = await api("/api/weread", {
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

    const r = await api("/api/action", {
      method: "POST", ms: 130000, body: { action: "shelf.add", ids },
    });
    if (!r.ok) throw new Error(r.msg || "加书架没成功");
    const out = [r.msg || "已加入书架"];
    if (note) out.push(note);
    out.push("微信读书书架与「归藏本地书架」是两回事，取正文仍要用 book_fetch。");
    return out.join("\n");
  },

  /* ── 剪藏：知乎 / 小红书 / X / 普通网页 ─────────────────── */

  async clip_url({ url, mode = "save" }) {
    await ensureServer();
    const u = String(url || "").trim();
    if (!/^https?:\/\//i.test(u)) throw new Error("请给出以 http(s):// 开头的完整链接");
    const r = await api("/api/clip", { method: "POST", ms: 180000, body: { mode, url: u } });
    if (!r.ok) throw new Error(r.msg || "这篇剪不动");
    if (mode === "preview") {
      const p = r.preview || {};
      return [`《${p.title || "（无标题）"}》${p.author ? " · " + p.author : ""}`,
        `${p.site || ""} · ${fmtChars(p.words)} · ${p.url || u}`,
        "", "正文开头：", (p.head || "").slice(0, 400),
        "", "要收进书架的话，再调一次 clip_url 并把 mode 留空（默认 save）。"].join("\n");
    }
    const b = r.book || {};
    // 平台不同，能拿到的完整度不同：知乎/小红书常常要登录才给全文，这里如实说。
    const plat = platformName(u);
    const out = [`已剪进书架：《${b.title || ""}》${b.chars || b.words || 0} 字`];
    out.push(`本地 id：${b.id || ""}（可以用 book_files / book_detail 继续操作）`);
    if (plat) out.push(`来源：${plat}${(b.title || "").length <= 2 ? "（拿到的标题很短，多半只抓到登录墙，建议用 preview 先看一眼）" : ""}`);
    return out.join("\n");
  },

  /* ── RSS 订阅 ────────────────────────────────────────── */

  async feed_list() {
    const d = await readFeed({ mode: "list" });
    const subs = d.subs || [], sum = d.summary || {};
    if (!subs.length) return "还没有订阅任何源。给一个站点链接，用 feed_discover 找出它的 RSS 地址。";
    const out = [`共 ${subs.length} 个订阅源，未读 ${sum.unread || 0} 条`];
    for (const f of subs) {
      const bad = f.error ? `  · 最近抓取失败：${String(f.error).slice(0, 60)}` : "";
      out.push(`- ${f.title || f.url}${f.site ? " · " + f.site : ""}  [id: ${f.id}]`);
      out.push(`  未读 ${f.unread || 0} 条 · 最近抓取 ${f.last_fetched ? fmtWhen(f.last_fetched) : "—"}${bad}`);
    }
    out.push("", "看更新：feed_entries；把某条收进本地书架：feed_to_shelf。");
    return out.join("\n");
  },

  async feed_discover({ url }) {
    const r = await postFeed({ act: "discover", url: String(url || "").trim() });
    const cands = r.cands || [];
    if (!cands.length) {
      return [`在 ${url} 身上没找到能订阅的地址。`,
        "常见原因是：这个站点没有 RSS；或者页面是纯前端渲染、抓不到 <link rel=alternate>。",
        "可以试试该站的「/feed」「/rss」「/atom.xml」后缀，或换个站点。"].join("\n");
    }
    const out = [`找到 ${cands.length} 个可订阅地址：`];
    for (const c of cands) out.push(`- ${c.title || "（无标题）"}\n  ${c.url}`);
    out.push("", "挑一个，用 feed_add 订阅（url 传上面那条）。");
    return out.join("\n");
  },

  async feed_add({ url, folder }) {
    const r = await postFeed({ act: "add", url: String(url || "").trim(), folder: folder || "" });
    if (!r.ok) throw new Error(r.msg || "订阅没成");
    const f = r.feed || {};
    return [`已订阅：《${f.title || f.url}》`, `本地 id：${f.id || ""}`,
      "要立刻抓一批文章回来看，调 feed_refresh；看条目用 feed_entries。"].join("\n");
  },

  async feed_entries({ feed, unread = false, q, limit = 40 }) {
    const qs = new URLSearchParams({ mode: "entries", limit: String(Math.max(1, Math.min(Number(limit) || 40, 200))) });
    if (feed) qs.set("feed", String(feed));
    if (unread) qs.set("unread", "1");
    if (q) qs.set("q", String(q));
    const d = await readFeed(Object.fromEntries(qs));
    const rows = d.entries || [];
    if (!rows.length) return "没有符合条件的条目。可能还没抓过：先调 feed_refresh。";
    const out = [`${rows.length} 条${unread ? "（只看未读）" : ""}${q ? `（匹配「${q}」）` : ""}`];
    for (const e of rows) {
      out.push(`- [${e.read ? "已读" : "未读"}] ${e.title || "（无标题）"}`);
      out.push(`  ${e.feed_title || ""}${e.published ? " · " + e.published : ""}  [id: ${e.id}]`);
    }
    out.push("", "读全文：feed_entry；收进本地书架：feed_to_shelf（收完就能用阅读器读、做笔记、导出）。");
    return out.join("\n");
  },

  async feed_entry({ id }) {
    const eid = String(id || "").trim();
    if (!eid) throw new Error("请给出条目 id，用 feed_entries 可以拿到");
    const d = await readFeed({ mode: "entry", id: eid });
    const e = d.entry;
    if (!e) throw new Error(`没找到 id 为 ${eid} 的条目，先用 feed_entries 确认`);
    const body = (e.content_text || e.summary || "").trim();
    return [`《${e.title || "（无标题）"}》`,
      `${e.feed_title || ""}${e.author ? " · " + e.author : ""}${e.published ? " · " + e.published : ""}`,
      `${e.link || ""}`, "", body.slice(0, 6000),
      body.length > 6000 ? `\n……（正文共 ${body.length} 字，余下部分请打开本机链接看）` : ""].join("\n");
  },

  async feed_refresh({ feed, force = false }) {
    const r = await postFeed({ act: "refresh", id: feed || "", force: !!force },
      { ms: 180000 });
    if (!r.ok) throw new Error(r.msg || "刷新没成");
    const out = [r.msg || "已刷新"];
    if ((r.errors || []).length) out.push("有源没抓成，可能是站点临时抽风或需要代理，过会儿再试。");
    out.push("看新条目：feed_entries。");
    return out.join("\n");
  },

  async feed_to_shelf({ id, mark_read = true }) {
    const r = await postFeed({ act: "shelf", id: String(id || "") }, { ms: 180000 });
    if (!r.ok) throw new Error(r.msg || "收进书架没成");
    const b = r.book || {};
    const out = [`已收进本地书架：《${b.title || ""}》${b.chars ? " · " + fmtChars(b.chars) : ""}`];
    out.push(`本地 id：${b.id || ""} —— 现在可以像别的书一样读、做笔记、导出 EPUB/PDF。`);
    if (mark_read) await postFeed({ act: "read", ids: [String(id || "")], read: true }).catch(() => {});
    return out.join("\n");
  },

  async feed_remove({ feed }) {
    const r = await postFeed({ act: "remove", id: String(feed || "") });
    if (!r.ok) throw new Error(r.msg || "退订没成");
    return `${r.msg || "已退订"}。已经收进本地书架的文章不受影响。`;
  },

  /* ── 视频转笔记 ──────────────────────────────────────── */

  async video_capability() {
    await ensureServer();
    const d = await api("/api/video?mode=status");
    const a = d.available || {};
    const out = ["视频转笔记依赖四样东西，缺哪样都会如实告诉你：",
      `- 下载器 yt-dlp：${a.ytdlp ? "就绪" : "缺（设置里点「补齐组件」）"}`,
      `- ffmpeg（抽音轨/转码）：${a.ffmpeg ? "就绪" : "缺（点「装组件」让归藏自己下一份）"}`,
      `- 本地语音识别：${(a.asr || {}).local ? "就绪（" + (a.engines || []).join(" / ") + "）" : "没装 mlx-whisper / faster-whisper"}`,
      `- 云端语音识别：${(a.asr || {}).cloud ? "可用（需在设置里填 Key）" : "没配"}`,
      `- 大模型（写摘要/笔记/导图）：${a.llm ? "已配置" : "没配 —— 没配也能转写，只是没有 AI 摘要与导图"}`,
    ];
    return out.join("\n");
  },

  async video_plan({ url }) {
    const u = String(url || "").trim();
    if (!u) throw new Error("请给出视频链接");
    const d = await api(`/api/video?mode=plan&url=${encodeURIComponent(u)}`, { ms: 60000 });
    if (!d.ok) throw new Error(d.msg || "这个链接认不出来");
    const p = d.plan || {};
    const out = [`认出来了：《${p.title || ""}》`];
    if (p.site) out.push(`平台：${p.site}`);
    if (p.duration) out.push(`时长：${hrs(p.duration)}`);
    if (p.parts && p.parts > 1) out.push(`分 P：${p.parts} 个`);
    out.push("", "确认无误就调 video_to_shelf 开跑：它会下载音频 → 语音转文字 → 生成摘要/知识笔记/思维导图 → 存成本地书架里的一本书。");
    return out.join("\n");
  },

  async video_to_shelf({ url, engine, language }) {
    await ensureServer();
    const s = await getState();
    if (s.task && s.task.running) {
      return `现在有任务在跑（${s.task.kind}${s.task.book ? " · " + s.task.book : ""}）。`
        + "后端一次只跑一件，等它跑完或用 task_stop 中止后再来。";
    }
    const avail = (s.video && s.video.available) || (await api("/api/video?mode=status")).available || {};
    if (!avail.ytdlp) throw new Error("还没装下载器 yt-dlp，先在界面设置里点「补齐组件」");
    const body = { act: "start", url: String(url || "").trim() };
    if (engine) body.asr = { engine: String(engine) };
    if (language) body.language = String(language);
    const r = await api("/api/video", { method: "POST", body });
    if (!r.ok) throw new Error(r.msg || "这条视频没跑起来");
    const out = [r.msg || "已开始转笔记"];
    if (!avail.llm) out.push("提醒：现在没配大模型，这次只会存下转写文字，没有 AI 摘要和思维导图。");
    out.push("这是分钟级的长任务（要下音频、跑语音识别），我不在这里等。");
    out.push("隔一会儿用 app_status / task_log 看进度；跑完可以在书架里找到这本。");
    return out.join("\n");
  },

  /* ── 视频转写工作台（界面上那一屏的能力，这里给到 agent） ────────── */

  async video_books() {
    const d = await api("/api/video?mode=books", { ms: 60000 });
    const books = d.books || [];
    if (!books.length) return "本地还没有「视频转出来」的书。用 video_plan 认链接、video_to_shelf 开跑，跑完再来回来看。";
    const out = [`视频转出来的书 ${books.length} 本（最近动过的排前面）：`];
    for (const b of books) {
      out.push(`- 《${b.title}》  [id: ${b.id}]`);
      const l1 = [];
      if (b.site) l1.push(b.site);
      if (b.duration) l1.push(hrs(b.duration));
      l1.push(`${b.segments || 0} 段`);
      l1.push(`${b.chapters || 0} 节`);
      if (b.engine) l1.push(b.engine);
      if (b.words) l1.push(fmtChars(b.words));
      out.push(`  ${l1.join(" · ")}`);
      const l2 = [b.edited ? "转写改过" : "转写没改过",
        (b.exports || []).length ? `导出过 ${(b.exports || []).length} 份` : "还没导出",
        b.has_json ? "有带时间戳的转写" : (b.has_txt ? "只有纯文本那份" : "没有转写文件")];
      if (b.ai_error) l2.push(`AI 那步没成（${String(b.ai_error).slice(0, 40)}）`);
      out.push(`  ${l2.join(" · ")}${b.updated_at ? " · " + fmtWhen(b.updated_at) : ""}`);
    }
    out.push("", "改转写：video_transcript 读、video_transcript_save 存；存完要 video_rebuild，章节正文才跟着新转写走。");
    return out.join("\n");
  },

  async video_transcript({ book, q, from, to, offset = 0, limit = 60 }) {
    const b = await localBook(book);
    const qs = new URLSearchParams({
      mode: "transcript", book: b.id,
      offset: String(Math.max(0, Math.floor(Number(offset) || 0))),
      limit: String(Math.max(1, Math.min(Math.floor(Number(limit) || 60), 240))),
    });
    if (q) qs.set("q", String(q));
    if (from) qs.set("from", String(from));
    if (to) qs.set("to", String(to));
    const d = await api(`/api/video?${qs}`, { ms: 60000 });
    if (!d.ok) throw new Error(d.msg || "这本的转写没读到");
    const t = d.transcript || {};
    const rows = t.segments || [];
    const head = [`《${t.title || b.title}》的转写`,
      `整本 ${t.total || 0} 段${(t.matched || 0) !== (t.total || 0) ? `，筛出 ${t.matched} 段` : ""}`
      + `；这次从第 ${(t.offset || 0) + 1} 段起给了 ${rows.length} 段`,
      `${t.source === "json" ? "带时间戳的那份（transcript.json）" : "纯文本那份（transcript.txt，没有时间戳）"}`
      + `${t.engine ? ` · 引擎 ${t.engine}` : ""}${t.language ? ` / ${t.language}` : ""}`
      + `${t.duration ? ` · 全片 ${hrs(t.duration)}` : ""}${t.edited ? " · 有人改过" : ""}`];
    if (!rows.length) {
      head.push("", "这一屏一段都没有。可能是筛得太狠（q / from / to），也可能这本书确实没有转写。");
      return head.join("\n");
    }
    head.push("");
    for (const s of rows) {
      head.push(`- [${cue(s.start)}]${s.speaker ? ` ${s.speaker}：` : " "}${String(s.text || "")}  [id: ${s.id}]`);
    }
    if (t.has_more) head.push("", `后面还有：再调一次 video_transcript，offset 传 ${(t.offset || 0) + rows.length}。`);
    head.push("", "要改就调 video_transcript_save：只传改的那几段的 id 和新文字，不必把整本抄一遍。");
    return head.join("\n");
  },

  async video_transcript_save({ book, segments, edits, drop, add }) {
    const b = await localBook(book);
    const patch = [edits, drop, add].some(x => Array.isArray(x) && x.length);
    const whole = Array.isArray(segments) && segments.length > 0;
    if (patch && whole) throw new Error("segments（整本覆盖）与 edits/drop/add（只改几段）只能选一样：两个都给就说不清哪份算数。");
    if (!patch && !whole) throw new Error("没给要存的内容：用 edits/drop/add 说清改了哪几段，或用 segments 整本覆盖（空列表一律不接，防的就是把整本抹掉）。");
    const notes = [];
    const rows = whole ? segments : await patchTranscript(b.id, { edits, drop, add }, notes);
    const r = await api("/api/video", { method: "POST",
      body: { act: "save_transcript", book: b.id, segments: rows } }, { ms: 90000 });
    if (!r.ok) throw new Error(r.msg || "转写没存进去");
    const out = [r.msg || "转写已存好"];
    if (notes.length) out.push(`这次做了：${notes.join("；")}。`);
    out.push("存好的只是那一份带时间戳的转写，章节正文还是旧的 —— 正文要跟着改，接着调 video_rebuild。");
    return out.join("\n");
  },

  async video_rebuild({ book }) {
    const b = await localBook(book);
    const r = await api("/api/video", { method: "POST", body: { act: "rebuild", book: b.id } }, { ms: 180000 });
    if (!r.ok) throw new Error(r.msg || "章节没能按转写重建");
    const g = r.rebuilt || {};
    const out = [r.msg || "章节已重建"];
    if (g.chapters) out.push(`重铺出 ${g.chapters} 节；写之前旧的那批文件挪进了 ${g.backup || "书目录里的备份"}，不满意可以挪回来。`);
    out.push("你的划线和笔记条目一个字都不会改（它们存在 notes.json 里，重建只是拿它重导一份 notes.md）；");
    out.push("但笔记是按正文文字对位的，你改过的那些句子附近的定位可能挪 —— 这是改写内容的正常代价。");
    return out.join("\n");
  },

  async video_export({ book, fmt = "srt" }) {
    const b = await localBook(book);
    const f = String(fmt || "srt").trim().toLowerCase().replace(/^\./, "");
    const r = await api("/api/video", { method: "POST", body: { act: "export", book: b.id, fmt: f } }, { ms: 120000 });
    if (!r.ok) throw new Error(r.msg || "导出没成");
    const e = r.export || {};
    const out = [`导好了：${e.name || "（没拿到文件名）"} · ${fmtBytes(e.bytes)} · ${e.segments || 0} 段`];
    if (!e.timed && (f === "srt" || f === "vtt")) {
      out.push("提醒：这本的转写没有时间戳，字幕里每一条都会挤在 00:00 —— 要字幕得先重转一次，只想留文字就导 txt 或 md。");
    }
    // downloads 挂在 export 那份信封里（路由把整个结果塞进 "export"），不在最外层：
    // 读错了地方就会「明明导好了两份，回话却说一份都没有」。
    const dl = e.downloads || r.downloads || [];
    out.push(`文件落在这本书的 exports/ 里，目前共 ${dl.length} 份导出物`
      + `${dl.length ? `（${dl.slice(-4).map(x => x.name).join("、")}）` : ""}；界面上「导出」那一栏能直接下载。`);
    return out.join("\n");
  },

  /* ── 笔记思维导图 ─────────────────────────────────────────── */

  async map_show({ book, as = "text", form }) {
    const b = await localBook(book);
    const mode = String(as || "text").trim().toLowerCase();
    const qs = new URLSearchParams({ book: b.id });
    if (form) qs.set("form", String(form));
    if (mode === "md" || mode === "svg") qs.set("mode", mode);
    const d = await api(`/api/mindmap?${qs}`, { ms: 60000 });
    if (!d.ok) throw new Error(d.msg || "这张图没读出来");
    if (mode === "md") {
      const md = String(d.md || "").trim();
      return md ? md + "\n\n（这份 Markdown 可以直接贴进笔记；界面上「导图」那一栏导出时也用它。）"
               : "这张图还是空的，只导出一个标题。";
    }
    if (mode === "svg") {
      // 一大段 XML 贴回对话里对谁都没用：agent 看不见像素，用户也 copy 不动。
      // 只报大小和「去哪儿拿它」，真要图片请在界面上点「存为图片」落成文件。
      const len = String(d.svg || "").length;
      return [`导图画出来了：形态「${d.form || ""}」，${len} 个字符的 SVG。`,
        "这份是即时算的，没有落成文件；界面上「思维导图」那一栏点「存为图片」才会写到书目录里。",
        "要看结构请用 as=text（缩进大纲）或 as=json（含每个框的 id 与坐标，能原样拿去改）。"].join("\n");
    }
    const doc = d.doc || {};
    if (mode === "json") return JSON.stringify(doc, null, 2);
    const forms = d.forms || [];
    const nameOf = k => (forms.find(x => x.key === k) || {}).name || k;
    // 传了 form 时后端只按它临时排一遍，盘上存的那个形态没变 —— 这里若照 doc.form 报，
    // 用户点了「换辐射图看看」却被告知「形态：鱼骨图」，看起来像那个参数根本没生效。
    const asked = form ? String(form) : "";
    const shown = asked && asked !== doc.form
      ? `形态：${nameOf(asked)}（这只是临时排布，盘上存的还是「${nameOf(doc.form)}」；要固定下来用 map_save 传 form）`
      : `形态：${nameOf(doc.form)}`;
    const out = [doc.title && doc.title !== b.title
      ? `《${b.title}》的思维导图 · 图名「${doc.title}」`
      : `《${b.title}》的思维导图`,
      shown,
      `（可换：${forms.map(x => `${x.key}·${x.name}`).join(" / ") || "tree·括号图 / radial·辐射图 / fishbone·鱼骨图 / concept·概念图"}）`,
      `节点：${countNodes(doc.nodes)} 个${doc.dropped ? ` · 曾被裁掉 ${doc.dropped} 个（超出单图上限）` : ""}`];
    if (!doc.nodes || !doc.nodes.length) {
      out.push("", "这张图还是空的。用 map_from_notes 从这本书的笔记生成一棵树，或用 map_save 直接写入结构。");
      return out.join("\n");
    }
    out.push("", ...outlineTree(doc.nodes));
    out.push("", "换形态不改变内容，只改排布：map_show 传 form=radial / fishbone / concept 再看看。");
    return out.join("\n");
  },

  async map_from_notes({ book, cut = "tag" }) {
    const b = await localBook(book);
    const d = await api("/api/mindmap", { method: "POST",
      body: { act: "from_notes", book: b.id, cut: String(cut || "tag") } }, { ms: 90000 });
    if (!d.ok) throw new Error(d.msg || "笔记没能变成图");
    const doc = d.doc || {};
    const out = [d.has_existing
      ? `已经从笔记生成一张新图（${CUT_NAME[String(cut)] || "按标签分叉"}，${countNodes(doc.nodes)} 个节点）—— 还没存盘，这本书原来那张还在。`
      : `已经从这本书的笔记生成一张图（${CUT_NAME[String(cut)] || "按标签分叉"}，${countNodes(doc.nodes)} 个节点）—— 还没存盘。`];
    if (!countNodes(doc.nodes)) {
      out.push("生成出来是空的：这本书大概还没有笔记条目。先在阅读时记几条，或换一种切法。");
      return out.join("\n");
    }
    out.push("", ...outlineTree(doc.nodes));
    out.push("", d.has_existing
      ? "看满意了再存：map_save 传同样的书名 + 这份 nodes（一存就会盖掉原来那张）。"
      : "看满意了再存：map_save 传同样的书名 + 这份 nodes。");
    out.push("不满意就不存 —— 只是看一眼的话，盘上的东西一点没动。");
    return out.join("\n");
  },

  async map_save({ book, title, form, nodes }) {
    const b = await localBook(book);
    const doc = {};
    const given = Array.isArray(nodes) && nodes.length;
    if (given) {
      doc.nodes = nodes;
      doc.links = [];
      doc.title = String(title || b.title || "");
    } else {
      // 没给 nodes 就只改标题 / 形态，绝不顺手交一张空图出去：这个接口是整本覆盖，
      // 空 nodes 会把用户手点出来的那张清干净 —— 丢一次图，用户就再也不信这个工具了。
      const cur = await api(`/api/mindmap?${new URLSearchParams({ book: b.id })}`, { ms: 30000 });
      if (!cur.ok) throw new Error(cur.msg || "先读不到这本书现有的导图，这次没动盘");
      Object.assign(doc, cur.doc || {});
      if (title) doc.title = String(title);
    }
    if (form) doc.form = String(form);
    const r = await api("/api/mindmap", { method: "POST", body: { act: "save", book: b.id, doc } }, { ms: 90000 });
    if (!r.ok) throw new Error(r.msg || "这张图没存进去");
    const out = [r.msg || `导图已存好（${r.nodes || 0} 个节点）`];
    if (r.dropped) out.push(`注意：有 ${r.dropped} 个节点被裁掉了 —— 一张图的上限是 400 个框、8 层深。`);
    if (!given) out.push("这次只改了标题 / 形态，节点内容原样留着。");
    out.push(`存的位置：书目录里的 mindmap.json。界面上打开这本书的「思维导图」就是它，形态随时可换。`);
    return out.join("\n");
  },

  /* ── 画板（边看书边涂鸦，涂鸦当笔记存） ──────────────────────── */

  async board_list({ book }) {
    const b = await localBook(book);
    const d = await api(`/api/board?${new URLSearchParams({ book: b.id })}`, { ms: 30000 });
    if (!d.ok) throw new Error(d.msg || "这本书的画板没读到");
    const rows = d.boards || [];
    const head = [`《${b.title}》的画板：${d.count || rows.length} / ${d.max_boards || "?"} 块`,
      `文件都在：${d.dir || ""}`];
    if (!rows.length) {
      head.push("", "一块板都还没有。board_new 建一块（可选题名和纸面），或者让用户在阅读界面右栏「画板」上直接画。");
      return head.join("\n");
    }
    head.push("");
    for (const x of rows) {
      head.push(`- ${x.title || "（未命名）"}  [id: ${x.id}]`);
      const l = [`纸面 ${x.paper}`, fmtBytes(x.bytes)];
      l.push(x.has_png ? "有 PNG" : x.has_svg ? "有 SVG" : "还没导出过图");
      if (x.caption) l.push(`说明「${String(x.caption).slice(0, 30)}」`);
      head.push(`  ${l.join(" · ")}${x.updated ? " · " + x.updated : ""}`);
    }
    head.push("", "看板上画了什么：board_show；想把它并进笔记：board_show as=md。");
    return head.join("\n");
  },

  async board_show({ book, id, as = "info" }) {
    const b = await localBook(book);
    const bid = String(id || "").trim();
    if (!bid) throw new Error("请给出画板 id（board_list 里每块都写着）");
    const mode = String(as || "info").trim().toLowerCase();
    if (mode === "md") {
      const r = await api("/api/board", { method: "POST", body: { act: "md", book: b.id, id: bid } }, { ms: 60000 });
      if (!r.ok) throw new Error(r.msg || "这块板转不成笔记");
      return [String(r.md || "").trim(), "", r.has_image
        ? "（图导出过，笔记里那条链接点开就是它。）"
        : "（这块板还没导出过图片：笔记里那条图片链接会点不开。要让它有图，得在界面上打开这块板点「导出图片」—— 画布内容本身生成不出图。）"].join("\n");
    }
    const d = await reqSoft(`/api/board?${new URLSearchParams({ book: b.id, id: bid })}`);
    if (!d.ok || !d.board) {
      // 后端 404 信封里那句「没有这块画板」本身没错，但对 agent 不够用：它手上只有一个 id，
      // 分不清是这个 id 抄错了还是这本书根本没有板。把 id 原样报回去并指路 board_list。
      throw new Error(`这本书里没有 id 为 ${bid} 的画板${d.msg ? `（${d.msg}）` : ""} —— 先 board_list 看看这本书到底有哪些板。`);
    }
    const doc = d.board || {};
    if (mode === "canvas") return JSON.stringify(doc.canvas || { objects: [] }, null, 2);
    const objs = (doc.canvas && doc.canvas.objects) || [];
    const s = await getState();
    const out = [`画板「${doc.title || "（未命名）"}」  [id: ${doc.id}]`,
      `纸面：${doc.paper}${doc.caption ? ` · 说明「${doc.caption}」` : ""}`,
      `最后编辑：${doc.updated || "?"}${doc.created ? ` · 建于 ${doc.created}` : ""} · 画布 ${fmtBytes(doc.bytes)}`,
      `文件：${join(s.out || "", b.id, "boards", doc.id + ".json")}`];
    if (!objs.length) {
      out.push("", "画布是空的 —— 这块板建出来了还没画。界面上阅读右栏「画板」可以画；board_save 能改标题 / 说明 / 纸面。");
      return out.join("\n");
    }
    const byType = new Map();
    for (const o of objs) {
      const k = String(o.type || "未知");
      byType.set(k, (byType.get(k) || 0) + 1);
    }
    out.push("", `上面有 ${objs.length} 个对象：${[...byType].map(([k, v]) => `${k} ×${v}`).join("、")}`);
    const texts = objs.filter(o => typeof o.text === "string" && o.text.trim())
      .map(o => o.text.trim());
    if (texts.length) {
      out.push(`其中写了字的 ${texts.length} 处：`);
      for (const t of texts.slice(0, 12)) out.push(`- ${t.length > 60 ? t.slice(0, 60) + "…" : t}`);
      if (texts.length > 12) out.push(`……还有 ${texts.length - 12} 处（as=canvas 能拿到全部）。`);
    } else {
      out.push("这些对象里没有文字，全是笔画 / 形状 —— 想看清画的是什么，得在界面上打开这块板。");
    }
    out.push("", "要原样拿走画布（比如照着改一笔）用 as=canvas；要并进笔记用 as=md。");
    return out.join("\n");
  },

  async board_new({ book, title, paper }) {
    const b = await localBook(book);
    const list = await api(`/api/board?${new URLSearchParams({ book: b.id })}`, { ms: 30000 });
    if (!list.ok) throw new Error(list.msg || "这本书的画板目录没读到，先确认书名对不对");
    const papers = list.papers || [];
    const want = paper ? String(paper).trim().toLowerCase() : "";
    if (want && !papers.includes(want)) {
      throw new Error(`纸面只认 ${papers.join(" / ")}，「${paper}」不会用 —— 这次一块板也没建，免得留一块跟你想的不一样。`);
    }
    const created = await api("/api/board", { method: "POST",
      body: { act: "new", book: b.id, title: String(title || "") } }, { ms: 30000 });
    if (!created.ok) throw new Error(created.msg || "新建画板没成");
    const doc = created.board || {};
    if (want && want !== doc.paper) {
      doc.paper = want;
      const saved = await api("/api/board", { method: "POST", body: { act: "save", book: b.id, doc } }, { ms: 30000 });
      if (!saved.ok) return `画板建好了 [id: ${doc.id}]，但纸面没改成「${want}」：${saved.msg || "保存没成"}`;
    }
    return [`新建了一块画板：「${doc.title || "（未命名）"}」  [id: ${doc.id}]`,
      `纸面 ${doc.paper}，画布还是空的。`,
      "界面上阅读右栏「画板」选中它就能画；board_save 可以改标题 / 说明 / 纸面。"].join("\n");
  },

  async board_save({ book, id, title, caption, paper, canvas }) {
    const b = await localBook(book);
    const bid = String(id || "").trim();
    if (!bid) throw new Error("请给出画板 id（board_list 里每块都写着）");
    // 先读回现有那一份再动笔：存盘是整块覆盖，只传个标题就会把用户画的那一叠笔画抹平。
    const cur = await reqSoft(`/api/board?${new URLSearchParams({ book: b.id, id: bid })}`);
    if (!cur.ok || !cur.board) {
      throw new Error(`这本书里没有 id 为 ${bid} 的画板${cur.msg ? `（${cur.msg}）` : ""} —— 要新建请用 board_new，别在这里存一块不存在的。`);
    }
    const doc = { ...cur.board };
    if (title !== undefined) doc.title = String(title);
    if (caption !== undefined) doc.caption = String(caption);
    if (paper !== undefined) doc.paper = String(paper);
    let replaced = false;
    if (canvas !== undefined) {
      if (!canvas || typeof canvas !== "object" || Array.isArray(canvas)) {
        throw new Error("canvas 得是一个对象（fabric 那份画布 JSON）。不想动画面就别传这个参数。");
      }
      doc.canvas = canvas;
      replaced = true;
    }
    const r = await api("/api/board", { method: "POST", body: { act: "save", book: b.id, doc } }, { ms: 60000 });
    if (!r.ok) throw new Error(r.msg || "这块板没存进去");
    const out = [r.msg || "画板已存好", `画板 ${r.id} · 画布 ${fmtBytes(r.bytes)}`];
    const objs = (doc.canvas && doc.canvas.objects) || [];
    if (replaced) {
      out.push(`这次是整块画布覆盖，现在上面有 ${objs.length} 个对象。`);
      if (!objs.length) out.push("注意：传进来的画布是空的 —— 这块板上的东西这次没了。只想改标题 / 说明的话，别带 canvas。");
    } else {
      out.push(`画布原样没动（上面还是 ${objs.length} 个对象）—— 这次只改了标题 / 说明 / 纸面。`);
    }
    out.push("导出图（PNG/SVG）只能由界面那侧生成：board_show as=md 会告诉你图有没有备好。");
    return out.join("\n");
  },

  async board_delete({ book, id, purge = true }) {
    const b = await localBook(book);
    const bid = String(id || "").trim();
    if (!bid) throw new Error("请给出要删的画板 id（board_list 里每块都写着）");
    const r = await api("/api/board", { method: "POST",
      body: { act: "delete", book: b.id, id: bid, purge: !!purge } }, { ms: 30000 });
    if (!r.ok) throw new Error(r.msg || "这块板没删掉");
    const out = [r.msg || "画板已删掉", `这本书还剩 ${(r.boards || []).length} 块板。`];
    out.push(purge ? "画布 JSON 和它的 PNG / SVG 导出物一起清了。"
      : "只收了画布 JSON，导出图留在原处 —— 要连图一起清就再删一次带 purge=true。");
    out.push("删掉的画布找不回来（.prev 那份留档只保上一次保存的内容）。");
    return out.join("\n");
  },

  async flomo_notes({ id, tag, q, limit, offset, order }) {
    // id 那一支单独走：列表为了不让一次调用灌爆上下文，每条掐到 400 字，
    // 想要原文就按 id 再取一次 —— 这是「读得到全文」和「一次别吐十万字」的折中。
    if (id) {
      const d = await api(`/api/flomo/notes?mode=one&id=${encodeURIComponent(String(id).trim())}`);
      if (!d.ok) throw new Error(d.msg || "读这条便签失败了");
      if (!d.memo) return `没有 id 为「${id}」的那条便签（被忘掉过，或本来就没有）。用 tag / q 重新筛一次。`;
      return fmtFlomoMemo(d.memo, true);
    }
    const n = Math.max(1, Math.min(Number(limit) || 20, 200));
    const off = Math.max(0, Number(offset) || 0);
    const qs = [["mode", "list"], ["tag", String(tag || "")], ["q", String(q || "")],
                ["limit", String(n)], ["offset", String(off)],
                ["order", String(order || "desc") === "asc" ? "asc" : "desc"]]
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
    const d = await api(`/api/flomo/notes?${qs}`);
    if (!d.ok) throw new Error(d.msg || "读便签失败了");
    const st = d.stats || {};
    if (!st.memos) {
      return "本机还没有导入 flomo 便签。让用户在界面「便签」那一格的右上角菜单里导入 flomo 导出包（一个 zip），"
        + "导入后这里才有内容。";
    }
    const tagline = (d.tags || []).slice(0, 20).map(t => `${t.tag}(${t.n})`).join("、");
    const how = [];
    if (tag) how.push(`标签「${tag}」`);
    if (q) how.push(`含「${q}」`);
    const out = [`${how.length ? "按" + how.join("、") + "筛出" : "本机存着"} ${d.total} 条，`
                 + `这里给第 ${off + 1}-${off + (d.count || 0)} 条。`];
    if (!d.total) {
      out.push("", `这一格对不上任何一条。标签账上有：${tagline || "一个标签都没有"}。`);
      return out.join("\n");
    }
    out.push(`这批笔记的骨架：全程 ${st.memos} 条 · ${st.days} 天（${st.first} ~ ${st.last}）· `
             + `${fmtChars(st.words)} · ${st.images} 张图 · ${st.tags} 个标签。`);
    if (!tag && !q && tagline) out.push(`标签（点父标签会带上子标签）：${tagline}`);
    out.push("");
    for (const m of d.memos || []) out.push(fmtFlomoMemo(m), "");
    const left = d.total - off - (d.count || 0);
    if (left > 0) out.push(`还剩 ${left} 条，翻页用 offset=${off + (d.count || 0)}。`);
    out.push("想知道「这位用户是怎么记东西的」先调 flomo_portrait —— 那份画像不含原文，读它比读一百条便宜。");
    return out.join("\n").trim();
  },

  async flomo_portrait() {
    const d = await api("/api/flomo/notes?mode=portrait");
    if (!d.ok) throw new Error(d.msg || "生成记忆画像失败了");
    const p = d.portrait || {};
    if (!p.memos) return "本机还没有导入 flomo 便签，画像是空的。先让用户在界面「便签」那一格里导入导出包。";
    return [(d.md || "").trim(), "",
            `这一页存在：${d.file}`,
            `机器读的那份（同一份数据的 JSON）：${d.json}`,
            "",
            "口径：这份画像只由条数、字数、日期与标签算出，不含任何一条笔记原文。",
            "替这位用户做事之前先读它 —— 它决定该一次给多少条、按哪套标签去找、回复写多长。"].join("\n");
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
    description: "在本地书架上新建一个文件夹，用来给已取回的书归类（不动微信读书那边的书架）。名字重名时会告诉你已存在；建完用 book_move 把书移进去。",
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
  {
    name: "clip_url",
    description: "把一个网页链接剪藏成归藏本地书架里的一本书。知乎、小红书、X（推特）有专门的解析（尽量免登录，拿不到全文会如实说，不会把验证页当正文）；其它网页走通用正文提取。mode 传 preview 可先看标题/字数而不入架。",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string", description: "以 http(s):// 开头的完整链接" },
        mode: { type: "string", description: "save（默认，直接入库）/ preview（只看一眼）" },
      },
      required: ["url"],
      additionalProperties: false,
    },
  },
  {
    name: "feed_list",
    description: "列出全部 RSS 订阅源与各自的未读数、最近抓取时间。第一次用订阅功能时先调它看现状。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "feed_discover",
    description: "给一个网站或页面链接，找出它可以订阅的 RSS / Atom / JSON Feed 地址（返回候选列表，不自动订阅）。",
    inputSchema: {
      type: "object",
      properties: { url: { type: "string", description: "网站首页或某个页面的链接" } },
      required: ["url"],
      additionalProperties: false,
    },
  },
  {
    name: "feed_add",
    description: "订阅一个 RSS 源（写操作）。url 用 feed_discover 给出的地址，也可以直接给带 /feed 的地址。",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string", description: "订阅源地址" },
        folder: { type: "string", description: "可选：给这个源一个分组名" },
      },
      required: ["url"],
      additionalProperties: false,
    },
  },
  {
    name: "feed_entries",
    description: "列出订阅里的文章条目（最新在前），可按订阅源、未读、关键词过滤。拿到 id 后可读全文或收进书架。",
    inputSchema: {
      type: "object",
      properties: {
        feed: { type: "string", description: "只看某个订阅源，传它的 id；不传就是全部" },
        unread: { type: "boolean", description: "true 则只看未读" },
        q: { type: "string", description: "在标题与摘要里搜的关键词" },
        limit: { type: "number", description: "最多几条，默认 40，最多 200" },
      },
      additionalProperties: false,
    },
  },
  {
    name: "feed_entry",
    description: "读一条订阅文章的正文（本地已存的全文本，不需要联网）。id 从 feed_entries 取。",
    inputSchema: {
      type: "object",
      properties: { id: { type: "string", description: "条目 id" } },
      required: ["id"],
      additionalProperties: false,
    },
  },
  {
    name: "feed_refresh",
    description: "立刻去抓一遍订阅源，把新文章拉回来（写操作，会联网）。不传 feed 就刷新全部。",
    inputSchema: {
      type: "object",
      properties: {
        feed: { type: "string", description: "只刷某个源，传它的 id；不传就是全部" },
        force: { type: "boolean", description: "true 则忽略上次的缓存标记，硬抓一次" },
      },
      additionalProperties: false,
    },
  },
  {
    name: "feed_to_shelf",
    description: "把一条订阅文章收进归藏本地书架，成为一本可以阅读、做笔记、导出 EPUB/PDF 的书，并自动标为已读。",
    inputSchema: {
      type: "object",
      properties: { id: { type: "string", description: "条目 id（从 feed_entries 取）" } },
      required: ["id"],
      additionalProperties: false,
    },
  },
  {
    name: "feed_remove",
    description: "退订一个订阅源（写操作）。已经收进本地书架的文章不受影响。",
    inputSchema: {
      type: "object",
      properties: { feed: { type: "string", description: "订阅源 id" } },
      required: ["feed"],
      additionalProperties: false,
    },
  },
  {
    name: "video_capability",
    description: "查「视频转笔记」这条线现在能走到哪一步：下载器、ffmpeg、本地/云端语音识别、大模型各就绪没有。用户问「能不能转视频」时先调它。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "video_plan",
    description: "给一个视频链接（B 站 / YouTube 等），先认一下它是哪支视频：标题、时长、平台、分 P 数。确认后再开跑。",
    inputSchema: {
      type: "object",
      properties: { url: { type: "string", description: "视频链接" } },
      required: ["url"],
      additionalProperties: false,
    },
  },
  {
    name: "video_to_shelf",
    description: "把一支视频转成一本本地书：下载音频 → 语音转文字 →（配了大模型时）生成摘要、知识笔记与思维导图 → 存进本地书架，之后可阅读、做笔记、导出。这是分钟级长任务，本工具立即返回，进度用 app_status / task_log 查。",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string", description: "视频链接" },
        engine: { type: "string", description: "转写引擎：auto（默认）/ mlx / faster / cloud" },
        language: { type: "string", description: "语言代码，如 zh、en；留空自动" },
      },
      required: ["url"],
      additionalProperties: false,
    },
  },
  {
    name: "video_books",
    description: "列出本地所有「由视频转出来」的书，含每本的转写段数、节数、转写引擎、改过没有、导出过几份、有没有 AI 摘要。想改某支视频的转写，先用它拿到书的 id。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "video_transcript",
    description: "读一本视频书的转写（逐段、带时间与说话人）。可按关键词筛、按时间区间筛，分页读：返回每段都带 id，改完拿这个 id 去 video_transcript_save。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id（video_books 里有）" },
        q: { type: "string", description: "只看含这个词的段（也认说话人名）" },
        from: { type: "string", description: "起点时间，如 1:23 或 83（秒）" },
        to: { type: "string", description: "终点时间，同上" },
        offset: { type: "number", description: "从第几段开始，默认 0" },
        limit: { type: "number", description: "这次要几段，默认 60，最多 240（免得一次灌爆上下文）" },
      },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "video_transcript_save",
    description: "把改动写回一本书的转写。推荐用 edits/drop/add 只描述改动（工具自己读整本、在内存里改、再整本发回去，所以不会把你没看的段落弄丢）；segments 是整本覆盖的低级用法，两者不能同时给。存完记得 video_rebuild，章节正文才会跟着改。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        edits: {
          type: "array", description: "要改的那些段",
          items: {
            type: "object",
            properties: {
              id: { type: "string", description: "段 id（video_transcript 里每段都标着）" },
              text: { type: "string", description: "新的正文；不传就是不改正文" },
              speaker: { type: "string", description: "新的说话人；不传就是不改" },
            },
            required: ["id"],
          },
        },
        drop: { type: "array", items: { type: "string" }, description: "要整段删掉的段 id" },
        add: {
          type: "array", description: "要新加的段（比如把一句拆成两句）",
          items: {
            type: "object",
            properties: {
              after: { type: "string", description: "插在哪一段后面，传那段的 id；不传就接在末尾" },
              text: { type: "string", description: "这一段的正文" },
              speaker: { type: "string", description: "说话人，可留空" },
              start: { type: "number", description: "起始秒数；不给就从前后邻居推一个" },
              end: { type: "number", description: "结束秒数" },
            },
            required: ["text"],
          },
        },
        segments: {
          type: "array", description: "整本覆盖用的全量段落列表（谨慎：这份会替换掉整本转写）",
          items: { type: "object" },
        },
      },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "video_rebuild",
    description: "按现在这份转写重建一本视频书的章节正文与合并稿（旧的章节文件先挪进备份）。转写改过就必须走这一步，正文才会跟着变；划线与笔记条目不动。",
    inputSchema: {
      type: "object",
      properties: { book: { type: "string", description: "书名或 id" } },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "video_export",
    description: "把一本视频书的转写导出成字幕或文本文件，落在该书目录的 exports/ 里。格式认 srt / vtt / txt / md / json。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        fmt: { type: "string", description: "srt（默认）/ vtt / txt / md / json" },
      },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "map_show",
    description: "看一本书的思维导图。默认给缩进大纲（最省上下文）；as=md 要一份能贴进笔记的 Markdown，as=json 要含每个框 id 与坐标的完整信封（照着改就能存回去），as=svg 只报大小（大段 XML 贴回对话没意义）。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        as: { type: "string", description: "text（默认）/ md / json / svg" },
        form: { type: "string", description: "临时换一种排布看：tree / radial / fishbone / concept" },
      },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "map_from_notes",
    description: "把一本书的笔记（划线、想法、章节）自动组织成一棵导图树并展示出来 —— 只「给」不「存」，确认满意后再用 map_save 落盘，所以点错了也不会盖掉用户手画的图。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        cut: { type: "string", description: "怎么分叉：tag（按标签，默认）/ chapter（按章节）/ entry（按每条笔记）" },
      },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "map_save",
    description: "保存一本书的思维导图。给 nodes 就是整张图替换（nodes 是嵌套树，每框 {text, children:[...]}）；不给就只改标题 / 形态，现有节点原样留着。上限：400 个框、8 层深。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        title: { type: "string", description: "图的标题，默认用书名" },
        form: { type: "string", description: "形态：tree / radial / fishbone / concept" },
        nodes: {
          type: "array", description: "节点树；每个框是 {text, children:[...]}，也可以带 id / note / color",
          items: { type: "object" },
        },
      },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "board_list",
    description: "列出一本书名下的画板（涂鸦笔记）：每块的标题、id、纸面、大小、有没有导出过图片，以及画板文件所在目录。",
    inputSchema: {
      type: "object",
      properties: { book: { type: "string", description: "书名或 id" } },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "board_show",
    description: "看一块画板。默认给「上面有什么」的摘要（对象数量与其中的文字内容），as=md 给一份并进笔记用的 Markdown，as=canvas 给原始画布 JSON。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        id: { type: "string", description: "画板 id（board_list 里每块都标着）" },
        as: { type: "string", description: "info（默认）/ md / canvas" },
      },
      required: ["book", "id"],
      additionalProperties: false,
    },
  },
  {
    name: "board_new",
    description: "在某一本书名下新建一块空白画板。纸面可选 plain / grid / dots / lines / dark（以本机实际支持的清单为准，写错了会直接告诉你可选项，不会建出一块不对的板）。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        title: { type: "string", description: "这块板的名字" },
        paper: { type: "string", description: "纸面样式，可留空用默认" },
      },
      required: ["book"],
      additionalProperties: false,
    },
  },
  {
    name: "board_save",
    description: "改一块画板。只传 title / caption / paper 时画布原样不动（工具会先读回现有那块再改）；canvas 是整块画布覆盖，除非你确实要重写画面，否则别传它。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        id: { type: "string", description: "画板 id" },
        title: { type: "string", description: "新标题" },
        caption: { type: "string", description: "这块板的一句话说明（会跟着进笔记）" },
        paper: { type: "string", description: "纸面样式" },
        canvas: { type: "object", description: "整块画布 JSON（谨慎：这是覆盖）" },
      },
      required: ["book", "id"],
      additionalProperties: false,
    },
  },
  {
    name: "board_delete",
    description: "删掉一块画板（不可恢复）。purge=true（默认）连导出图一起清；false 只收画布、留着导出的 PNG/SVG。",
    inputSchema: {
      type: "object",
      properties: {
        book: { type: "string", description: "书名或 id" },
        id: { type: "string", description: "要删的画板 id" },
        purge: { type: "boolean", description: "要不要连导出图一起删，默认要" },
      },
      required: ["book", "id"],
      additionalProperties: false,
    },
  },
  {
    name: "flomo_notes",
    description: "读本机导入的 flomo 便签（只读，一个字都不改）。默认按时间倒序给最近 20 条，每条带 id、日期时间、字数、标签和正文。tag 按标签筛（点父标签会带上它的子标签），q 搜正文与标签，id 只取那一条并给完整正文（列表里超 400 字会掐尾）。",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string", description: "只要这一条，给全文；给了它就忽略 tag / q / limit" },
        tag: { type: "string", description: "按标签筛，如「读书」或「读书/神经科学」" },
        q: { type: "string", description: "关键字，搜正文和标签" },
        limit: { type: "number", description: "给几条，默认 20，最多 200" },
        offset: { type: "number", description: "跳过几条，用来翻页" },
        order: { type: "string", description: "desc（默认，新的在上）/ asc（旧的在上）" },
      },
      additionalProperties: false,
    },
  },
  {
    name: "flomo_portrait",
    description: "读「用户记忆画像」：从全部 flomo 便签算出体量与节奏、标签层级、长短分布、记笔记的高峰时段和已深加工的条数，写成半页中文，并给出它落在本机的位置。只含数字与标签，不含任何一条原文。替用户做与笔记有关的事之前先调它一次，比翻几十条便签便宜得多。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
];

/* ── 协议 ───────────────────────────────── */
function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

const serverInfo = { name: "guizang", version: "1.5.0" };

/** 自检：`node mcp/guizang-mcp.mjs --identity` 打一行 JSON 就退，不接 stdin、不起服务。
 *  门禁拿它跟 Python 那边的 code_fingerprint 对一遍 —— 两边各写各的哈希，
 *  一个字节不一样就会把「同一份代码」判成「旧后端」，那是最难查的一类毛病。 */
if (process.argv.slice(2).includes("--identity")) {
  const code = diskCode();
  const state = await identity();
  process.stdout.write(JSON.stringify({
    adapter: serverInfo.version,
    repo: REPO.replace(homedir(), "~"),
    port: PORT,
    disk_code: code,
    running_code: state?.code || "",
    running_version: state?.version || "",
    same: !code || !state?.code ? null : code === state.code,
  }) + "\n", () => process.exit(0));
}

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
