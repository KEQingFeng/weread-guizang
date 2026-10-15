# -*- coding: utf-8 -*-
"""web_parse 的 X 推文编号越界：四百位的 `/status/<id>` 不许把 OverflowError 弹给用户。

背景（改之前实测）
    `.venv/bin/python -c "import web_parse as w; w._tweet_id('https://x.com/u/status/'+'9'*400)"`
      -> 拿到那串 400 位数字（`_X_STATUS` 的 `\\d+` 不设上限）
    `w._tweet_token('9'*400)`
      -> OverflowError: int too large to convert to float

`_tweet_token` 里那一步是 `int(tid) / 1e15`：`int()` 收得下几百位的整数，除以
1e15（float）时才抛 OverflowError。它不是 ValueError —— `/api/clip` 那条
「抓取失败全是人话、原样递出去」的兜底（ui_server.py 剪藏路由的 `except Exception`
+ `str(e)`）接不住，用户拿到的是一行英文 Python 报错。

这一条钉四件事，全程离线（纯函数，一个请求都不发）：

  1. 上限真的收在编号那一层：超长 / 恰好 26 位的编号不算编号（回空串），
     正常 19 位雪花 id 一个字不改；
  2. **不许静默截断**：26 位的那串不能只取出前 25 位照发 —— 那是个错请求，
     比抛错更难查；
  3. 从 `extract()`（真入口）走一遍：超长编号抛的是 ValueError 带的人话，
     而且根本不该发请求（`fetch_json` 换成一枚「一碰就红」的绊线）；
  4. `_tweet_token` 自己也有闸：几百位 / 非数字 / 全角数字 / 空值一律人话，
     对照证明旧的算式确实会抛 OverflowError（免得门禁空转）。

跑法：.venv/bin/python tests/check_web_parse_bigid.py
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import web_parse  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


HUGE = "9" * 400                 # 足够越过 float 上限（约 1.8e308）
X_TWEET_ID = "1712749080000000000"      # 与 check_web_parse.py 用同一个真 id
X_TOKEN = "45grcv5u1rx"                 # 该 id 手算出来的 token（另一条门禁也钉着）


def _url(tid):
    return "https://x.com/demo_user/status/" + tid


# ── 1. 对照：旧的算式确实会抛 OverflowError（这条门禁不是空转）──────────
try:
    (int(HUGE) / 1e15)
    old_exc = ""
except OverflowError as e:
    old_exc = str(e)
except Exception as e:  # noqa: BLE001
    old_exc = "__另一个异常：%s__" % type(e).__name__
chk("对照：`int(400位) / 1e15` 抛的正是 OverflowError", "too large" in old_exc, old_exc)

# ── 2. 编号这一层：超长不算编号，正常 id 不改 ──────────────────────────
chk("_tweet_id：400 位编号不算编号（回空串）", web_parse._tweet_id(_url(HUGE)) == "",
    web_parse._tweet_id(_url(HUGE))[:40])
chk("_tweet_id：恰好 26 位也不收（上界就是 25）",
    web_parse._tweet_id(_url("9" * 26)) == "")
chk("_tweet_id：25 位仍在界内", web_parse._tweet_id(_url("9" * 25)) == "9" * 25)
chk("_tweet_id：正常 19 位雪花 id 一个字不改",
    web_parse._tweet_id(_url(X_TWEET_ID)) == X_TWEET_ID)
chk("_tweet_id：结尾跟字母的老写法仍认（/status/123abc）",
    web_parse._tweet_id("https://x.com/u/status/123abc") == "123")

# 不许静默截断：26 位那串若被取成前 25 位，会发出一个指向别的推文的请求
got = web_parse._tweet_id(_url("9" * 26))
chk("不许静默截断：26 位不许被取成前 25 位", got != "9" * 25, got)

# ── 3. _tweet_token 自己也有闸：认不出就抛人话，不抛英文运行期错误 ──────
def token_outcome(value):
    """回 (异常类型名, 消息)；没抛回 ("", 结果)。"""
    try:
        return "", web_parse._tweet_token(value)
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, str(e)


for label, bad in (("400 位编号", HUGE), ("26 位编号", "9" * 26),
                   ("空串", ""), ("None", None), ("字母串", "abc"),
                   ("科学计数法", "1e5"), ("全角数字", "１２３４５")):
    kind, msg = token_outcome(bad)
    chk("_tweet_token（%s）：抛 ValueError 且是人话" % label,
        kind == "ValueError" and bool(msg) and "推文编号" in msg,
        (kind, msg))

chk("_tweet_token：正常 id 仍算出同一个 token（与 check_web_parse 同一把尺）",
    web_parse._tweet_token(X_TWEET_ID) == X_TOKEN, web_parse._tweet_token(X_TWEET_ID))

# ── 4. 真入口 extract()：抛人话，且一个请求都不发 ─────────────────────
HITS = []


def _tripwire(url):
    HITS.append(url)
    raise AssertionError("超长编号根本不该走到发请求这一步")


_real_fetch_json = web_parse.fetch_json
web_parse.fetch_json = _tripwire
try:
    try:
        web_parse.extract(_url(HUGE))
        entry_kind, entry_msg = "", "__没抛__"
    except ValueError as e:
        entry_kind, entry_msg = "ValueError", str(e)
    except Exception as e:  # noqa: BLE001
        entry_kind, entry_msg = type(e).__name__, str(e)
finally:
    web_parse.fetch_json = _real_fetch_json

chk("extract()：超长编号抛 ValueError（不是 OverflowError）", entry_kind == "ValueError",
    (entry_kind, entry_msg))
chk("extract()：消息是中文人话，不含英文运行期错误",
    "编号" in entry_msg and "int too large" not in entry_msg, entry_msg)
chk("extract()：超长编号没有发出任何请求", HITS == [], HITS)

# ── 5. 一处实现：上界与闸真的写在 web_parse.py 里 ─────────────────────
SRC = (REPO / "web_parse.py").read_text(encoding="utf-8")
for label, wire in (
        ("编号上界常量", "_X_ID_MAX = 25"),
        ("/status/ 的 id 收口", "([0-9]{1,%d})(?![0-9])"),
        ("_tweet_token 的第二道闸", 're.fullmatch(r"[0-9]{1,%d}" % _X_ID_MAX, text)'),
):
    chk("接线：" + label, wire in SRC)

print()
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("X 推文编号越界收口：%d 项通过" % PASSED)
