#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写作 AI 那三张「往下写的方向」卡，与喂给模型的素材，都是纯函数在管。

为什么单独一份（离线、不 import 服务、不联网）：这两处毛病都不报错，只是
「悄悄少一块」，正是这一仓最容易栽的那类：

  A `parse_sparks` 把方向行收成卡片时，旧正则 `^[-*+•\\d]+[.)、:：]?\\s*`
    把「项目符号」和「数字」混成一个可选分隔符的字符类 —— 任何以数字开头的方向
    都会被啃掉开头那一截。方向是给人看的，「2024 年的数据说明了什么」里的年份、
    「3 个能立刻用的方法」里的数目就是信息本身，不该当成列表编号吃掉。
  B `material_block` 夹素材时，头一条自己就超过上限会直接 break：keep 还是空的，
    整段素材被丢成 ""，用户挂着便签，模型却一个字都没收到 —— 与它自己文档里
    「夹到 limit 字」的说法正相反。

钉死的口径：
  A1 真的列表标记照旧摘掉（- * • / 1. 1、1) 1: 1：），`**加粗**` 与引号照旧清掉
  A2 数字开头的正文一个字不许少（年份 / 数目 / 小数 / 时刻 / 5G）
  A3 去重、限长 40、最多 n 条、空行跳过 —— 出口仍然严
  B1 单独一条超限要夹一段带上，不再是空串；总长仍受 limit 约束
  B2 正常多条按顺序拼、抬头与「- 」前缀还在；全空才回空串
  B3 续写 / 灵感 / 润色三条提示词都吃得到这段素材（接线，不是只看函数本身）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import writer  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


# ── A1. 真的列表标记照旧摘掉 ───────────────────────────────
for line, want in [
    ("- 从一个具体场景切入", "从一个具体场景切入"),
    ("-方向不带空格", "方向不带空格"),
    ("* 换个叙述者", "换个叙述者"),
    ("• 先写结果", "先写结果"),
    ("1. 先写人物动机", "先写人物动机"),
    ("12. 两数编号也认", "两数编号也认"),
    ("2、从小到大", "从小到大"),
    ("3) 第三种写法", "第三种写法"),
    ("4: 时间线", "时间线"),
    ("5：视角", "视角"),
    ("**方向**：从反面写", "方向：从反面写"),
    ("「换个叙述者」", "换个叙述者"),
]:
    got = writer.parse_sparks(line, 3)
    chk("A1 摘标记：" + repr(line) + " → " + repr(want), got == [want], got)

# ── A2. 数字开头的正文一个字不许少（旧代码在这里啃掉开头） ──
for line in [
    "2024 年的数据说明了什么",
    "3 个能立刻用的方法",
    "5G 时代的信息密度",
    "10 分钟后主角醒来",
    "3.14 是圆周率",
    "1:30 的节奏",
]:
    got = writer.parse_sparks(line, 3)
    chk("A2 数字是内容，不是编号：" + repr(line), got == [line], got)

chk("A2 项目符号后面的数字要留着（只摘 '- '）",
    writer.parse_sparks("- 3 个转折点", 3) == ["3 个转折点"],
    writer.parse_sparks("- 3 个转折点", 3))

# ── A3. 出口仍然严：去重 / 限长 / 最多 n 条 / 空行 ──────────
multi = writer.parse_sparks("第一个方向\n第一个方向\n第二个方向\n\n第三个方向\n第四个方向", 3)
chk("A3 去重后按出现顺序、最多 n 条",
    multi == ["第一个方向", "第二个方向", "第三个方向"], multi)
chk("A3 n=1 只回一条", len(writer.parse_sparks("甲方向\n乙方向", 1)) == 1)
long_line = "方" * 60
capped = writer.parse_sparks(long_line, 3)
chk("A3 单条卡片文字夹到 40 字", capped == ["方" * 40] and len(capped[0]) == 40, capped)
chk("A3 全是空行回空表", writer.parse_sparks("\n\n   \n", 3) == [])
chk("A3 非字符串输入不炸", writer.parse_sparks(None, 3) == [])

# ── B1. 素材夹取：单独一条超限不再整段丢 ────────────────────
big = "素" * 7000
blk = writer.material_block([big], 6000)
chk("B1 单独一条超限时不再回空串", blk != "", repr(blk)[:80])
chk("B1 抬头还在、开头那段内容进得去",
    blk.startswith("〔作者的笔记素材") and "素" in blk, repr(blk)[:80])
chk("B1 正文夹到 limit，不把 7000 字整段塞进去", len(blk) <= 6000 + 64, len(blk))
exact = writer.material_block(["素" * 6000], 6000)
chk("B1 正好卡在上限的整条要完整留着",
    exact.endswith("素" * 6000) and ("素" * 6000) in exact, len(exact))

# ── B2. 正常多条 / 空输入 ──────────────────────────────────
two = writer.material_block(["第一条素材", "第二条素材"], 6000)
chk("B2 抬头 + 每条一个「- 」前缀、按给的顺序",
    two == "〔作者的笔记素材，可作事实与角度来源，不要照抄原句〕\n- 第一条素材\n- 第二条素材",
    two)
chk("B2 空条目跳过，全空才回空串",
    writer.material_block(["", "  ", None], 6000) == ""
    and writer.material_block([], 6000) == "")
kept = writer.material_block(["a" * 4000, "b" * 4000], 6000)
chk("B2 放不下的不牵连已经收下的（第一条留下）",
    "a" * 4000 in kept and "b" * 4000 not in kept, len(kept))

# ── B3. 接线：三条提示词都吃得到这段素材 ────────────────────
spark = writer.spark_messages("正文写到这里", "素材" * 2000, (), 3)[1]["content"]
chk("B3 灵感提示词里带着素材抬头与内容",
    "〔作者的笔记素材" in spark and "素材素材" in spark, spark[-60:])
cont = writer.cont_messages("前文", "素材" * 2000, "para", "")[0][1]["content"]
chk("B3 续写提示词里带着素材抬头", "〔作者的笔记素材" in cont, cont[-60:])
polish = writer.polish_messages("选中这一段", "smooth", "素材" * 2000)[1]["content"]
chk("B3 润色提示词里带着素材抬头", "〔作者的笔记素材" in polish, polish[-60:])

print()
print("写作方向卡与素材夹取：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
