#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频转笔记那条 LLM 读口收口：上游回「合法 JSON 但不是对象」不许带走整趟流水线。

故障长什么样
    `video_note.llm_chat` 收的是外站（OpenAI 兼容网关 / 中转站）的
    /chat/completions 回包。对方回的正文形状没人保证，下面这几类都是**合法 JSON**：

        {"choices": ["oops"]}                       # choices 里塞了字符串
        {"choices": [{"message": "hi"}]}            # message 是字符串不是对象
        {"choices": [{"message": {"content": [1, {"text": "甲"}]}}]}
        data: []                                    # SSE 片段里 choices 是空表
        data: 0 / data: "ping"                      # SSE 片段根本不是对象

    旧写法两条路都是裸的 `.get` 链：

        message = (choices[0] or {}).get("message") or {}
        for ch in (item.get("choices") or []):
            pieces.append(str(((ch or {}).get("delta") or {}).get("content") or ""))

    上面任一形状都会 AttributeError（`str` / `list` / `int` 没有 `.get`），而这两段
    都在 `urlopen` 那个 try **之外** —— 异常直接穿出 `llm_chat`，调用方只接 ValueError，
    于是整趟「视频转笔记」带着英文报错结束：用户看到的是「点了没反应 / 失败了但看不懂」，
    而不是本就说好的那句「认不出来 / 这次没说话」。

收口
    server 那边 `ui_server.choice_piece` / `completion_text` / `agent_deltas` 早就是这
    把尺子（非表当空、`choices` 不是列表当空表、`content` 分段数组拼起来、单条形状不对
    只跳过它自己）；同一个文件里的 `_post_audio` 也用 `isinstance(payload, dict)` 兜过
    一回。这一条把 `llm_chat` 的两个读口接到同一把尺子上：`_choices_of` + `_choice_text`
    + `_content_text`。

这一条钉四层：
    A 纯函数：`_choice_text` / `_content_text` 认干净形状、脏形状回空串、不抛；
    B `_llm_content`：顶层 / choices / message 各级脏形状都不抛，干净值一字不改；
    C 端到端：假 urlopen 喂一组脏回包（含 SSE），`llm_chat` 只许回正文或抛 ValueError
      （人话），绝不许把 AttributeError 漏出去；干净回包照旧回正文；
    D 接线：源码里那两个读口走 `_choices_of` / `_choice_text`，旧的裸 `.get` 链不再出现。

全程离线：假 urlopen 顶掉网络，数据目录指进临时沙盒。归 --fast 静态段。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录先指进临时沙盒再 import video_note：免得它在用户真实 cache/ 里建目录。
SB = pathlib.Path(tempfile.mkdtemp(prefix="guizang-vidllm-"))
os.environ["GUIZANG_DATA"] = str(SB / "cache")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import video_note as vn  # noqa: E402

FAILED = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:400]))
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)


def val(fn):
    """跑一次，异常原样返回（测试自己要判「抛的是什么类型」）。"""
    try:
        return fn()
    except Exception as exc:   # noqa: BLE001 —— 就是要看异常类型
        return exc


# ── 对照：旧写法确实会抛，证明收口不是空转 ───────────────────────────
def _old_llm_content(payload):
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = (choices[0] or {}).get("message") or {}
        text = message.get("content")
        if isinstance(text, list):
            text = "".join(str((c or {}).get("text") or "") for c in text)
        if text:
            return str(text)
    return ""


def _old_sse_pieces(item):
    out = []
    for ch in (item.get("choices") or []):
        out.append(str(((ch or {}).get("delta") or {}).get("content") or ""))
    return out


# ── A. 纯函数 _content_text / _choice_text ────────────────────────────
ct = getattr(vn, "_content_text", None)
cx = getattr(vn, "_choice_text", None)
chk("读口收口 _content_text 存在", callable(ct))
chk("读口收口 _choice_text 存在", callable(cx))

if callable(ct):
    chk("A1 content 是字符串原样吐", ct({"content": "正文"}) == "正文")
    chk("A2 content 分段数组拼起来", ct({"content": [{"text": "甲"}, {"text": "乙"}]}) == "甲乙")
    chk("A3 content 数组里的脏块只跳过自己，好块照拼",
        ct({"content": [1, "x", {"text": "甲"}, None, {"text": "乙"}]}) == "甲乙",
        ct({"content": [1, "x", {"text": "甲"}, None, {"text": "乙"}]}))
    chk("A4 容器不是对象回空串（不抛）",
        val(lambda: ct("hi")) == "" and val(lambda: ct([])) == "")
    chk("A5 content 是 0 / 空串照旧当没有", ct({"content": 0}) == "" and ct({"content": ""}) == "")
    chk("A6 干净 dict 不被改", (lambda d: (ct(d), d) == ("hi", {"content": "hi"}))({"content": "hi"}))

if callable(cx):
    chk("A7 delta 优先", cx({"delta": {"content": "流"}, "message": {"content": "整"}}) == "流")
    chk("A8 没有 delta 退到 message", cx({"message": {"content": "整"}}) == "整")
    chk("A9 delta 是空表/空串时退到 message",
        cx({"delta": {}, "message": {"content": "整"}}) == "整")
    chk("A10 一条 choices 不是对象回空串", val(lambda: cx(["oops"])) == "")
    chk("A11 message 是字符串回空串（不抛）", val(lambda: cx({"message": "hi"})) == "")
    chk("A12 delta 是字符串回空串（不抛）", val(lambda: cx({"delta": "x"})) == "")
    chk("A13 干净值一字不改", cx({"delta": {"content": "你好"}}) == "你好")

# ── B. _llm_content 各级脏形状 ────────────────────────────────────────
li = getattr(vn, "_llm_content", None)
chk("读口收口 _llm_content 存在", callable(li))
if callable(li):
    good = {"choices": [{"message": {"content": "正文"}}]}
    chk("B1 干净回包照旧取正文", li(good) == "正文")
    chk("B2 干净回包取过之后一个字不改", li(good) == "正文" and good ==
        {"choices": [{"message": {"content": "正文"}}]})
    chk("B3 content 分段数组照旧拼", li({"choices": [{"message": {"content":
        [{"type": "text", "text": "甲"}, {"text": "乙"}]}}]}) == "甲乙")
    chk("B4 choices 里是字符串不抛，回空串",
        val(lambda: li({"choices": ["oops"]})) == "")
    chk("B5 message 是字符串不抛，回空串",
        val(lambda: li({"choices": [{"message": "hi"}]})) == "")
    chk("B6 choices 不是列表回空串",
        val(lambda: li({"choices": "oops"})) == "" and li({"choices": {"a": 1}}) == "")
    chk("B7 顶层不是对象回空串", li([]) == "" and li("hi") == "" and li(None) == "")
    chk("B8 choices 空表回空串", li({"choices": []}) == "")
    # 对照：同样的输入，旧写法确实抛 AttributeError（证明这几条不是白测）
    old_raises = [val(lambda p=p: _old_llm_content(p)) for p in
                  ({"choices": ["oops"]}, {"choices": [{"message": "hi"}]},
                   {"choices": [{"message": {"content": [1, {"text": "甲"}]}}]})]
    chk("B9 对照：旧写法对这几组脏形状确实抛 AttributeError",
        all(isinstance(e, AttributeError) for e in old_raises),
        [type(e).__name__ for e in old_raises])

# ── C. 端到端 llm_chat：假 urlopen ────────────────────────────────────
CFG = {"url": "http://127.0.0.1:9/v1", "model": "fake-model", "key": "k"}


class FakeResp:
    def __init__(self, raw):
        self._raw = raw

    def read(self, *_a):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


_ORIG_URLOPEN = urllib.request.urlopen


def with_raw(raw):
    """把 llm_chat 的 urlopen 换成一个固定回包的假货，返回 (结果或异常)。"""
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    urllib.request.urlopen = lambda *_a, **_kw: FakeResp(payload)
    try:
        return val(lambda: vn.llm_chat(CFG, [{"role": "user", "content": "hi"}], timeout=1))
    finally:
        urllib.request.urlopen = _ORIG_URLOPEN


CASES = [
    # (说明, 回包, 期望：字符串 = 正文；ValueError = 抛人话)
    ("干净 JSON 回正文", '{"choices":[{"message":{"content":"正文"}}]}', "正文"),
    ("干净 JSON 的分段 content 拼起来",
     '{"choices":[{"message":{"content":[{"text":"甲"},{"text":"乙"}]}}]}', "甲乙"),
    ("choices 里是字符串：人话不是 AttributeError", '{"choices":["oops"]}', ValueError),
    ("message 是字符串：人话不是 AttributeError", '{"choices":[{"message":"hi"}]}', ValueError),
    ("message.content 是越界/空值：人话不是 AttributeError",
     '{"choices":[{"message":{"content":0}}]}', ValueError),
    ("顶层是数组：人话", '[]', ValueError),
    ("SSE：脏行只跳过、好的照收",
     'data: []\ndata: 0\ndata: "ping"\n'
     'data: {"choices":["oops"]}\n'
     'data: {"choices":[{"delta":{"content":"好"}}]}\ndata: [DONE]\n', "好"),
    ("SSE：只有脏行时回人话",
     'data: []\ndata: 0\ndata: {"choices":[{"delta":"x"}]}\ndata: [DONE]\n', ValueError),
    ("SSE：非流式网关回 message 也认",
     'data: {"choices":[{"message":{"content":"整段"}}]}\ndata: [DONE]\n', "整段"),
    ("SSE：半个 JSON 行继续跳过，好的照收",
     'data: {"choices":[{"delta":{"content":"前"}}]}\n'
     'data: {"choices":\ndata: [DONE]\n', "前"),
]

for label, raw, want in CASES:
    got = with_raw(raw)
    if want is ValueError:
        ok = isinstance(got, ValueError) and not isinstance(got, AttributeError)
    else:
        ok = got == want
    chk("C " + label, ok, got)

# 每条脏回包都不许把 AttributeError 漏出来（这正是本次要收的口）
leaked = [label for label, raw, _want in CASES if isinstance(with_raw(raw), AttributeError)]
chk("C 十条里没有一条漏出 AttributeError", not leaked, leaked)

# 对照：旧写法对同样几组脏回包确实抛 AttributeError
old_leak = []
for payload in ({"choices": ["oops"]}, {"choices": [{"message": "hi"}]}):
    try:
        _old_llm_content(payload)
    except AttributeError:
        old_leak.append(payload)
for item in ([], 0, "ping", {"choices": ["oops"]}, {"choices": [{"delta": "x"}]}):
    try:
        _old_sse_pieces(item)
    except AttributeError:
        old_leak.append(item)
chk("C 对照：旧写法确实漏 AttributeError（收口非空转）", len(old_leak) >= 6, old_leak)

# ── D. 接线：两个读口走同一把尺子，旧裸链不再出现 ────────────────────
SRC = (REPO / "video_note.py").read_text(encoding="utf-8")
chk("D1 源码里有 _choices_of / _choice_text 两把尺子",
    "def _choices_of(" in SRC and "def _choice_text(" in SRC)
chk("D2 _llm_content 走 _choices_of / _content_text",
    "_choices_of(payload)" in SRC and "return _content_text(ch.get(\"message\"))"
    in SRC)
chk("D3 SSE 那半段走 _choices_of / _choice_text",
    "for ch in _choices_of(item):" in SRC and "piece = _choice_text(ch)" in SRC)
chk("D4 旧的裸 .get 链不再出现（choices[0] 那句）",
    '(choices[0] or {}).get("message")' not in SRC)
chk("D5 旧的裸 .get 链不再出现（SSE 那句）",
    'for ch in (item.get("choices") or [])' not in SRC
    and '((ch or {}).get("delta") or {}).get("content")' not in SRC)


shutil.rmtree(SB, ignore_errors=True)
print("\n视频转笔记 LLM 回包形状门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  ✗ " + f)
sys.exit(1 if FAILED else 0)
