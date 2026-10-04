# -*- coding: utf-8 -*-
"""AI 小结：一次读多少、怎么问、模型答得不像话时怎么兜、结果存哪儿。

这一轮把「读书 / 剪藏 / 视频」三种内容的总结收成一个入口，规则全在 ai_sum.py，
界面只负责把「我在读哪一章」报上去。所以这一套也照着规则的形状来验：

  A module_of      —— 「这本书算哪一路」必须和 book_layout 同一口径。两处判据一旦分叉，
                      界面上就成了「剪藏的文章有时整篇读、有时只读一段」。
  B scope_for      —— 用户定的那条硬口径：读书一次只读当前这一章，没给章节就报空，
                      后端绝不替用户挑一章；剪藏 / 订阅 / 视频整篇读。
  C fit            —— 超长正文掐中段留首尾，而且要出声告诉模型略过了多少字。
  D messages       —— 两种模式的提示词各自长什么样（学习理解必须含那五节，归纳必须
                      是「一句结论 + 要点」），未知模式退回归纳而不是报错。
  E parse_map      —— 模型回的可能是散文、代码围栏、分支只有两条的半个骨架。
                      每种都得兜住，且绝不抛 —— 抛穿到界面上就是「点了没反应」。
  F head_tree      —— 兜底那张图至少是原文的目录，不是空框。
  G 缓存           —— 一个模式一份、一章一份，互不覆盖；章节名来自磁盘，先卡形状，
                      写不进去也不许抛。
  H 后端行为       —— 拿真书目录跑 ai_source / ai_summary_stream / ai_mindmap：
                      没配接口时不许碰网络，正文取不到时要说人话。
  H2 流式收尾      —— 上游半路拔线和「答完了」必须分得开：分不开就会把半截当成品
                      存盘，覆盖掉上一次那份好的，界面上还写着「小结好了」。
  I 接线           —— 路由、进包清单、界面上那两枚芯片的键名与后端对齐。

全程不联网、不开浏览器：LLM 那一头只验「没配就不发请求」，真流式回话归
tests/check_ai_summary_ui.py 那份真机套件（它自己起一个假的 completions 服务）。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-aisum-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import ai_sum                                                # noqa: E402
import book_layout                                           # noqa: E402
import ui_server                                             # noqa: E402

FAIL = []
PASSED = 0
# 与其余真机套件同一张表情判定表（界面零 emoji 是死规矩，提示词也算界面的一部分）
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➯️⬀-⯿]")


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def write(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    io.open(path, "w", encoding="utf-8").write(text)


def read_repo(fname):
    """读仓库里的一个源码文件（只给「接线还在不在」那类断言用）。"""
    return io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                fname), encoding="utf-8").read()


def depth(node):
    """树的层数：只有一个节点算 1 层。用来验模型给的超深树有没有被削平。"""
    kids = (node or {}).get("kids") or []
    return 1 + (max([depth(k) for k in kids], default=0))


# ── A. 哪一路：和 book_layout 同口径 ────────────────────────────
chk("module_of：六种来路各自认得",
    [ai_sum.module_of({"source": m}) for m in ("clip", "feed", "video", "local", "weread", "flomo")]
    == ["clip", "feed", "video", "local", "weread", "flomo"])
chk("module_of：没 meta 时靠目录前缀认（imp_ 算本地书架，别当微信读书）",
    ai_sum.module_of({}, "clip_123") == "clip"
    and ai_sum.module_of({}, "feed_x") == "feed"
    and ai_sum.module_of({}, "video_x") == "video"
    and ai_sum.module_of({}, "imp_SE_RECIPE") == "local"
    and ai_sum.module_of({}, "flomo_a1b2c3") == "flomo"
    and ai_sum.module_of({}, "SE_OS") == "weread")
chk("module_of：meta 说了算，前缀只是兜底（剪进来的书就算顶着 clip_ 也是剪藏）",
    ai_sum.module_of({"source": "clip"}, "SE_OS") == "clip")
# 两处判据分开写是有意的（总结不该跟着目录结构重排一起动），但口径不许分叉：
# 拿同一批 (book_id, meta) 让两边各判一次，任何一本判得不一样就是这块要出事。
CROSS = [("clip_SE_POST", {"source": "clip", "format": "html"}),
         ("feed_abc", {"source": "feed"}),
         ("video_abc", {"source": "video"}),
         ("imp_SE_RECIPE", {"source": "local", "format": "md"}),
         ("flomo_a1b2c3", {"source": "flomo", "format": "clip"}),
         ("SE_OS", {"source": "weread"}),
         ("SE_OLD", {"format": "epub"}),
         ("whatever", {})]
diverge = [(b, ai_sum.module_of(m, b), book_layout.module_of(b, m)) for b, m in CROSS
           if ai_sum.module_of(m, b) != book_layout.module_of(b, m)]
chk("口径一致：ai_sum 与 book_layout 对同一批书判的是同一个模块", not diverge, diverge)

# ── B. 这次读多少 ──────────────────────────────────────────────
chk("scope_for：剪藏 / 订阅 / 视频 / 便签整篇读，且不认章节",
    ai_sum.scope_for("clip", "0002.md") == ("whole", "")
    and ai_sum.scope_for("feed") == ("whole", "")
    and ai_sum.scope_for("video", "0001.md") == ("whole", "")
    and ai_sum.scope_for("flomo", "flomo.md") == ("whole", ""))
chk("scope_for：读书与本地导入只读当前这一章",
    ai_sum.scope_for("weread", "0003.md") == ("chapter", "0003.md")
    and ai_sum.scope_for("local", "0001.md") == ("chapter", "0001.md"))
# 这条是硬规矩：前端漏传章节时后端宁可回空，也不能「随便挑一章」——
# 「总结得不对」的头号来源就是替用户挑。
chk("scope_for：读书没给章节就报空，绝不自己挑一章",
    ai_sum.scope_for("weread") == ("", "") and ai_sum.scope_for("local", "   ") == ("", ""))
chk("常量自洽：WHOLE_MODULES 就是那四个整篇读的模块（便签一条就是整篇，别要求章节）",
    set(ai_sum.WHOLE_MODULES) == {"clip", "feed", "video", "flomo"})
chk("常量自洽：每一路都说得出体裁（漏一路会把便签说成「微信读书的一本书」）",
    set(ai_sum.KIND_BY_MODULE) == set(ai_sum.WHOLE_MODULES) | {"local", "weread"},
    sorted(set(ai_sum.KIND_BY_MODULE)))
chk("常量自洽：整篇的上限比单章高（视频长转写才喂得下）",
    ai_sum.MAX_FEED_WHOLE > ai_sum.MAX_FEED_CHAPTER
    and ai_sum.feed_limit("chapter") == ai_sum.MAX_FEED_CHAPTER
    and ai_sum.feed_limit("whole") == ai_sum.MAX_FEED_WHOLE)

# ── C. 超长正文 ───────────────────────────────────────────────
short = "第一段。\n第二段。\n"
chk("fit：没超长的原样不动（不许给正常长度的正文加戏）", ai_sum.fit(short, 9000) == short)
big = "".join("第%04d段内容，末尾留个记号。\n" % i for i in range(4000))
cut = ai_sum.fit(big, 3000)
chk("fit：超长时掐中段、留首尾", cut.startswith(big[:20]) and cut.endswith(big[-20:])
    and len(cut) < len(big))
chk("fit：略过了多少字要明说（不声不响地截，模型会把没有的当原文没有）",
    "中间略去 %d 字" % (len(big) - 3000) in cut, cut[-90:])
chk("fit：空正文不炸", ai_sum.fit("", 100) == "" and ai_sum.fit(None, 100) == "")

# ── D. 提示词 ─────────────────────────────────────────────────
ms = ai_sum.messages("learn", "chapter", "测试这本书", "正文一二三", "weread")
chk("messages：一发就是一条 system + 一条 user（不多塞角色，兼容面最广）",
    len(ms) == 2 and [m["role"] for m in ms] == ["system", "user"])
LEARN_SECTIONS = ("## 一句话核心", "## 术语对照", "## 讲透它", "## 最小执行步骤", "## 检验你")
chk("学习理解：五个小节一个都不能少（费曼那一路的骨架就是这五节）",
    all(s in ms[0]["content"] for s in LEARN_SECTIONS),
    [s for s in LEARN_SECTIONS if s not in ms[0]["content"]])
chk("学习理解：既要求「例子只能来自材料」，也给了材料没例子时的退路",
    "例子只能来自材料" in ms[0]["content"] and "材料没给例子" in ms[0]["content"])
chk("学习理解：末尾逼用户输出一次（自测题 + 留白）",
    "我的回答：＿＿＿" in ms[0]["content"])
brief = ai_sum.messages("brief", "whole", "测试", "正文", "clip")
chk("归纳：只要一句结论加要点，且明确不许复述铺垫",
    "第一行用一句不超过 40 字的话" in brief[0]["content"]
    and "不要复述铺垫" in brief[0]["content"])
chk("两种模式的提示词确实是两套（同一段 prompt 换来的是同一种回答）",
    ms[0]["content"] != brief[0]["content"])
chk("未知模式退回归纳，不抛错也不回空",
    ai_sum.messages("whatever", "whole", "t", "x")[0]["content"] == brief[0]["content"])
chk("范围说明跟着 scope 走：单章时禁止替全书下结论",
    "其中一章" in ms[1]["content"] and "不要替全书下结论" in ms[1]["content"])
chk("范围说明：整篇时按整篇讲", "按整篇来总结" in brief[1]["content"])
chk("体裁跟着来路走（剪藏的文章不该被当成一本书）",
    "一篇剪藏下来的网页文章" in brief[1]["content"]
    and "微信读书里取回来的一本书" in ms[1]["content"])
chk("正文进 user 消息前已经被 fit 削过",
    "中间略去" in ai_sum.messages("brief", "whole", "t", big, "clip")[1]["content"])
chk("标题截到 200 字（一本书的标题能有一千字，全喂进去没意义）",
    ai_sum.USER_TMPL.count("{title}") == 1
    and len(ai_sum.messages("brief", "whole", "标" * 400, "x")[1]["content"]
             .split("标题：")[1].split("正文：")[0]) < 210)
mp = ai_sum.map_messages("一本书", "正文", "chapter")
chk("导图提示词：形状、分支数、深度都是同一批常量（改了 prompt 忘了改常量会当场红）",
    ("%d 到 %d 个" % (ai_sum.MIN_BRANCHES, ai_sum.MAX_BRANCHES)) in mp[0]["content"]
    and ("最深 %d 层" % ai_sum.MAX_DEPTH) in mp[0]["content"]
    and ("分支名不超过 %d 字" % ai_sum.MAP_LABEL) in mp[0]["content"])
chk("导图提示词：禁止占位分支与平铺章节（那两种画出来不像导图）",
    "禁止「其他」「补充」「更多内容」这类占位词" in mp[0]["content"]
    and "一个章节一个分支" in mp[0]["content"])

# ── E. 模型输出的兜底 ──────────────────────────────────────────
GOOD_MAP = json.dumps({
    "title": "换个名",
    "root": {"label": "中心", "kids": [
        {"label": "分支甲", "kids": [{"label": "甲的具体做法", "kids": []}]},
        {"label": "分支乙", "kids": []},
        {"label": "分支丙", "kids": [{"label": "丙的末级", "kids": []}]}]},
}, ensure_ascii=False)
tree, warn = ai_sum.parse_map(GOOD_MAP, "原标题", "正文")
chk("parse_map：合格的树原样收下，不给警告", warn == "" and tree["label"] == "中心"
    and len(tree["kids"]) == 3, (tree, warn))
fenced = "```json\n%s\n```" % GOOD_MAP
chk("parse_map：代码围栏包着的 JSON 照样解得开",
    ai_sum.parse_map(fenced, "t", "")[0]["label"] == "中心")
chatty = "好的，这是您要的导图：\n" + GOOD_MAP + "\n希望有帮助。"
chk("parse_map：前后夹一句客套也抠得出来（模型爱这么干）",
    ai_sum.parse_map(chatty, "t", "")[0]["label"] == "中心")
wrapped = "<think>让我先想想……</think>" + GOOD_MAP
chk("parse_map：推理标签里那一大段不干扰解析",
    ai_sum.parse_map(wrapped, "t", "")[0]["label"] == "中心")
alias = json.dumps({"root": {"text": "中心", "children": [
    {"text": "甲", "children": []}, {"text": "乙"}, {"text": "丙"}]}}, ensure_ascii=False)
chk("parse_map：label/text、kids/children 两种写法都认（不同模型爱用的键不一样）",
    ai_sum.parse_map(alias, "t", "")[0]["label"] == "中心"
    and len(ai_sum.parse_map(alias, "t", "")[0]["kids"]) == 3)
thin = json.dumps({"root": {"label": "中心", "kids": [{"label": "只有一条"}]}},
                  ensure_ascii=False)
t2, w2 = ai_sum.parse_map(thin, "一本有目录的书", "# 第一节\n讲点A。\n\n# 第二节\n讲点B。\n\n# 第三节\n讲点C。\n")
chk("parse_map：分支少于三条时把原文小标题补进去凑到能看，并且出声（少于 3 条在界面上是一条直线，看着像坏了）",
    len(t2["kids"]) >= ai_sum.MIN_BRANCHES and t2["label"] == "中心" and w2 != "",
    (len(t2["kids"]), w2))
t3, w3 = ai_sum.parse_map("这一段话不是 JSON，模型今天不听话。", "t", "正文")
chk("parse_map：完全不是 JSON 时回兜底树 + 一句人话，绝不抛",
    isinstance(t3, dict) and "导图" in w3, (t3, w3))
t4, _w4 = ai_sum.parse_map("", "t", "")
chk("parse_map：空回复也回一棵树（界面上不许出现空框）", isinstance(t4, dict))
many = json.dumps({"root": {"label": "中心",
                            "kids": [{"label": "分支%d" % i, "kids": []}
                                     for i in range(12)]}}, ensure_ascii=False)
chk("parse_map：一级分支太多时削到上限（12 个分支摊成一圈没法看）",
    len(ai_sum.parse_map(many, "t", "")[0]["kids"]) == ai_sum.MAX_BRANCHES)
deep = {"label": "L1"}
cur = deep
for i in range(2, 10):
    cur["kids"] = [{"label": "L%d" % i, "kids": []}]
    cur = cur["kids"][0]
chk("parse_map：超过最大深度的那几层直接削掉（深度是排版算出来的天花板）",
    depth(ai_sum.parse_map(json.dumps(deep), "t", "")[0]) <= ai_sum.MAX_DEPTH,
    depth(ai_sum.parse_map(json.dumps(deep), "t", "")[0]))
long_label = json.dumps({"root": {"label": "中心", "kids": [
    {"label": "分" * 300, "kids": []}, {"label": "支" * 300, "kids": []},
    {"label": "名" * 300, "kids": []}]}}, ensure_ascii=False)
chk("parse_map：过长的分支名被裁短（不裁就撑破框，四个形态一起歪）",
    all(len(n["label"]) <= ai_sum.MAP_LEAF * 2
        for n in ai_sum.parse_map(long_label, "t", "")[0]["kids"]),
    [len(n["label"]) for n in ai_sum.parse_map(long_label, "t", "")[0]["kids"]])
chk("_json_object：花括号不配对的半截 JSON 回 None（不硬猜）",
    ai_sum._json_object('{"root": {"label": "断在这里"') is None)
chk("_json_object：字符串里的花括号不捣乱",
    ai_sum._json_object('{"root": {"label": "这里有 } 和 { 两个花括号", "kids": []}}')
    == {"root": {"label": "这里有 } 和 { 两个花括号", "kids": []}})

# ── F. 兜底树 ─────────────────────────────────────────────────
hd = ai_sum.head_tree("一本有目录的书",
                      "# 第一章\n\n这一章讲分配。后面还有一句。\n\n"
                      "## 要点一\n- 碎片\n- 合并\n2. 伙伴系统\n正文一段话。\n")
chk("head_tree：Markdown 标题变分支",
    [k["label"] for k in hd["kids"]] == ["第一章", "要点一"], hd)
chk("head_tree：列表项与正文首句都落成叶子（末级要点具体到能看懂）",
    [k["label"] for k in hd["kids"][1]["kids"]] == ["碎片", "合并", "伙伴系统", "正文一段话"],
    hd["kids"][1])
chk("head_tree：标题下的正文只取第一句进图（整段塞一个框会溢出）",
    [k["label"] for k in hd["kids"][0]["kids"]] == ["这一章讲分配"], hd["kids"][0])
chk("head_tree：正文首句只取到第一个句号",
    ai_sum.sentence("第一句很重要。第二句就不必进图了。") == "第一句很重要")
chk("head_tree：空材料也回一棵能渲染的树", hd["label"] == "一本有目录的书"
    and isinstance(ai_sum.head_tree("", "")["kids"], list))

# ── G. 缓存 ──────────────────────────────────────────────────
chk("cache_name：一个模式一份，两种模式互不覆盖",
    ai_sum.cache_name("whole", "learn") != ai_sum.cache_name("whole", "brief")
    and ai_sum.cache_name("whole", "learn") == "learn-whole.md")
chk("cache_name：单章按章名各存一份",
    ai_sum.cache_name("chapter", "brief", "0003.md") == "brief-0003.md")
chk("cache_name：未知模式退回 brief（不然会写出一个读不回来的文件名）",
    ai_sum.cache_name("whole", "whatever") == "brief-whole.md")
evil = ai_sum.cache_name("chapter", "brief", "../../etc/passwd")
evpath = ai_sum.cache_path("/tmp/whatever-book", "chapter", "brief", "../../etc/passwd")
chk("cache_name：章节名里的斜杠与点先消毒（文件名不许变成路径）",
    "/" not in evil and os.path.dirname(os.path.normpath(evpath))
    == os.path.join("/tmp/whatever-book", ai_sum.CACHE_DIR), (evil, evpath))
BK = os.path.join(ROOT, "cachebook")
p = ai_sum.save_result(BK, "whole", "learn", "", "归纳出来的内容")
chk("save_result：落在 <书>/_ai/learn-whole.md（用户要的是「这本书旁边」，不是一个全局仓库）",
    p == os.path.join(BK, ai_sum.CACHE_DIR, "learn-whole.md") and os.path.isfile(p), p)
chk("load_result：存进去的能原样读回来",
    ai_sum.load_result(BK, "whole", "learn") == "归纳出来的内容")
chk("load_result：单章与整篇各归各的，读串了会看见上一章的小结",
    ai_sum.save_result(BK, "chapter", "brief", "0002.md", "第二章")
    and ai_sum.load_result(BK, "chapter", "brief", "0002.md") == "第二章"
    and ai_sum.load_result(BK, "whole", "brief") == "")
chk("load_result：没存过回空串（界面据此显示「还没问过」，不是显示错误）",
    ai_sum.load_result(BK, "whole", "brief") == "")
chk("load_result：路径根本不存在也不抛", ai_sum.load_result("/nonexistent/x", "whole", "learn") == "")
write(os.path.join(ROOT, "afile"), "我是文件不是目录")
chk("save_result：书目录位置被一个文件占了 → 回空串，不抛异常（存盘失败不该让用户白等一场）",
    ai_sum.save_result(os.path.join(ROOT, "afile"), "whole", "learn", "", "x") == "")

# ── H. 后端拿真书目录跑一遍 ────────────────────────────────────
def mkbook(bid, module, chapters, meta_extra=None):
    d = os.path.join(book_layout.book_dir(ui_server.OUT_DIR, module), bid)
    os.makedirs(os.path.join(d, "chapters"), exist_ok=True)
    for name, body in chapters:
        write(os.path.join(d, "chapters", name), body)
    meta = {"title": "小结测试书", "source": module}
    if meta_extra:
        meta.update(meta_extra)
    write(os.path.join(d, "meta.json"), json.dumps(meta, ensure_ascii=False))
    return d


mkbook("clip_AITEST", "clip", [("0001.md", "# 剪藏一篇\n\n首段。\n\n尾段。\n")])
mkbook("SE_AITEST", "weread", [("0001.md", "# 第一章\n\n第一章的正文。\n"),
                               ("0002.md", "# 第二章\n\n第二章的正文，长一些。\n")])
ok, src = ui_server.ai_source("clip_AITEST")
chk("ai_source：剪藏整篇读（正文是合并后的整篇，不是只取第一段）",
    ok is True and src["scope"] == "whole" and src["module"] == "clip"
    and "首段" in src["text"] and "尾段" in src["text"], src)
ok2, src2 = ui_server.ai_source("SE_AITEST", "0002.md")
chk("ai_source：微信读书只取那一章，且章名原样回给调用方（存盘要用它当文件名）",
    ok2 is True and src2["scope"] == "chapter" and src2["chapter"] == "0002.md"
    and "第二章的正文" in src2["text"] and "第一章" not in src2["text"], src2)
no, msg = ui_server.ai_source("SE_AITEST")
chk("ai_source：读书没带章节 → 一句人话，不是异常（前端就照这句话显示）",
    no is False and "当前这一章" in msg, msg)
no2, msg2 = ui_server.ai_source("SE_AITEST", "0009.md")
chk("ai_source：章名对不上盘上的文件 → 说没找到这一章",
    no2 is False and "没找到这一章" in msg2, msg2)
no3, msg3 = ui_server.ai_source("SE_AITEST", "../../etc/passwd")
chk("ai_source：拿 ../ 当章名读不到任何东西（这道闸不许因为总结而松）",
    no3 is False, (no3, str(msg3)[:80]))
no4, msg4 = ui_server.ai_source("clip_DOES_NOT_EXIST")
chk("ai_source：书号不存在也不抛，回一句「先重新入库」",
    no4 is False and "重新入库" in msg4, msg4)

# 没配接口时，流式与导图都必须在下发请求之前就回绝 —— 这是「不问就不发」那句承诺。
got = []
okA, msgA = ui_server.ai_summary_stream("clip_AITEST", "brief", "", got.append)
chk("ai_summary_stream：没配 AI 接口时回的是配置提示，而且一个字都没往外发",
    okA is False and "还没配 AI 接口" in msgA and not got, (msgA, got))
okM, outM = ui_server.ai_mindmap("clip_AITEST")
chk("ai_mindmap：没配接口时同样先挡下来，回 {msg} 而不是抛",
    okM is False and "还没配" in outM.get("msg", ""), outM)
okM2, outM2 = ui_server.ai_mindmap("SE_AITEST")
chk("ai_mindmap：读书没带章节 → 挡在配置检查之前，说清要翻到某一章",
    okM2 is False and "当前这一章" in outM2.get("msg", ""), okM2)

# ── H2. 流式收尾 ───────────────────────────────────────────────
# 真机套件栽过一次：上游半路拔线，界面却写着「小结好了 · 已存进这本书的 _ai/」，
# 半截内容还把上一次那份好的覆盖掉了。根因在 agent_deltas —— 它分不清「答完了」
# 和「连接没了」。实测（本地起了个真 socket 假上游试过）拔线不抛异常，urllib 把
# EOF 读成安静结束，所以唯一的判据是收尾块。这里不必联网，直接喂字节行就同形。
def sse_lines(pieces, done_block=True, finish_reason=False):
    """拼一段上游 SSE。done_block 决定发不发 data: [DONE]；
    finish_reason 决定最后一片带不带 finish_reason（有些中转站只发这个、不发 [DONE]）。"""
    out = []
    for i, p in enumerate(pieces):
        ch = {"index": 0, "delta": {"content": p}}
        if finish_reason and i == len(pieces) - 1:
            ch["finish_reason"] = "stop"
        out.append(("data: " + json.dumps({"choices": [ch]}) + "\n\n").encode("utf8"))
    if done_block:
        out.append(b"data: [DONE]\n\n")
    return out


got, blew = [], None
try:
    for piece in ui_server.agent_deltas(sse_lines(["半句", "，还剩半句"], done_block=False)):
        got.append(piece)
except ui_server.AgentStreamCut as e:
    blew = str(e)
chk("上游半路拔线（[DONE] 和 finish_reason 都没见过）必须抛，而不是安静收束",
    blew is not None, got)
chk("抛之前那半截已经一段段递出去了（屏幕上得留着，一个字不剩只让人以为按钮坏了）",
    got == ["半句", "，还剩半句"], got)
chk("断流那句说人话，并且当场讲清这次没存",
    bool(blew) and "断了" in blew and "没存" in blew, blew)
chk("标准收尾 data: [DONE] 不许被当成断了",
    "".join(ui_server.agent_deltas(sse_lines(["一句", "两句"]))) == "一句两句")
chk("只发 finish_reason、不发 [DONE] 的中转站也算答完（误判会把能用的人挡在门外）",
    "".join(ui_server.agent_deltas(sse_lines(["一句", "两句"], done_block=False,
                                            finish_reason=True))) == "一句两句")
dirty = [b": ping\n\n", b"data: not-json-at-all\n\n"] + sse_lines(["正文"], done_block=False)
got2 = []
try:
    for piece in ui_server.agent_deltas(dirty):
        got2.append(piece)
except ui_server.AgentStreamCut:
    pass
chk("脏行照旧跳过：一行脏数据不许打断整段对话（断流判定只认收尾块）", got2 == ["正文"], got2)

# ── I. 接线 ──────────────────────────────────────────────────
SRV = read_repo("ui_server.py")
HTML = read_repo("ui.html")
BUILD = read_repo("shell/build_macos.sh")

chk("接线：流式口与缓存口都在（GET 读上次那份、POST 现问）",
    'if path == "/api/ai/summary"' in SRV and 'if u.path == "/api/ai/summary"' in SRV
    and 'if u.path == "/api/ai/mindmap"' in SRV)
POST = SRV[SRV.index('if u.path == "/api/ai/summary"'):SRV.index('if u.path == "/api/ai/mindmap"')]
chk("接线：「没配接口」这类话必须在 SSE 开始前回完（一转成流就改不了状态码）",
    "还没配 AI 接口" in POST and POST.index("还没配 AI 接口") < POST.index("self._sse_start()"),
    POST[:60])
chk("接线：只有问到内容才落盘（半路失败时留着上一次那份，比写个空文件好）",
    "if not text:" in SRV and SRV.index("if not text:") < SRV.index("ai_sum.save_result"))
chk("接线：流式口落盘排在 agent_deltas 之后（断流一抛，save_result 就整个跳过）",
    "agent_deltas" in POST and POST.index("agent_deltas") < POST.index("ai_sum.save_result"),
    POST[:60])
chk("接线：前端认的流类型就是后端发的那种（后端发 x-ndjson，前端比 event-stream "
    "就等于整层小结永远等不到）",
    "application/x-ndjson" in SRV and "x-ndjson" in HTML
    and "indexOf('event-stream')" not in HTML)
chk("接线：接口传来的模式先过白名单（不认的一概退回归纳，不许拿它当文件名）",
    SRV.count("if style not in ai_sum.STYLE_KEYS") >= 2)
chk("接线：导图存完顺手回一张 SVG（用户要的是能直接看的图，不只是一棵树）",
    'out["svg"] = mindmap.render_svg(out["doc"])' in SRV)
chk("接线：ai_sum.py 在进包清单里（漏了它，装完点小结就是 ModuleNotFoundError）",
    "\n  ai_sum.py\n" in BUILD)
chk("界面：入口只有一枚「小结」（三种内容共用一个按钮，不各摆一个）",
    HTML.count('id="rdSum"') == 1 and "aiSumOpen()" in HTML)
want = "const AI_STYLES = [" + ", ".join(
    "['%s', '%s']" % (k, ai_sum.STYLE_NAME[k]) for k in ai_sum.STYLE_KEYS) + "];"
chk("界面：两枚芯片的键名、顺序与中文称号和后端 STYLE_KEYS/STYLE_NAME 完全一致"
    "（不一致就是选了没换、或界面上多出一枚后端不认的芯片）", want in HTML, want)
chk("界面：芯片说明各自成话（用户得知道这两种模式差在哪）",
    "学习理解" in HTML and "拆开讲透" in HTML and "只留骨架" in HTML)
chk("界面：流式回话按帧合并渲染（每来一段就重排整篇 Markdown 会卡）",
    "requestAnimationFrame" in HTML.split("async function sumRun")[1][:2600])
chk("界面：半路断了的字留在屏幕上（一个字不剩，用户只当按钮坏了）",
    "断了 · 这次没存" in HTML)
chk("界面：画完导图要先收掉小结层再开脑图层（两层同 z-index，不关就盖住）",
    HTML.split("async function aiSumShowMap")[1].find(
        "$sum('Layer').classList.remove('open')")
    < HTML.split("async function aiSumShowMap")[1].find("ntMapLayer"))
chk("界面：存为笔记也要先收掉小结层再开编辑器（真机栽过：保存那颗钮被小结层盖住点不动）",
    HTML.split("function aiSumNote")[1].find(
        "$sum('Layer').classList.remove('open')")
    < HTML.split("function aiSumNote")[1].find("ntEdOpen(null)"))
chk("界面：换书 / 换屏时把上一本书的浮层收掉（真机栽过：这五层挂在 body 上，"
    "旧书的小结盖在新书上面，那颗「小结」点下去只会撞在它）",
    "function rdLayersTuck() {" in HTML
    and HTML.count("rdLayersTuck()") >= 3        # 定义一次，openReader 与切屏各调一次
    and HTML.index("function rdLayersTuck") < HTML.index("async function openReader"))
chk("界面：收浮层时编辑器里那篇还没存的先落盘（不能为了收层把用户写的字静默丢掉）",
    HTML.split("function rdLayersTuck")[1].split("async function openReader")[0].find(
        "NTED.dirty")
    < HTML.split("function rdLayersTuck")[1].split("async function openReader")[0].find(
        "ntEdClose"))
chk("界面：AI 那层接上了 Esc 与点外关闭（开着的层收不掉，等于把阅读器锁死）",
    "aiSumClose()" in HTML and "sum = document.getElementById('aiSumLayer')" in HTML)
chk("零 emoji：ai_sum 的提示词里一个 emoji 都没有（模型会照着提示词的语气回，界面也跟着学）",
    not EMOJI.search(read_repo("ai_sum.py")))

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("AI 小结取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
