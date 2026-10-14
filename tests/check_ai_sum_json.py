# -*- coding: utf-8 -*-
"""AI 小结：模型回话里那段真 JSON 不能因为前面一句带花括号的说明被整个丢掉。

`ai_sum._json_object` 原来只找**第一对配平的花括号**，json.loads 解不动就直接
return None。可模型很爱在 JSON 前写一句带花括号的话，比如

    好的，我先梳理一下 {书名 / 作者 / 三条主线}，结果如下：
    {"root": {"label": "…", "kids": [...]}}

那对花括号里是中文说明、不是 JSON，于是整段合格的导图被判成「模型没给出导图」，
`parse_map` 退回原文目录树，界面上还挂一句「模型没给出导图」。这一条把「逐段试到
第一段解得开的对象」钉死，同时守住老口径：半截未闭合的不硬猜、字符串里的花括号
不捣乱、think / 围栏照摘。

全程纯函数 + 离线：不 import ui_server、不起服务、不联网。归 --fast 静态段。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-aisum-json-")
os.environ.setdefault("GUIZANG_DATA", os.path.join(ROOT, "cache"))
os.environ.setdefault("GUIZANG_BOOKS", os.path.join(ROOT, "books"))

import ai_sum                                                # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


J = ai_sum._json_object

# ── A 老口径（回归）：正常 JSON / 散文前后缀 / think / 围栏 ─────────────────
chk("裸对象解得开", J('{"a": 1}') == {"a": 1})
chk("JSON 前后加一句不带花括号的话仍认得",
    J('好的，结果如下：\n{"a": 1}\n以上。') == {"a": 1})
chk("think 标签整段摘掉再取 JSON",
    J('<think>先想 {一下} 再答</think>{"a": 1}') == {"a": 1})
chk("代码围栏照摘",
    J('```json\n{"a": 1}\n```') == {"a": 1})
chk("字符串里的 { } 不参与配对",
    J('{"label": "这里有 } 和 { 两个花括号"}') == {"label": "这里有 } 和 { 两个花括号"})
chk("字符串里转义的双引号不提前收尾",
    J('{"label": "他说 \\"然后 }\\" 就停了"}') == {"label": '他说 "然后 }" 就停了'})
chk("半个 JSON（没闭合）回 None：不硬猜",
    J('{"root": {"label": "断在这里"') is None)
chk("一个花括号都没有回 None", J("plain text, no braces") is None)

# ── B 本次要钉的：前面那对解不开的花括号不许把后面的真 JSON 带走 ─────────────
chk("开场白里一对解不开的花括号之后仍有真 JSON → 取真 JSON",
    J('好的，先看 {书名 / 作者 / 三条主线}，结果如下：\n'
      '{"root": {"label": "书名", "kids": []}}')
    == {"root": {"label": "书名", "kids": []}})
chk("连着好几段解不开的花括号也照样往后找",
    J('{一段} {又一段} {还是一段} {"a": 1}') == {"a": 1})
chk("前后都是解不开的说明，只认中间那段真 JSON",
    J('{开头说明} {"a": 1} {结尾说明}') == {"a": 1})
chk("全都是解不开的花括号才回 None",
    J('{一段} {又一段} 没了') is None)
chk("开场就是没闭合的花括号，后面的真 JSON 仍捞得回来",
    J('{半截没关…… {"a": 1}') == {"a": 1})
chk("两段都解得开时取靠前的那个（不跳序）",
    J('{"a": 1} 中间话 {"b": 2}') == {"a": 1})
chk("think 里的花括号被摘掉，正文 JSON 不受影响",
    J('<think>{一段推理}</think>{"a": 1}') == {"a": 1})

# ── C 下游 parse_map：模型给的导图真的被用上，而不是被兜底盖掉 ───────────────
MODEL_MAP = ('{"root": {"label": "这本书", "kids": ['
             '{"label": "主线一", "kids": [{"label": "要点", "kids": []}]},'
             '{"label": "主线二", "kids": []},'
             '{"label": "主线三", "kids": []}]}}')
PROSE = ("# 第一章\n讲了第一件事。\n\n# 第二章\n讲了第二件事。\n\n"
         "# 第三章\n讲了第三件事。\n\n# 第四章\n讲了第四件事。\n")

tree, warn = ai_sum.parse_map("好的，先看 {书名 / 主线}，结果如下：\n" + MODEL_MAP,
                              title="书名", text=PROSE)
chk("带花括号开场白的模型导图被完整用上（3 条主线都在）",
    [k["label"] for k in (tree or {}).get("kids", [])] == ["主线一", "主线二", "主线三"],
    tree)
chk("用上模型导图时不报「模型没给出导图」", warn == "", warn)

tree2, warn2 = ai_sum.parse_map("模型这轮什么都没答", title="书名", text=PROSE)
chk("模型没有任何 JSON 时仍退回原文目录树，并出声说明",
    len((tree2 or {}).get("kids", [])) >= 3 and warn2 != "", (tree2, warn2))

print()
print("AI 小结 JSON 抠取：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    print("未过：" + "、".join(FAIL))
sys.exit(1 if FAIL else 0)
