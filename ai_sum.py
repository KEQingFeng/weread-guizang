# -*- coding: utf-8 -*-
"""AI 小结的统一入口：一次读多少、怎么问、结果存哪儿。

读书、剪藏、视频这三条来路以前各有一套 AI 入口（右下角会聊、划词能译、点词查义），
但「把这一章 / 这一篇总结一下」这件事哪儿都没有 —— 用户想要的不是聊天，是读完。
这一轮把它收成一个模块、一个界面入口，三种内容共用。

两条硬口径是用户定的，写在这里而不是散在前端：
  · **范围**：读书一次只处理当前这一章（整本喂进去既烧钱又答得空），剪藏 / 视频 /
    订阅这类「本来就是一篇」的内容整篇读；
  · **模式**：学习理解（费曼那一路：讲透 + 最小执行步骤 + 逼用户输出一次）与
    归纳（把水分挤掉，只留能扫完的要点）。

本模块不碰网络、不读界面配置：正文由调用方喂进来，LLM 由调用方去问，落盘由调用方
给目录。于是它能在没起服务、没配 Key 的情况下被完整测一遍 —— 提示词长什么样、
范围怎么切、模型回的烂 JSON 怎么兜，都是纯逻辑。
"""

import json
import os
import re

# 两种总结模式（界面上的两枚芯片，键名同时是缓存文件名的一部分）
STYLES = (("learn", "学习理解"), ("brief", "归纳"))
STYLE_KEYS = [k for k, _ in STYLES]
STYLE_NAME = dict(STYLES)

# 「本来就是一篇」的那几路：整篇读。微信读书取回来的整本、自己导入的整本，
# 一次只读一章 —— 判据用 meta.source，目录前缀只是没 meta 时的兜底。
# flomo 也算整篇：一条笔记收成书之后就是「一篇几百字的东西」，拿它当一章去要求
# 章节参数，界面上点「总结」就会永远报「没选章节」。
WHOLE_MODULES = ("clip", "feed", "video", "flomo")

# 喂给模型的正文上限。章一般远小于它；全文那一路是视频长转写会顶到，
# 顶到就掐中段留首尾 —— 结论通常在结尾，掐尾巴比掐头更糟。
MAX_FEED_CHAPTER = 9000
MAX_FEED_WHOLE = 24000

# 导图的骨架约束：和界面上「这张图像不像话」的最低标准对齐
MIN_BRANCHES = 3
MAX_BRANCHES = 6
MAX_DEPTH = 4
MAP_LABEL = 24
MAP_LEAF = 40

CACHE_DIR = "_ai"


# ─────────────────────────── 范围 ───────────────────────────

def module_of(meta, book_id=""):
    """这本书属于哪一路：meta.source 说了算，没有就看目录前缀，最后才当微信读书。
    判据和 book_layout 同一套，但这里不 import 它 —— 总结不该跟着目录结构的重排
    一起动，两处口径靠 tests/check_ai_sum.py 钉住。
    """
    src = str((meta or {}).get("source") or "").strip()
    for key in ("clip", "feed", "video", "local", "weread", "flomo"):
        if src.startswith(key):
            return key
    bid = str(book_id or "")
    for prefix, key in (("clip_", "clip"), ("feed_", "feed"), ("video_", "video"),
                        ("imp_", "local"), ("flomo_", "flomo")):
        if bid.startswith(prefix):
            return key
    return "weread"


def scope_for(module, chapter=""):
    """这一次总结读多少：chapter（单章）还是 whole（整篇）。
    读书类没给章节就报空 —— 前端必须把「当前这一章」传过来，
    后端绝不擅自替用户挑一章（那是「总结得不对」的头号来源）。
    """
    ch = str(chapter or "").strip()
    if module in WHOLE_MODULES:
        return "whole", ""
    if not ch:
        return "", ""
    return "chapter", ch


def fit(text, limit):
    """超长正文掐中段留首尾，并明确告诉模型中间略过了 —— 不声不响地截，
    模型会把「结尾没有的东西」当成原文没有。"""
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    head = raw[:limit * 2 // 3]
    tail = raw[-(limit // 3):]
    return ("%s\n\n……（中间略去 %d 字，未喂给模型）……\n\n%s"
            % (head, len(raw) - len(head) - len(tail), tail))


def feed_limit(scope):
    return MAX_FEED_CHAPTER if scope == "chapter" else MAX_FEED_WHOLE


# ─────────────────────────── 提示词 ───────────────────────────

LEARN_SYS = (
    "你是学习教练，不是复读机。用户给你一段材料，你要帮他真正学会，而不是把材料"
    "换个说法再讲一遍。按下面五个小节输出 Markdown，小节标题原样使用，不要增删，"
    "不要开场白和收尾客套。\n"
    "## 一句话核心\n一句不超过 40 字的中文说清这段到底在讲什么，像讲给没读过的人听，"
    "不许用术语解释术语。\n"
    "## 术语对照\n把关键概念逐个拆开：「术语 → 用日常话说的意思」，最多 6 条，"
    "每条不超过 30 字；确实没有新术语就整节写「这段没有新术语」。\n"
    "## 讲透它\n挑最关键的两三个观点，各写一小段：它是什么 → 凭什么成立 → 一个具体的"
    "例子或类比。例子只能来自材料；材料没给例子就明写「材料没给例子」，再基于材料内的"
    "信息推演一次，不许引入外部事实。\n"
    "## 最小执行步骤\n把「学会它」拆成 3 到 5 个今天就能做完的动作，每步一句，"
    "动词开头，可勾选。\n"
    "## 检验你\n出 2 到 3 道自问自答式的题，每题下面留一行「我的回答：＿＿＿」，"
    "逼用户自己输出一次。\n"
    "硬约束：只许用材料里的事实，不得编造数据、人名、出处；材料撑不起某一节时直接写"
    "「材料里没有」，不要用套话填空。"
)

BRIEF_SYS = (
    "你是信息编辑。任务是把材料挤干水分，变成能一眼扫完的要点，不要复述铺垫、"
    "重复表述和口头禅。按这个格式输出 Markdown，不要开场白和收尾客套：\n"
    "第一行用一句不超过 40 字的话给出这段的核心结论；随后 3 到 8 条要点，一条一个信息，"
    "每条不超过 50 字，按「结论 → 依据 → 条件或风险」的顺序排；材料给过具体数字、"
    "时间、名称就照实引用，没给就不要自己补。内容是观点就写观点，是教程就写步骤，"
    "不要把教程压成一句「讲了怎么做」。"
    "硬约束：忠实原文，不得编造；判断不了的地方直接省略，不要用「可能」「似乎」蒙混。"
)

SYS_BY_STYLE = {"learn": LEARN_SYS, "brief": BRIEF_SYS}

SCOPE_NOTE = {
    "chapter": ("范围说明：下面是这本书的**其中一章**，不要替全书下结论，"
                "也不要写「本章」「这一节」这类话 —— 直接讲内容。"),
    "whole": "范围说明：下面是一篇完整内容，按整篇来总结。",
}

KIND_BY_MODULE = {
    "clip": "一篇剪藏下来的网页文章",
    "feed": "一条订阅源里的文章",
    "video": "一支视频转写出来的笔记",
    "flomo": "一条 flomo 便签（用户自己记的，短就三五字，长则几百字）",
    "local": "用户自己导入的一本书",
    "weread": "微信读书里取回来的一本书",
}

USER_TMPL = """{scope_note}
体裁：{kind}。

标题：
{title}

正文：
{text}
"""


def messages(style, scope, title, text, kind="weread"):
    """拼出这一次问的 messages。范围与模式都在这里定，前端不再自己写提示词 ——
    两处各写一套，改一处漏一处，界面上就变成「同一个按钮有时答得对不对」。"""
    key = style if style in SYS_BY_STYLE else "brief"
    body = fit(text, feed_limit(scope))
    user = USER_TMPL.format(
        scope_note=SCOPE_NOTE.get(scope, SCOPE_NOTE["whole"]),
        kind=KIND_BY_MODULE.get(kind, KIND_BY_MODULE["weread"]),
        title=str(title or "（没有标题）")[:200],
        text=body)
    return [{"role": "system", "content": SYS_BY_STYLE[key]},
            {"role": "user", "content": user}]


# ─────────────────────────── 思维导图 ───────────────────────────

MAP_SYS = (
    "你是知识结构化编辑，把一段材料重组成一棵真正的思维导图树：以概念、方法、例子、"
    "结论之间的关系为骨架，而不是把小标题换个层级重排。只输出一个 JSON 对象，"
    "不要代码围栏，不要任何解释。\n"
    "形状：{\"title\":字符串,\"root\":{\"label\":字符串,\"kids\":[{…}]}}, kids 递归。\n"
    "约束：root 的一级分支（theme）必须 %d 到 %d 个，彼此语义独立且合起来覆盖主要内容，"
    "禁止「其他」「补充」「更多内容」这类占位词，也禁止一个章节一个分支地平铺；"
    "整棵树最深 %d 层；分支名不超过 %d 字，末级节点不超过 %d 字，末级要具体到"
    "点开就能看懂；每个节点的 label 都必须有信息量。只许用材料里的事实。"
    % (MIN_BRANCHES, MAX_BRANCHES, MAX_DEPTH, MAP_LABEL, MAP_LEAF)
)

MAP_USER = """请把下面这篇内容画成一棵思维导图树，只返回 JSON。

标题：
{title}

正文：
{text}
"""


def map_messages(title, text, scope):
    return [{"role": "system", "content": MAP_SYS},
            {"role": "user", "content": MAP_USER.format(
                title=str(title or "（没有标题）")[:200],
                text=fit(text, feed_limit(scope)))}]


def _json_object(raw):
    """从模型输出里抠出最外层那个 JSON 对象。先去 think 标签和代码围栏，
    再按花括号配对取第一段完整对象 —— 模型爱在 JSON 前后加一句「好的」。"""
    text = str(raw or "")
    text = re.sub(r"<think[^>]*>.*?</think\s*>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _clean_node(raw, depth):
    """一个节点 → {"label","kids"}；坏数据丢其子树而不是丢整张图。"""
    if not isinstance(raw, dict):
        return None
    label = re.sub(r"\s+", " ", str(raw.get("label") or raw.get("text") or "")).strip()
    if not label:
        return None
    node = {"label": label[:MAP_LEAF * 2], "kids": []}
    if depth >= MAX_DEPTH:
        return node
    kids = raw.get("kids")
    if not isinstance(kids, list):
        kids = raw.get("children")
    if isinstance(kids, list):
        for k in kids:
            built = _clean_node(k, depth + 1)
            if built is not None:
                node["kids"].append(built)
    return node


def head_tree(title, text, limit=24):
    """材料本身的结构 → 一棵树：Markdown 标题当分支，标题下的段落首句当叶子。
    模型没答、答得不像树时就用它 —— 这张图至少是原文的目录，不是空框。"""
    root = {"label": (title or "这篇内容")[:MAP_LABEL], "kids": []}
    current = None
    for ln in str(text or "").splitlines():
        line = ln.strip()
        if not line:
            continue
        m = re.match(r"^(#{1,4})\s+(.+)$", line)
        if m:
            if len(root["kids"]) >= limit:
                continue
            current = {"label": re.sub(r"\s+", " ", m.group(2))[:MAP_LABEL], "kids": []}
            root["kids"].append(current)
        elif line.startswith(("-", "*", "·")) or re.match(r"^\d+[.)、]\s", line):
            leaf = re.sub(r"^[-*·]\s*|^\d+[.)、]\s*", "", line)[:MAP_LEAF]
            parent = (current or root)
            if len(parent["kids"]) < 8:
                parent["kids"].append({"label": leaf, "kids": []})
        elif current is not None and len(current["kids"]) < 4:
            current["kids"].append({"label": sentence(line)[:MAP_LEAF], "kids": []})
    return root


def sentence(text):
    """一句话的主干：取第一个句号 / 问号 / 感叹号之前的部分，没有就取前若干字。"""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    raw = re.sub(r"^#+\s*", "", raw)
    parts = re.split(r"[。！？!?]", raw)
    parts = [p for p in parts if p.strip()]
    return (parts[0] if parts else raw).strip()


def _top_branches(tree):
    return len((tree or {}).get("kids") or [])


def parse_map(raw, title="", text=""):
    """模型输出的导图 JSON → 干净的树。不合格时退回原文结构树，绝不抛。

    「不合格」有三种：不是 JSON / 没有 root / 一级分支少于 MIN_BRANCHES ——
    少于 3 条的图在界面上就是一条直线，用户会以为生成失败了，所以宁可用目录树兜。
    返回 (tree, 警告或空串)，警告是给界面看的人话。
    """
    obj = _json_object(raw)
    tree = None
    if isinstance(obj, dict):
        node = obj.get("root")
        if not isinstance(node, dict):
            nodes = obj.get("nodes")
            if isinstance(nodes, dict):
                node = nodes
        tree = _clean_node(node, 0)
        if tree is not None and str(obj.get("title") or "").strip():
            title = str(obj["title"]).strip() or title
    if tree is None or _top_branches(tree) < MIN_BRANCHES:
        fallback = head_tree(title, text)
        if _top_branches(fallback) >= MIN_BRANCHES:
            return (fallback if tree is None else _merge(tree, fallback)), (
                "模型给的导图分支太少，已改用这篇内容自己的小标题排" if tree is not None
                else "模型没给出导图，已按原文小标题排了一张")
        if tree is None:
            return fallback, "模型没能画出导图，先用原文结构顶一张"
    if _top_branches(tree) > MAX_BRANCHES:
        tree["kids"] = tree["kids"][:MAX_BRANCHES]
    return tree, ""


def _merge(tree, fallback):
    """分支不够时，把原文结构里没被模型覆盖到的分支补进去，凑到能看。"""
    have = {n["label"] for n in tree["kids"]}
    for k in fallback["kids"]:
        if _top_branches(tree) >= MIN_BRANCHES:
            break
        if k["label"] not in have:
            tree["kids"].append(k)
    return tree


# ─────────────────────────── 缓存 ───────────────────────────

def cache_name(scope, style, chapter=""):
    """结果存成什么名字：模式 + 范围 +（章时）章节文件名，一次一份，互不覆盖。
    章节名来自磁盘，形状先卡一遍，免得「../」混进文件名。章名本来就带 .md，
    先把尾缀摘掉再拼 —— 「brief-0003.md.md」这种双扩展名在用户的文件夹里像坏了。"""
    key = style if style in STYLE_KEYS else "brief"
    if scope == "chapter":
        ch = re.sub(r"\.md\Z", "", str(chapter or "").strip())
        ch = re.sub(r"[^A-Za-z0-9._-]", "_", ch)[:80]
        return "%s-%s.md" % (key, ch)
    return "%s-whole.md" % key


def cache_dir(book_dir):
    return os.path.join(str(book_dir or ""), CACHE_DIR)


def cache_path(book_dir, scope, style, chapter=""):
    return os.path.join(cache_dir(book_dir), cache_name(scope, style, chapter))


def save_result(book_dir, scope, style, chapter, text):
    """把总结存进这本书的 _ai/ 里。写不动也不抛 —— 存盘失败不该让用户白等一场，
    屏幕上那段话还在。"""
    path = cache_path(book_dir, scope, style, chapter)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(text or ""))
        return path
    except Exception:
        return ""


def load_result(book_dir, scope, style, chapter=""):
    """读上次存下来的那份；没有或读不动回空串。"""
    path = cache_path(book_dir, scope, style, chapter)
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    except Exception:
        return ""
    return ""
