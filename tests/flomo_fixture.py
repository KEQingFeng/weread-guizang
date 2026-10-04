# -*- coding: utf-8 -*-
"""flomo 走查用的假导出包（两份套件共用：tests/check_flomo_notes.py 与 check_flomo_ui.py）。

这里每一条正文都是现编的示例句子。**用户的真实笔记一个字都不许进来** —— 这份文件会
推到 GitHub 上，写进去就等于把某个人的私人日记公开贴出来。所以夹具的形状对着官方导出
的样子做（`<div class="memo">` + time / content / files 三层、包名那层目录、图在
`file/<日期>/<号码>/` 下），内容全换掉。

形状要点（都是踩过坑的地方，改夹具前先读这段）：
  · 包里的路径带最外层包名 `flomo@…-20260901/file/…`，HTML 里的 `src` 却没有那一层 ——
    这层差就是「图存下来了但笔记里看不见」那个 bug 的来源，夹具必须保留它；
  · 同一张图在包里换个路径再出现一次（用户转发过），看的是「本机只落一份」；
  · 有一条正文与第一条一字不差、只有时间不同，看的是「算新的一条而不是覆盖旧的」；
  · 有一条只有图没有字，走的是书名兜底那条路；
  · 有一条特意写长，界面上「长笔记自动折叠」按字数判，得给它一个样本。
"""
import base64
import io
import re
import zipfile

# 两张 1x1 的 PNG
PNG_A = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_B = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

PKG = "flomo@demo-20260901"
IMG_A_IN_ZIP = "%s/file/2026-08-29/75134/a.png" % PKG
IMG_A_IN_HTML = "file/2026-08-29/75134/a.png"
IMG_B_IN_ZIP = "%s/file/2026-08-30/75134/b.png" % PKG
IMG_B_IN_HTML = "file/2026-08-30/75134/b.png"

# 每条：时间 + 正文 HTML（+ 可选的图，按 HTML 里写的那个小路径）
# 界面上「折不折」按字数判（>180 字或 >420 字符），折完拿 max-height:172px 裁。
# 所以这一条要比那道闸还长得多：1440 那一屏卡片很宽，一行放得下八九十个字，
# 四百字摊下来不到 140px —— 比 172 还矮，「折住了」和「没折」量出来一样高，
# 断言就成了假的（踩过一次，别再拿短样本冒充长笔记）。
LONG_BODY = "这一段故意写长一点，凑够界面那把尺要的字数，" * 40
MEMOS = [
    ("2026-08-29 07:12:03",
     '<p>早上一句话的灵感 #灵感/写作</p><p>第二段：先写下来再改。</p>'),
    ("2026-08-29 09:40:00",
     '<p>清单测试：</p><ul><li>买米</li><li>换滤芯</li></ul><p>后面还有一句 #SOP/家务</p>'),
    ("2026-08-29 21:05:59",
     '<p>今天看到一段话讲<strong>复利</strong>，还有<mark>高亮</mark>和<code>代码</code>，'
     '末尾跟个序号 #3 不算标签。</p>'),
    ("2026-08-30 06:30:10", '<p>带图的一条，图在 files 那层。 #读书</p>', IMG_A_IN_HTML),
    ("2026-08-30 22:48:00",
     '<p>引用与换行：<br>第二行<br>第三行</p><blockquote>这一整块是引用。</blockquote>'
     ' #SOP/flomo'),
    # 正文里套一层标签：解析器要跟着深度走，不许在这里把外层 memo 当结束。
    ("2026-08-31 08:00:00",
     '<p>外层第一段</p><p>内层套了个 div：<span>里面这句不该被吞掉</span></p> #读书/神经科学'),
    # 只有图、没有字的一条（书名的兜底路径）。
    ("2026-08-31 12:00:00", '<p></p>', IMG_B_IN_HTML),
    # 长正文：界面上「长笔记自动折叠」的样本。
    ("2026-08-31 23:59:59",
     "<p>%s</p><p>%s</p>" % (LONG_BODY, "补一句收尾。 #灵感")),
    # 与第一条正文一字不差、只有时间不同 → 哈希不同，应当当成新的一条。
    ("2026-09-01 10:00:00",
     '<p>早上一句话的灵感 #灵感/写作</p><p>第二段：先写下来再改。</p>'),
]
N_MEMOS = len(MEMOS)


def memo_html(idx):
    h = ('<div class="memo">'
         '<div class="time">%s</div>'
         '<div class="content">%s</div>'
         '<div class="files">%s</div>'
         '</div>' % (MEMOS[idx][0], MEMOS[idx][1],
                     "".join('<img src="%s" alt="memo image" />' % s
                             for s in MEMOS[idx][2:])))
    # 自己先看一眼：占位符没被填上时，解析器会老老实实把 "%s" 当成图的路径，
    # 于是一串「找不到附件」的失败从别处冒出来，查半天查不到是夹具的锅。
    for s in MEMOS[idx][2:]:
        assert s in h, "第 %d 条的图路径没填进去（%s）" % (idx, s)
    return h


def export_html(with_images=True):
    """那一页 HTML。with_images=False 模拟「按标签导出的那份不含附件目录」。"""
    body = []
    for i in range(N_MEMOS):
        h = memo_html(i)
        if not with_images:
            h = re.sub(r'<div class="files">.*?</div>', '<div class="files"></div>', h, flags=re.S)
        body.append(h)
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Notes</title></head>'
            '<body><div class="memos">%s</div></body></html>' % "".join(body))


def export_zip(with_images=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("%s/index.html" % PKG, export_html(with_images))
        if with_images:
            z.writestr(IMG_A_IN_ZIP, PNG_A)
            z.writestr(IMG_B_IN_ZIP, PNG_B)
            # 同一张图换个路径又出现一次（用户自己转发过）→ 本机只该落一份
            z.writestr("%s/file/2026-08-29/88888/a-again.png" % PKG, PNG_A)
    return buf.getvalue()
