# -*- coding: utf-8 -*-
"""小 Agent：配好之后，上游那几种「不标准」的回法都不许把它变成「没配 / 断线」。

用户点名的是一条：对原先设置的 Agent 检查问题并修复。查下来真会咬人的不是配置存不下，
而是**形状**：兼容 OpenAI 的称呼听着统一，各家回话却有好几副面孔 ——

  * 收了 stream:true 仍整段 JSON 一次回（不带 `data:` 前缀）；
  * SSE 里给 `message.content` 而不是 `delta.content`；
  * 明明 stream:false，却按 SSE 一坨回来。

这三种在旧代码里分别被当成「断线」「一个字都没回」「Expecting value」。用户看着满屏
好好的答案被判成断线，或者「测一下」永远测不通 —— 所以这一套按形状逐条钉死：
凡是有正文的，都必须正常吐出来、正常收尾。

  A 地址补全 —— 少 /v1、少 /chat/completions 都补齐；填全的原样；查询串不被顶掉。
  B 流式形状 —— 标准 SSE / 只给 finish_reason / 整段 JSON / message 形 都算答完；
                 真半截（两种收尾都没见过）仍必须抛 AgentStreamCut。
  C 非流式抠字 —— completion_text 认 JSON 也认 SSE，认不出回空串而不是抛。
  D 配置读写 —— 只改模型不逼重贴 Key；清空地址＝关；Key 只回末四位。
  E 接线 —— 保存 / 测试 / 两个问答口都在，且顺序仍守着「没配先在 SSE 前挡掉」。

全程离线、不开浏览器：SSE 直接喂字节行，配置读写全在临时沙盒。
用法: .venv/bin/python tests/check_agent.py
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-agent-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import ui_server                                             # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def read_repo(fname):
    return io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                fname), encoding="utf-8").read()


# ── A. 地址补全 ─────────────────────────────────────────────────
F = ui_server.agent_url_full
chk("地址：只填域名 → 补 /v1/chat/completions",
    F("https://api.x.com") == "https://api.x.com/v1/chat/completions")
chk("地址：只填 /v1 → 补 /chat/completions",
    F("https://api.x.com/v1") == "https://api.x.com/v1/chat/completions")
chk("地址：末尾有多余斜杠也补齐（不叠成 //v1）",
    F("https://api.x.com/v1/") == "https://api.x.com/v1/chat/completions")
chk("地址：填全了原样返回（不改写用户给的路子）",
    F("https://api.x.com/v1/chat/completions") == "https://api.x.com/v1/chat/completions")
chk("地址：查询串留在末尾，不被 /v1 顶掉（Azure 那种 ?api-version=…）",
    F("https://x.openai.azure.com/openai/deployments/d/chat/completions?api-version=2024-02-01")
    == "https://x.openai.azure.com/openai/deployments/d/chat/completions?api-version=2024-02-01")
chk("地址：/v1?x=1 这种也要补齐，且查询串跟在新路径后面",
    F("https://api.x.com/v1?k=1") == "https://api.x.com/v1/chat/completions?k=1")
chk("地址：不是 http(s) 一律拒（不许把 ftp / file 填进来）",
    F("ftp://x/y") == "" and F("javascript:alert(1)") == "")


# ── B. 流式形状 ─────────────────────────────────────────────────
def sse(pieces, done_block=True, finish_reason=False, shape="delta"):
    out = []
    for i, p in enumerate(pieces):
        if shape == "delta":
            ch = {"index": 0, "delta": {"content": p}}
        else:
            ch = {"index": 0, "message": {"role": "assistant", "content": p}}
        if finish_reason and i == len(pieces) - 1:
            ch["finish_reason"] = "stop"
        out.append(("data: " + json.dumps({"choices": [ch]}) + "\n\n").encode("utf8"))
    if done_block:
        out.append(b"data: [DONE]\n\n")
    return out


def collect(lines):
    got, blew = [], None
    try:
        for piece in ui_server.agent_deltas(lines):
            got.append(piece)
    except ui_server.AgentStreamCut as e:
        blew = str(e)
    return got, blew


chk("流式：标准 SSE（data + [DONE]）正常吐字",
    "".join(ui_server.agent_deltas(sse(["一句", "两句"]))) == "一句两句")
chk("流式：只给 finish_reason、不给 [DONE] 也算答完",
    "".join(ui_server.agent_deltas(sse(["一句", "两句"], done_block=False,
                                       finish_reason=True))) == "一句两句")
got, blew = collect(sse(["半句", "，还剩半句"], done_block=False))
chk("流式：真半截（两种收尾都没见过）仍必须抛，且半截已经递出去",
    blew is not None and got == ["半句", "，还剩半句"], (got, blew))
chk("流式：断流那句仍要说清「这次没存」",
    bool(blew) and "断了" in blew and "没存" in blew, blew)

# 这一条是这次修的病根：上游无视 stream:true，整段 JSON 一次回（无 data: 前缀）。
whole = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant",
                                                          "content": "整段答案"}}]},
                   ensure_ascii=False).encode("utf8")
got, blew = collect([whole])
chk("流式：整段 JSON（无 data: 前缀）也要吐出来，不许当成断线",
    got == ["整段答案"] and blew is None, (got, blew))
chk("流式：SSE 里给 message.content（不是 delta）也算答完",
    "".join(ui_server.agent_deltas(sse(["中转", "站回法"], shape="message"))) == "中转站回法")
chk("流式：脏行照旧跳过，不打断整段",
    "".join(ui_server.agent_deltas([b": ping\n\n", b"data: not-json\n\n"]
                                   + sse(["正文"]))) == "正文")
got, blew = collect([b"data: [done]\n\n"])
chk("流式：小写 [done] 也认（有的站不注意大小写）", got == [] and blew is None, (got, blew))


# ── C. 非流式抠字 ───────────────────────────────────────────────
chk("非流式：标准 JSON → message.content",
    ui_server.completion_text(json.dumps(
        {"choices": [{"message": {"content": " 在的 "}}]})) == "在的")
chk("非流式：上游回了 SSE（stream:false 却被无视）→ 把 data: 行拼起来",
    ui_server.completion_text("\n".join(
        l.decode("utf8").strip() for l in sse(["在", "的"]))) == "在的")
chk("非流式：content 是分段数组（多模态）也能拼",
    ui_server.completion_text(json.dumps(
        {"choices": [{"message": {"content": [{"type": "text", "text": "甲"},
                                              {"type": "text", "text": "乙"}]}}]})) == "甲乙")
chk("非流式：空 / 认不出 → 回空串，绝不抛（由调用方翻人话）",
    ui_server.completion_text("") == "" and ui_server.completion_text("upstream 502") == "")
chk("非流式：error 信封不会被当成正文",
    ui_server.completion_text(json.dumps({"error": {"message": "bad key"}})) == "")


# ── D. 配置读写 ─────────────────────────────────────────────────
ui_server.set_agent_cfg("", "", "")           # 清干净起跑
ui_server.set_agent_cfg("https://api.x.com", "sk-abc-1234", "gpt-4o-mini")
st = ui_server.agent_state()
chk("配置：存下后 url_set / key_set / model 都对，地址已补齐",
    st["url_set"] and st["key_set"] and st["model"] == "gpt-4o-mini"
    and st["url"] == "https://api.x.com/v1/chat/completions", st)
chk("配置：Key 只回末四位，明文不往界面送",
    st["key_tail"] == "1234" and "sk-abc-1234" not in json.dumps(st, ensure_ascii=False), st)

ui_server.set_agent_cfg("https://api.x.com/v1", "", "gpt-4o")
url, key, model = ui_server.agent_cfg()
chk("配置：只改模型时 Key 传空＝不改（不逼用户重贴一遍）",
    key == "sk-abc-1234" and model == "gpt-4o", (key, model))

ok, msg = ui_server.set_agent_cfg("https://api.x.com/v1", "sk-new-9999", "")
chk("配置：缺模型名时报错且不落盘（返回人话）",
    ok is False and "模型" in msg, (ok, msg))

ui_server.set_agent_cfg("", "", "")
chk("配置：地址清空＝关闭（三项一起撤掉，不留半套）",
    ui_server.agent_cfg() == ("", "", ""), ui_server.agent_cfg())


# ── E. 接线 ─────────────────────────────────────────────────────
SRV = read_repo("ui_server.py")
HTML = read_repo("ui.html")
chk("接线：两个问答口都在（/api/agent/chat 流式、/api/agent/quick 一次问完）",
    '/api/agent/chat' in SRV and '/api/agent/quick' in SRV)
chk("接线：保存 / 测一下两个动作都在",
    'action == "agent.save"' in SRV and 'action == "agent.test"' in SRV)
SEG = SRV[SRV.index('if u.path == "/api/agent/chat"'):SRV.index('if u.path == "/api/agent/quick"')]
chk("接线：「没配」必须在 SSE 开始前回完（一转成流就改不了状态码）",
    "小 Agent 还没配" in SEG and SEG.index("小 Agent 还没配") < SEG.index("_sse_start()"),
    SEG[:80])
chk("接线：三条非流式的口都走 completion_text（不再各写一遍 json.loads）",
    SRV.count("completion_text(") >= 4, SRV.count("completion_text("))
chk("接线：流式口的线上格式是本机统一的 x-ndjson（前端四处流式同一个口径）",
    "application/x-ndjson" in SRV and "x-ndjson" in HTML)
chk("接线：设置里 Key 框是 password（明文不回界面）",
    'id="agentkey" type="password"' in HTML)

print()
print("小 Agent 体检：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for n in FAIL:
    print("  ✗ " + n)
sys.exit(1 if FAIL else 0)
