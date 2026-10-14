#!/usr/bin/env python3
"""书城短 id 的那把尺子，只在这一个地方定义。

微信读书里「一本书」有两套编号，加书架只认其中一套：

  书城短 id   /store/search 的 bookInfo.bookId、/shelf/sync 每条里的 bookId。
              纯数字，加书架接口 POST /mp/shelf/addToShelf 只收这种。
  阅读器 id   书架 deepLink 里 ?v= 那一段，字母数字都有。它也能塞进接口，
              但回包 succ:0 不算成功 —— 所以这一层就先把它挡掉。

判定用 fullmatch 而不是 search：整串必须都是数字，5–20 位。

为什么上限是 20、为什么单开一个模块：
    曾经仓库里有三把对不上的尺子 —— 界面（ui.html / mcp 适配器 / ui_server.py）
    放到 20 位，子进程 shelf_add.py 却只到 12 位。书城搜到 13–20 位的书时，
    界面上算「有可加的短 id」，点下去却被引擎判成「没有可用的书籍编号
    （需要书城短 id）」，用户只看到一颗点了没用的按钮。现在 Python 这一侧
    只留这一把尺子，改也只改这一处；界面与 MCP 是 JS / HTML，另在
    tests/check_shelf_add.py 里钉住「几处写的都是同一个数」。

这个模块刻意不 import 任何第三方库：门禁要能在没装 playwright 的机器上跑。
"""
import re

# 一个数字都不许少、也不许多：5–20 位纯数字。
# 这两个数就是「书城短 id 长什么样」的全部定义，门禁拿它去比对界面上另外几处
# （ui.html / mcp 适配器）写的是不是同一个数。
STORE_BOOK_ID_MIN = 5
STORE_BOOK_ID_MAX = 20
STORE_BOOK_ID_RE = re.compile(r"\d{%d,%d}" % (STORE_BOOK_ID_MIN, STORE_BOOK_ID_MAX))


def is_store_book_id(value):
    """这个值能不能当书城短 id 用（能，就说明它可以进加书架请求）。"""
    return bool(STORE_BOOK_ID_RE.fullmatch(str(value if value is not None else "").strip()))


def clean_store_book_ids(raw):
    """把一串候选滤成真正能加书架的 id：去空白、只留纯数字短 id、保序去重。

    传进来的可能是书名、链接、阅读器长 id，一律安静丢掉 —— 加书架接口不回
    有用的错，宁可在这里少发几个请求，也不要把明知道会被 succ:0 的东西发出去。
    """
    out = []
    for s in raw or ():
        s = str(s if s is not None else "").strip()
        if STORE_BOOK_ID_RE.fullmatch(s) and s not in out:
            out.append(s)
    return out
