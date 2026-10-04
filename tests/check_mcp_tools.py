# -*- coding: utf-8 -*-
"""MCP 适配器这一层的门禁：工具清单不漂移，工具在真机上按人话办事。

为什么要单开一份：MCP 是「让 agent 替用户点归藏」的那只手。它和界面走同一批后端接口，
却没有界面那些护栏 —— 保存按钮背后有「先读回整本再写」，工具这边只有 agent 发上来的一段
JSON。这轮功能长出画板、导图、转写工作台三条线，工具从 32 涨到 45，最容易坏的两件事：

1. **漂移**。写了处理函数忘了登记定义（agent 根本看不见这个工具），或者定义了却没实现
   （agent 一点就「不认识的工具」）。两边都在同一个文件里隔了几百行，肉眼扫不出漏了哪个。
2. **覆盖语义伤人**。后端 save_transcript / 导图 save / 画板 save 全是「整本（整块）覆盖」。
   用户在界面上改一句无损，因为前端手里握着一整本；agent 手上只有自己那一句，照字面发上去
   就把几千段转写、几万笔涂鸦抹成一句。工具层必须自己读回来再写 —— 这条只有去磁盘上
   数「没被点名的那些还在不在」才验得出来。

跑法（run_all.sh 会代劳起服务与铺书架）：
    .venv/bin/python tests/seed.py
    .venv/bin/python tests/check_mcp_tools.py http://127.0.0.1:8899
本机没有 node 时整套明确 SKIP 并退 0：适配器是 .mjs，起不来就等于这门禁没跑，
不能把它装成绿的 —— 报「通过」比报「没测」更坏。
"""
import atexit
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from seed import _write_video_book, book_dir  # noqa: E402

BASE = selftest.need_base(1)
ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_FILE = ROOT / "mcp" / "guizang-mcp.mjs"
FAIL = []
PASSED = 0
names = []

# 一本一次性视频书：转写、画板、导图三条线全在它名下跑（后端 safe_book_dir 只认
# [A-Za-z0-9_-]+，所以名字用 ASCII；界面上的书名是中文，正好把「书名 → id」那一步
# 也验一遍）。跑完连目录一起删，不给别的套件留野书。
MK = "video_MCPTOOLS"
MK_TITLE = "工具链取证这本"
BOOKS = pathlib.Path(book_dir(""))
_write_video_book(BOOKS, MK, MK_TITLE, "讲师丙")
atexit.register(lambda: shutil.rmtree(BOOKS / MK, ignore_errors=True))


def http_get(path):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


NODE = shutil.which("node") or shutil.which("node.exe")
if not NODE:
    print("SKIP  本机 PATH 里没有 node，MCP 适配器起不起来，这套件没测任何东西。")
    print("      （静态检查也一起跳过：清单要读实时 tools/list，光看源码挡不住漏定义。）")
    sys.exit(0)
if not MCP_FILE.is_file():
    print("FAIL  找不到适配器：" + str(MCP_FILE))
    sys.exit(1)


class MCP:
    """最小 JSON-RPC over stdio 客户端：逐行 newline-delimited，只认自己发出去的 id。

    读线程 + 队列，不用 select：Windows 上 select 只认 socket，管道会当场报错，
    而这个仓库是要在 Windows 上跑的。真挂住时超时报错，别让门禁把整个 run_all 拖死。
    """

    def __init__(self):
        self.env = dict(os.environ,
                        GUIZANG_API=BASE,
                        GUIZANG_PORT=str(urllib.parse.urlparse(BASE).port or ""))
        self.p = subprocess.Popen([NODE, str(MCP_FILE)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=self.env, cwd=str(ROOT))
        self.q = queue.Queue()
        self.err = []
        for stream, sink in ((self.p.stdout, self.q.put), (self.p.stderr, self.err.append)):
            t = threading.Thread(target=self._pump, args=(stream, sink), daemon=True)
            t.start()
        self.n = 0
        self._handshake()

    @staticmethod
    def _pump(stream, sink):
        for line in stream:
            sink(line)
        sink(None)          # 进程退出：让读侧知道「后面不会有了」

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def _reply(self, rid, timeout=200):
        while True:
            try:
                line = self.q.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError("适配器 %d 秒没回 id=%s 这一条" % (timeout, rid))
            if line is None:
                raise RuntimeError("适配器提前退出了：%s" % "".join(self.err[-6:]))
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue          # 杂输出（console.log）不当协议内容
            if msg.get("id") == rid:
                return msg

    def _rpc(self, method, params, timeout=200):
        self.n += 1
        rid = self.n
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        msg = self._reply(rid, timeout)
        if "error" in msg:
            raise RuntimeError("%s 回了协议错误：%s" % (method, msg["error"]))
        return msg["result"]

    def _handshake(self):
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "guizang-selftest", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def tools(self):
        return self._rpc("tools/list", {})["tools"]

    def call(self, name, args=None):
        """回 (是不是错误, 那段人话)。工具层把异常都折成 isError + 中文文案，
        这正是给 agent 看的形态，所以断言要拿文本来断，不是拿状态码。"""
        r = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        text = "\n".join(c.get("text", "") for c in r.get("content") or [])
        return bool(r.get("isError")), text

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        self.p.kill()


def j(path):
    """磁盘上那份 JSON，读不到回 None —— 断言要区分「没有」和「是空的」。"""
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def http_post(path, body):
    """直接敲后端：套件要摆出「板已经导出过图」这种状态，只能走那条写口。
    工具那侧不生成图，这是产品口径（图由画布渲染出来，只有界面那台 fabric 干得了）。"""
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


# ── 1. 源码两半对齐：处理函数 ↔ 工具定义 ───────────────────────────
src = MCP_FILE.read_text(encoding="utf-8")
handlers = re.findall(r"^  (?:async )?([A-Za-z_]\w*)\(",
                      src.split("const tools = {", 1)[1].split("\n};", 1)[0], re.M)
declared = re.findall(r'^\s{4}name: "([^"]+)"',
                      src.split("const TOOL_DEFS = [", 1)[1], re.M)
chk("源码：tools 里每个处理函数都在 TOOL_DEFS 登记了（漏了 agent 就看不见它）",
    set(handlers) == set(declared),
    {"只有实现": sorted(set(handlers) - set(declared)),
     "只有定义": sorted(set(declared) - set(handlers))})
chk("源码：定义不重复登记（重复会撞出两个同名工具，agent 点到哪个说不清）",
    len(declared) == len(set(declared)), [d for d in declared if declared.count(d) > 1])

m = MCP()
try:
    tools = m.tools()
    names = [t["name"] for t in tools]
    chk("握手：tools/list 数出来的工具与 TOOL_DEFS 一致",
        len(names) == len(declared) == len(handlers), (len(names), len(declared), len(handlers)))
    chk("清单：至少 47 个工具（便签那两条加完的新下限；再往下就是工具被删了却没改这里）",
        len(names) >= 47, len(names))
    chk("清单：名字唯一", len(names) == len(set(names)))
    missing = sorted(set(handlers) - set(names))
    chk("清单：agent 能看见全部实现", not missing, missing)

    shape_bad = []
    for t in tools:
        sch = t.get("inputSchema") or {}
        props = sch.get("properties") or {}
        req = sch.get("required") or []
        why = []
        if len(str(t.get("description") or "")) < 20:
            why.append("说明太短")
        if sch.get("type") != "object":
            why.append("inputSchema.type 不是 object")
        if not isinstance(props, dict):
            why.append("properties 不是对象")
        for k, v in props.items():
            if not isinstance(v, dict) or "type" not in v:
                why.append("参数 %s 没写类型" % k)
        for k in req:
            if k not in props:
                why.append("required 里的 %s 不在 properties" % k)
        if not why and sch.get("additionalProperties") is not False:
            why.append("没关 additionalProperties")
        if why:
            shape_bad.append((t["name"], why))
    chk("清单：每个工具都写清了怎么调（说明、参数类型、必填项都在 properties 里）",
        not shape_bad, shape_bad[:4])

    # 这轮新长的 13 个：一条一条点名，别让「加了但没登记」这种事故蒙过上面那条集合比较。
    brand_new = ["video_books", "video_transcript", "video_transcript_save", "video_rebuild",
                 "video_export", "map_show", "map_from_notes", "map_save",
                 "board_list", "board_show", "board_new", "board_save", "board_delete"]
    chk("清单：视频 / 导图 / 画板三条线的工具都在（%d 个）" % len(brand_new),
        all(x in names for x in brand_new), [x for x in brand_new if x not in names])

    # ── 2. 书名解析：安全目录名这件事由工具兜住 ──────────────────
    bad, txt = m.call("board_list", {"book": "《压根没这本书》"})
    chk("书名对不上时回人话并指路 shelf_list，不甩 HTTP 码",
        bad and "shelf_list" in txt and "HTTP" not in txt, txt[:200])

    # ── 3. 转写：只报改动，不能把整本抹掉 ───────────────────────
    bad, txt = m.call("video_books")
    chk("video_books：把这本视频书列出来了", not bad and MK_TITLE in txt, txt[:200])

    bad, txt = m.call("video_transcript", {"book": MK})
    ids = re.findall(r"\[id: (s\d{5})\]", txt)
    chk("video_transcript：每段都带 id 和时间码（agent 靠这两个东西说话）",
        not bad and len(ids) == 8 and re.search(r"\[\d\d:\d\d:\d\d\]", txt)
        and txt.count("[id: ") == 8, (len(ids), txt[:200]))

    before = (j(BOOKS / MK / "transcript.json") or {}).get("segments") or []
    untouched = next(s for s in before if s["id"] not in ("s00000", "s00002"))
    bad, txt = m.call("video_transcript_save", {
        "book": MK,
        "edits": [{"id": "s00000", "text": "改过的第一段"},
                  {"id": "s00001", "speaker": "新讲师"}],
        "drop": ["s00002"],
        "add": [{"after": "s00001", "text": "补上的那一句"}],
    })
    after = (j(BOOKS / MK / "transcript.json") or {}).get("segments") or []
    by_id = {s.get("id"): s for s in after}
    chk("video_transcript_save：改一段 / 删一段 / 插一段，一次做完还存回去了",
        not bad and len(after) == 8, (txt[:160], len(after)))
    chk("video_transcript_save：没被点名的段落一段没丢（这条守的是整本覆盖那个坑）",
        untouched.get("id") in by_id
        and by_id[untouched["id"]].get("text") == untouched.get("text"),
        (untouched.get("id"), len(before), len(after)))
    chk("改动真的落盘：正文改了、说话人改了、被删的那段没了、新增的那段在",
        by_id.get("s00000", {}).get("text") == "改过的第一段"
        and by_id.get("s00001", {}).get("speaker") == "新讲师"
        and "s00002" not in by_id
        and any(s.get("text") == "补上的那一句" for s in after),
        [s.get("id") for s in after])
    new_seg = next((s for s in after if s.get("text") == "补上的那一句"), {})
    prev_end = by_id.get("s00001", {}).get("end")
    next_start = by_id.get("s00003", {}).get("start")
    chk("新插的那段自己没给时间时，从邻居推出来的秒数还在屏幕顺序里（不越过右边那段）",
        new_seg.get("start") is not None
        and (prev_end is None or new_seg["start"] + 0.01 >= prev_end)
        and (next_start is None or new_seg["start"] <= next_start + 0.01),
        (new_seg.get("start"), prev_end, next_start))

    bad, txt = m.call("video_transcript_save", {
        "book": MK, "segments": [{"id": "s00000", "text": "整本只留这一段"}],
        "edits": [{"id": "s00001", "text": "又要改这段"}]})
    chk("护栏：整本覆盖与只改几段两种模式同时给时拒绝，不猜哪个算数",
        bad and "只能选一样" in txt, txt[:200])
    bad, txt = m.call("video_transcript_save", {"book": MK})
    chk("护栏：什么都没给就报「没给要存的内容」，不动盘",
        bad and "没给" in txt, txt[:200])
    bad, txt = m.call("video_transcript_save", {"book": MK, "segments": []})
    still = len(((j(BOOKS / MK / "transcript.json") or {}).get("segments")) or [])
    chk("护栏：空 segments 一律不接（后端整本覆盖，接了就等于清空用户转写）",
        bad and "没给" in txt and still == 8, (txt[:160], still))
    bad, txt = m.call("video_transcript_save", {
        "book": MK, "edits": [{"id": "s99999", "text": "对不上的那段"}]})
    still = len(((j(BOOKS / MK / "transcript.json") or {}).get("segments")) or [])
    chk("对不上的段 id 只报不猜：点名是哪几个没找到、已跳过，绝不顺手改「最像的那一段」",
        not bad and "s99999" in txt and "跳过" in txt and still == 8, (txt[:200], still))
    # 一次「全选删除」：这种请求在编辑器里正常，走到 agent 手上就成了「整本没了」。
    # 逐段删是界面上一次一笔的事，工具不该一句话就把转写清空。
    all_ids = [s.get("id") for s in ((j(BOOKS / MK / "transcript.json") or {}).get("segments") or [])]
    bad, txt = m.call("video_transcript_save", {"book": MK, "drop": all_ids})
    kept = len(((j(BOOKS / MK / "transcript.json") or {}).get("segments")) or [])
    chk("护栏：改到整本一段不剩时拒绝写盘，盘上那 8 段还在（清空转写请重转或在界面逐段删）",
        bad and "不写盘" in txt and kept == 8, (txt[:220], kept))

    bad, txt = m.call("video_rebuild", {"book": MK})
    ch0 = ""
    try:
        ch0 = (BOOKS / MK / "chapters" / "0000.md").read_text(encoding="utf-8")
    except Exception:
        pass
    chk("video_rebuild：章节正文跟着这份转写重铺（改了字，正文里就该看得见）",
        not bad and "改过的第一段" in ch0, (txt[:160], ch0[:120]))
    chk("video_rebuild：回话说清旧章节挪进了备份，不是直接盖掉",
        "备份" in txt, txt[:240])

    bad, txt = m.call("video_export", {"book": MK, "fmt": "srt"})
    exp = sorted((BOOKS / MK / "exports").glob("*.srt")) if (BOOKS / MK / "exports").is_dir() else []
    cnt = re.search(r"目前共 (\d+) 份", txt)
    chk("video_export：字幕落到 exports/，回话报的份数与磁盘一致（不是张口就说 0 份）",
        not bad and exp and int(cnt.group(1)) == len(exp), (txt[:220], [p.name for p in exp]))

    # ── 4. 思维导图：不给 nodes 就不许清空用户画的图 ──────────────
    bad, txt = m.call("map_show", {"book": MK})
    chk("map_show：还没有图时说得清楚，并指路怎么生成",
        not bad and "空" in txt and "map_save" in txt, txt[:240])

    bad, txt = m.call("map_save", {"book": MK, "title": "取证图", "nodes": [
        {"text": "中心", "children": [{"text": "甲", "children": [{"text": "甲一"}]},
                                       {"text": "乙"}, {"text": "丙"}]}]})
    disk_map = j(BOOKS / MK / "mindmap.json") or {}
    chk("map_save：写进去的树落盘了（4 个框 = 1 根 + 3 子，含一层孙）",
        not bad and len(re.findall(r'"id"', json.dumps(disk_map))) >= 4
        and disk_map.get("title") == "取证图", (txt[:180], len(disk_map.get("nodes") or [])))

    n_before = len(json.dumps(disk_map.get("nodes") or []).split('"text"')) - 1
    bad, txt = m.call("map_save", {"book": MK, "title": "改了名", "form": "fishbone"})
    after_map = j(BOOKS / MK / "mindmap.json") or {}
    n_after = len(json.dumps(after_map.get("nodes") or []).split('"text"')) - 1
    chk("map_save：只改标题 / 形态时节点一个没少（空 nodes 会把用户手画的清干净，这条是护栏）",
        not bad and n_after == n_before and n_after >= 4
        and after_map.get("title") == "改了名" and after_map.get("form") == "fishbone",
        (n_before, n_after, txt[:180]))
    chk("map_save：不传 nodes 时回话明说「只改了标题 / 形态」",
        "只改了标题" in txt, txt[:240])

    bad, txt = m.call("map_show", {"book": MK})
    chk("map_show：大纲形态看得见每个框，并报得出盘上存的形态",
        not bad and "中心" in txt and "甲一" in txt and "鱼骨图" in txt, txt[:300])
    bad, txt = m.call("map_show", {"book": MK, "form": "radial"})
    chk("map_show：换 form 只是临时排布 —— 回话要说清盘上还是原来那个形态",
        not bad and "临时排布" in txt and "辐射图" in txt and "鱼骨图" in txt, txt[:320])
    bad, txt = m.call("map_show", {"book": MK, "as": "svg"})
    chk("map_show as=svg：报大小和怎么落成文件，不把一大段 XML 灌回对话",
        not bad and "SVG" in txt and "<svg" not in txt, txt[:240])
    bad, txt = m.call("map_show", {"book": MK, "as": "json"})
    try:
        as_json = json.loads(txt)
    except Exception:
        as_json = None
    chk("map_show as=json：能拿到含 id 与坐标的完整信封（照着改就能存回去）",
        not bad and isinstance(as_json, dict) and bool((as_json.get("nodes") or [{}])[0].get("id")),
        txt[:200])

    bad, txt = m.call("map_from_notes", {"book": MK})
    still = j(BOOKS / MK / "mindmap.json") or {}
    chk("map_from_notes：只给不存 —— 盘上那张图一个字没动（点错一下不该盖掉用户画的）",
        not bad and still == after_map, (txt[:160], still.get("title")))

    # ── 5. 画板：只改标题不能把笔画抹平 ─────────────────────────
    bad, txt = m.call("board_list", {"book": MK})
    chk("board_list：一块板都没有时讲明白，并报出上限和目录",
        not bad and "还没有" in txt and "boards" in txt, txt[:260])

    bad, txt = m.call("board_new", {"book": MK, "title": "乱纸面", "paper": "sparkle"})
    chk("board_new：纸面写错时列出可选项，并且这次一块也不建",
        bad and "sparkle" in txt and "grid" in txt
        and not (BOOKS / MK / "boards").exists(), (txt[:260],
        sorted(p.name for p in (BOOKS / MK / "boards").glob("*.json"))
        if (BOOKS / MK / "boards").is_dir() else []))

    bad, txt = m.call("board_new", {"book": MK, "title": "取证板", "paper": "grid"})
    bid = (re.search(r"\[id: ([A-Za-z0-9_-]+)\]", txt) or [None, ""])[1]
    env = j(BOOKS / MK / "boards" / (bid + ".json")) if bid else None
    chk("board_new：建出来了，回话带着 id（agent 后面全靠它），纸面按要的那种存",
        not bad and bid and env and env.get("paper") == "grid", (txt[:240], bid))

    canvas = {"objects": [{"type": "i-text", "left": 20, "top": 30, "text": "这行是笔记"},
                          {"type": "path", "path": ["M 0 0", "L 10 10"]},
                          {"type": "rect", "left": 5, "top": 5, "width": 8, "height": 8}]}
    bad, txt = m.call("board_save", {"book": MK, "id": bid, "canvas": canvas})
    objs = ((j(BOOKS / MK / "boards" / (bid + ".json")) or {}).get("canvas") or {}).get("objects") or []
    chk("board_save：整块覆盖这条低路走得通，回话说清现在上面有几个对象",
        not bad and len(objs) == 3 and "3 个对象" in txt, (txt[:200], len(objs)))

    bad, txt = m.call("board_save", {"book": MK, "id": bid, "title": "改过名的板",
                                     "caption": "边看书边画的那一块"})
    env = j(BOOKS / MK / "boards" / (bid + ".json")) or {}
    chk("board_save：不带 canvas 时画布原样没动（只传标题就会抹平笔画的那个坑）",
        not bad and len((env.get("canvas") or {}).get("objects") or []) == 3
        and env.get("title") == "改过名的板" and "原样没动" in txt,
        (txt[:200], len((env.get("canvas") or {}).get("objects") or [])))

    bad, txt = m.call("board_show", {"book": MK, "id": bid})
    chk("board_show：摘要报得出对象数，并把写在板上的字读出来",
        not bad and "3 个对象" in txt and "这行是笔记" in txt, txt[:300])
    bad, txt = m.call("board_show", {"book": MK, "id": bid, "as": "canvas"})
    chk("board_show as=canvas：原样给出画布 JSON（照着改一笔就能存回去）",
        not bad and len(json.loads(txt).get("objects") or []) == 3, txt[:160])
    bad, txt = m.call("board_show", {"book": MK, "id": bid, "as": "md"})
    chk("board_show as=md：没导出过图时不写那条点开是坏图的链接，并说清图从哪儿来",
        not bad and "还没有导出图片" in txt and "](" not in txt and "导出图片" in txt, txt[:300])
    http_post("/api/board", {"act": "export", "book": MK, "id": bid, "fmt": "svg",
                             "data": "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'></svg>"})
    bad, txt = m.call("board_show", {"book": MK, "id": bid, "as": "md"})
    chk("board_show as=md：图导出过之后，笔记里那条链接就指向真的那个文件",
        not bad and "boards/%s.svg" % bid in txt and "图导出过" in txt, txt[:300])

    bad, txt = m.call("board_show", {"book": MK, "id": "bnotexist"})
    chk("读一块不存在的板：把 id 报回去并指路 board_list，不甩 HTTP 404",
        bad and "bnotexist" in txt and "board_list" in txt and "HTTP" not in txt, txt[:260])
    bad, txt = m.call("board_save", {"book": MK, "id": "bnotexist", "title": "顺手存一下"})
    chk("存一块不存在的板：拒绝，并说明要用 board_new（不在这里凭空造一块）",
        bad and "board_new" in txt and "HTTP" not in txt, txt[:260])

    bad, txt = m.call("board_save", {"book": MK, "id": bid, "canvas": {"objects": []}})
    chk("护栏：真把空画布覆盖上去时，回话明说「这块板上的东西这次没了」",
        not bad and "这次没了" in txt, txt[:260])

    bad, txt = m.call("board_delete", {"book": MK, "id": bid})
    chk("board_delete：删掉了，回话报还剩几块",
        not bad and "还剩 0" in txt and not (BOOKS / MK / "boards" / (bid + ".json")).exists(),
        txt[:240])
    bad, txt = m.call("board_delete", {"book": MK, "id": bid})
    chk("board_delete：删第二遍不算失败，也不甩红字（文件本来就不在）",
        not bad and "不在了" in txt, txt[:240])

    # ── 6. 便签（flomo）：Agent 读的是用户那本笔记，不是它自己编的清单 ──
    # 这一条走的是「界面之外的第二条通路」：界面上看得见的东西，工具那边必须同样读得到，
    # 而且读到的必须是同一批数据 —— 所以这里先导入真导出包，再从工具口子里数条数。
    import base64
    import flomo_fixture as FF
    http_post("/api/flomo/notes", {"act": "clear"})     # 从干净账开始，别被上一次跑的残留骗了

    bad, txt = m.call("flomo_notes", {})
    chk("flomo_notes：账是空的时候不报错，说的是「去哪导入」",
        not bad and "还没有导入" in txt and "便签" in txt, txt[:160])
    bad, txt = m.call("flomo_portrait")
    chk("flomo_portrait：账是空的时候不给假画像",
        not bad and "空的" in txt, txt[:160])

    imp = http_post("/api/flomo/notes", {"act": "import", "name": "mcp.zip",
                                         "data": base64.b64encode(FF.export_zip()).decode()})
    chk("夹具：这份导出包真的进了本机账（%d 条）" % FF.N_MEMOS,
        imp.get("ok") and (imp.get("stats") or {}).get("memos") == FF.N_MEMOS,
        imp if not imp.get("ok") else (imp.get("stats") or {}).get("memos"))

    bad, txt = m.call("flomo_notes", {})
    ids = re.findall(r"\[id: (fm_\w{6,})\]", txt)
    chk("flomo_notes：默认那一屏把每条的 id、时间、标签与正文都给了（agent 靠 id 说话）",
        not bad and len(ids) == FF.N_MEMOS and "本机存着 %d 条" % FF.N_MEMOS in txt,
        (len(ids), txt[:260]))
    chk("flomo_notes：头部带这批笔记的骨架数字（条 / 天 / 字数 / 图 / 标签）",
        "骨架" in txt and "个标签" in txt and "张图" in txt, txt[:200])
    chk("flomo_notes：一次给全时不说翻页，条目多到给不完时才报「还剩几条」",
        "还剩" not in txt, txt[-200:])
    # 界面上标签只在卡片下面那排药丸里露一次，正文末尾那几个字是前端摘掉的。Agent 这条
    # 通路没有前端：后端要是把原始 md 直接吐给它，同一个标签就在同一条里出现两遍 ——
    # 用户屏幕上看不到的东西，喂给模型的也不该有。逐条比对「头一行列的标签」与正文。
    dupes, cur = [], None
    for ln in txt.split("\n"):
        hit = re.search(r"\[id: (fm_\w{6,})\]", ln)
        if hit:
            cur = (hit.group(1), set(re.findall(r"#([^\s#]+)", ln.split("[id:")[0])))
            continue
        if not ln.strip():
            continue
        if not ln.startswith("  "):
            cur = None
            continue
        for t in (cur[1] if cur else ()):
            if "#" + t in ln:
                dupes.append((cur[0], t, ln.strip()[:44]))
    chk("flomo_notes：正文不再重复头一行列过的 #标签（Agent 读的和屏幕上是一个样）",
        not dupes, dupes[:3])
    chk("flomo_notes：句末那个「#3」序号原样留着（摘的只是账上的标签，不是所有井号）",
        "#3" in txt, txt[:400])
    pg = m.call("flomo_notes", {"limit": 5, "offset": 0})[1]
    chk("flomo_notes：翻页数得清（前 5 条、还剩 %d 条、下一页 offset=5）" % (FF.N_MEMOS - 5),
        "第 1-5 条" in pg and "还剩 %d 条" % (FF.N_MEMOS - 5) in pg and "offset=5" in pg,
        (pg[:120], pg[-200:]))
    chk("flomo_notes：末尾指着「先读画像」这条更便宜的路",
        "flomo_portrait" in txt, txt[-200:])

    leaf = m.call("flomo_notes", {"tag": "读书/神经科学"})[1]
    bad, txt = m.call("flomo_notes", {"tag": "读书"})
    chk("flomo_notes：按标签筛用的是界面那同一把尺（父标签带子标签，叶子不多捞）",
        not bad and "筛出 2 条" in txt and "筛出 1 条" in leaf, (txt[:140], leaf[:140]))
    bad, txt = m.call("flomo_notes", {"tag": "不存在的那个标签"})
    chk("flomo_notes：标签落空时把账上有的标签报回去，不只说一句「没有」",
        not bad and "筛出 0 条" in txt and "标签账上有" in txt, txt[:200])
    bad, txt = m.call("flomo_notes", {"q": "复利"})
    chk("flomo_notes：关键字搜的是正文里那句话",
        not bad and "复利" in txt and "筛出 1 条" in txt, txt[:200])

    trunc = m.call("flomo_notes", {"tag": "灵感"})[1]
    long_id = re.findall(r"\[id: (fm_\w{6,})\][^\n]*\n[^\n]*还差", trunc)
    chk("flomo_notes：长那条在列表里掐尾，并说清还差多少字",
        not bad and "还差" in trunc and len(long_id) == 1, trunc[:200])
    bad, txt = m.call("flomo_notes", {"id": long_id[0] if long_id else ids[0]})
    chk("flomo_notes：带着 id 再取一次给的是全文（列表里那句「还差」在这儿兑现）",
        not bad and "还差" not in txt and "[id: " in txt, txt[:160] + "..." + txt[-160:])
    chk("flomo_notes：图链不给打不开的本地路径，换成「几张图」这句话",
        "files/" not in txt and "![](" not in txt, txt[:200])
    bad, txt = m.call("flomo_notes", {"id": "fm_deadbeef"})
    chk("flomo_notes：id 认不出来时说人话，并指路用 tag / q 重筛",
        not bad and "没有 id" in txt and "tag" in txt, txt[:160])

    bad, txt = m.call("flomo_portrait")
    wrote = http_get("/api/flomo/notes?mode=portrait")
    chk("flomo_portrait：读回来的是那页画像（体量、分类习惯、常打的标签、节奏、深加工）",
        not bad and all(s in txt for s in ("我的记忆画像", "分类习惯", "常打的标签",
                                           "节奏", "深加工")), txt[:200])
    chk("flomo_portrait：画像里的条数与账一致（不是另算一套）",
        ("%d 条 flomo 笔记" % FF.N_MEMOS) in txt, txt[:240])
    chk("flomo_portrait：说清了它存在哪，且那个位置真有东西",
        (wrote.get("file") or "").endswith(".md") and wrote.get("ok")
        and (wrote.get("md") or "").startswith("# 我的记忆画像")
        and str(wrote.get("file")) in txt, txt[-260:])
    first_memo = ((http_get("/api/flomo/notes?mode=list&limit=200").get("memos")
                   or [{}])[0].get("md") or "")[:14]
    chk("红线：画像一个字都不带笔记原文（它每次任务前都要喂给模型）",
        len(first_memo) > 6 and first_memo not in txt, first_memo)

    http_post("/api/flomo/notes", {"act": "clear"})
    bad, txt = m.call("flomo_notes", {})
    chk("清空之后：工具那边跟着回到「还没有导入」（读的是同一本账，不是缓存的旧数）",
        not bad and "还没有导入" in txt, txt[:160])

    # ── 7. 界面上那句「一键接入 MCP」的说明不能停在旧数字 ──────────
    # 取的是 /api/mcp 渲染出来的成品文本，不是源码里的字面量：说明里的数是现算的，
    # 只有跑起来才看得见它到底说了几个工具（上一版写死「20 个」，工具涨到 45 个也没人发现）。
    prompt = ""
    try:
        with urllib.request.urlopen(BASE + "/api/mcp", timeout=30) as r:
            prompt = json.loads(r.read().decode("utf-8")).get("prompt") or ""
    except Exception as exc:
        prompt = ""
        chk("/api/mcp 取得到接入说明", False, repr(exc))
    stated = re.findall(r"(\d+) 个工具", prompt)
    chk("接入说明里的工具数与 tools/list 真数一致（写旧数字就是把说明变成误导）",
        stated == [str(len(names))], (stated, len(names)))
    absent = [x for x in names if x not in prompt]
    chk("接入说明把全部 %d 个工具都点到了名（漏了 agent 就不知道有这个能力）" % len(names),
        not absent, absent)
    chk("接入说明讲清了长任务与覆盖语义这两条设计口径",
        "轮询" in prompt and "整本覆盖" in prompt and "canvas" in prompt, prompt[-320:])
    chk("接入说明交代了「与笔记有关先读画像、便签只读」这条口径（用户要的流程）",
        "flomo_portrait 再动手" in prompt and "便签一律只读" in prompt, prompt[-520:])
finally:
    if FAIL:
        tail = "".join(m.err[-12:])
        if tail.strip():
            print("── 适配器 stderr（排查用）──────────────")
            print(tail.strip()[-1200:])
    m.close()

print()
print("MCP 门禁：%d 项通过，失败 %d 项（tools/list 数到 %s 个工具）"
      % (PASSED, len(FAIL), len(names) if names else "?"))
sys.exit(1 if FAIL else 0)
