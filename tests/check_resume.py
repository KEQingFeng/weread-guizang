# -*- coding: utf-8 -*-
"""续传锚点的取证：卡住之后再点一次取书，必须能接着往前读，而不是回到同一个死点。

为什么单独一份：实测《你身体里的奥秘》卡在 84/223 —— main() 一遇到「本轮没有写出
新章节」就直接收工，而续传位置又是阅读器自己的「上次读到」；它停在一个再也翻不出
内容的地方（方向键按 12 次一个字都抓不到），重跑照样落在同一点，于是永久卡死。

口径在 1.0.9 改过一次，理由是量出来的，不是猜的：
  旧口径是「跳到最后一个已写出项的下一项」，理由是「目录里的缺项多半是假缺，
  文字已经在相邻章节里」。拿那本书的 139 个缺项实测（把缺项标题与全书正文都走
  `bare()` 再子串比对）：**126 项的标题字一个都没出现过** —— 那 126 节的正文压根
  没被画出来过，是真漏，不是假缺。旧口径于是永远指向目录末尾（219 → 220），
  每一轮都钉在同一个死点上，用户看到的就是「每次都在相同的地方卡住」。
  全仓 13 本书里 6 本落盘数远低于目录项数（151/44、223/84…），不是孤例。

新口径三件事：
  A 续传落点 = **目录里第一个还没写出来的那一项**（顺序回填缺口，永远往前推进）
  B 缺口回填时不许把已写过的章节重抄一遍 → ChapterSplitter 的 gapfill 模式：
    落点标题直接认（anchor），路上撞到「已经写出去过的标题」就转进丢弃态，
    直到遇见下一个还没写过的标题才重新开章。
  C 有的项就是跳不过去（那一屏死活不重画）。连着两轮钉不住就把它记进
    _stuck.json、以后不再钉它 —— 只试一次就跳会把浮层遮挡、网络抖一下
    误判成永久缺陷；一次都不跳又会让下一次取书钉回同一个死点。

这里只验不依赖浏览器的那部分判据：
  A resume_catalog_index 的语义（第一个缺项；没有缺项 / 没有目录 / skip 掉某项时该返回什么）
  B 目录顺序照旧（load_catalog_ordered 不排序，下标才对得上 DOM）
  C 接口还在（run_session 收了 resume_at；jump_to_first_item 仍委托给通用版）
    C1 外层每一轮都按目录钉落点，并把「钉不住」的项记下来跳过
    C2 那份记录要攒够两轮才跳，目录重新 dump 过就作废
  D gapfill 分章：落点直接开章、旧章节落盘不重抄、假缺不丢内容、非 gapfill 行为不变
"""
import inspect
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import export_precise as E  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="guizang-resume-")


def book(items, written_idx, with_catalog=True):
    """造一本「目录 N 项、其中若干项已写出」的书，返回 (catalog_path, md_dir)。"""
    d = tempfile.mkdtemp(dir=TMP)
    cat = os.path.join(d, "_catalog.json")
    if with_catalog:
        io.open(cat, "w", encoding="utf-8").write(json.dumps(items, ensure_ascii=False))
    md = os.path.join(d, "chapters")
    os.makedirs(md)
    for i in written_idx:
        # 正文里的标题是干净的；「当前读到 xx%」只挂在目录项上，不落到章文件里。
        io.open(os.path.join(md, "%04d.md" % (i + 1)), "w", encoding="utf-8").write(
            "# " + items[i].split("当前读到")[0].strip() + "\n\n正文\n")
    return cat, md


# ── A. 落点语义：第一个还没写出来的目录项 ──────────────────
ITEMS = ["版权信息", "文前", "序言", "一 思维模式", "二 情绪健康", "结语"]

cat, md = book(ITEMS, [])
chk("一章都没写出时从目录第一项开始", E.resume_catalog_index(cat, md) == 0)

cat, md = book(ITEMS, [0, 1])
chk("写出前两项 → 续第 3 项（下标 2）", E.resume_catalog_index(cat, md) == 2)

# 缺口散在中间（《你身体里的奥秘》的实况）：**必须回填最早的缺口**。
# 旧口径在这里返回 5（最后写出项的下一项）= 目录末尾附近，那一跑就是一条死路，
# 于是每次重跑都卡在同一处；实测 139 个缺项里 126 个是真漏，回填才有内容可捞。
cat, md = book(ITEMS, [0, 2, 4])
chk("中间有缺口时回到最早的缺口（不跳到末尾）",
    E.resume_catalog_index(cat, md) == 1, E.resume_catalog_index(cat, md))

cat, md = book(ITEMS, [1, 2, 3, 4, 5])
chk("只缺第一项时钉回第一项（开头那几章不再被跳过）",
    E.resume_catalog_index(cat, md) == 0, E.resume_catalog_index(cat, md))

cat, md = book(ITEMS, list(range(len(ITEMS))))
chk("目录项全都写出来了 → 返回 None（该收尾，别再往下读）",
    E.resume_catalog_index(cat, md) is None)

cat, md = book(ITEMS, [0], with_catalog=False)
chk("没有目录文件时返回 None（没有位置可以钉）",
    E.resume_catalog_index(cat, md) is None)

# 「当前读到 42%」是目录项自带的进度尾巴，不能参与下标计算
cat, md = book(["版权信息 当前读到 3%", "序言 当前读到 9%", "结语"], [0])
chk("目录项的「当前读到 xx%」被剥掉再算",
    E.resume_catalog_index(cat, md) == 1)

# 章文件里的标题带装饰（比如被写成「## 序言」这种没被认出来的排版）时对不上号，
# 宁可多回填一次，也不能把「以为写过了」当成「写过了」。
cat, md = book(ITEMS, [0])
io.open(os.path.join(md, "0009.md"), "w", encoding="utf-8").write("# 序言（修订）\n\n正文\n")
chk("目录项与章标题差一个字也算没写出",
    E.resume_catalog_index(cat, md) == 1, E.resume_catalog_index(cat, md))

# 跳过「钉不住的那几项」才不会再回到同一个死点：一项连着两轮什么都没写出来之后，
# 落点必须挪到它后面的第一个缺口（不然下一次取书又钉回它，症状一模一样）。
cat, md = book(ITEMS, [0, 1, 2, 3, 4])
chk("没有 skip 时钉在唯一剩下的缺口上", E.resume_catalog_index(cat, md) == 5)
chk("skip 掉它之后没有缺口了 → None（该收尾）",
    E.resume_catalog_index(cat, md, {5}) is None)

cat, md = book(ITEMS, [0, 3, 4, 5])
chk("skip 只影响被跳过的那一项，别的缺口照旧",
    E.resume_catalog_index(cat, md) == 1 and E.resume_catalog_index(cat, md, {1}) == 2,
    (E.resume_catalog_index(cat, md), E.resume_catalog_index(cat, md, {1})))

# ── B. 目录顺序 ──────────────────────────────────────────
cat, md = book(["短", "很长很长很长的一个标题啊"], [])
ordered = E.load_catalog_ordered(cat)
chk("load_catalog_ordered 保持目录原序（不按长度排）",
    [o for _n, o in ordered] == ["短", "很长很长很长的一个标题啊"], ordered)
by_len = E.load_catalog_titles(cat)
chk("load_catalog_titles 仍按长度降序（分章匹配要这个）",
    by_len and by_len[0][1] == "很长很长很长的一个标题啊", by_len)

# ── C. 接口 ──────────────────────────────────────────────
sig = inspect.signature(E.run_session)
chk("run_session 收了 resume_at（续传重定位的入口）", "resume_at" in sig.parameters,
    list(sig.parameters))
chk("jump_to_catalog_item 是协程", inspect.iscoroutinefunction(E.jump_to_catalog_item))
chk("jump_to_first_item 也还在（老调用点不用改）",
    inspect.iscoroutinefunction(E.jump_to_first_item))
src = inspect.getsource(E.jump_to_first_item)
chk("jump_to_first_item 委托给通用版，只有一份实现",
    "jump_to_catalog_item(" in src, src)
src = inspect.getsource(E.main)
chk("每一轮续传都按目录钉落点（不再只赌阅读器的上次读到）",
    "resume_catalog_index(" in src and "if recovering" not in src, src)
chk("钉不住的落点会被记下来并跳过（不会每一轮回到同一处）",
    "stuck_skip(" in src and "stuck_bump(" in src, src)
rs = inspect.getsource(E.run_session)
chk("回填缺口那一轮的分章开 gapfill（重读到的旧章节不重抄）",
    "gapfill=resume_at is not None" in rs, rs[:200])
chk("落点那一章由目录直接开章（标题不再画出来也认得）",
    "sp.anchor(" in rs, rs[:200])

# ── C2. 「钉不住」的记录：攒够两轮才跳，目录变了就作废 ────────
STUCK = os.path.join(tempfile.mkdtemp(dir=TMP), "_stuck.json")
chk("一开始谁都没被跳过", E.stuck_skip(STUCK, 10) == set())
chk("第一次钉不住只记一笔，还不跳",
    E.stuck_bump(STUCK, 10, 7) is False and E.stuck_skip(STUCK, 10) == set())
chk("连着两轮钉不住才把它计入跳过",
    E.stuck_bump(STUCK, 10, 7) is True and E.stuck_skip(STUCK, 10) == {7})
chk("别的项不受影响", E.stuck_bump(STUCK, 10, 2) is False and E.stuck_skip(STUCK, 10) == {7})
chk("目录项数变了（重新 dump 过）→ 老记录整份作废",
    E.stuck_skip(STUCK, 11) == set() and E.load_stuck(STUCK, 11) == {})
chk("记录读坏/不存在时退回「谁都没被跳过」",
    E.stuck_skip(os.path.join(TMP, "nope.json"), 10) == set())

# ── D. gapfill 分章：回填缺口不许重抄已写过的章节 ──────────
CAT = ["第一节 血", "第二节 汗", "第三节 泪", "第四节 唾液"]      # 目录原序


def mkcat(items):
    p = os.path.join(tempfile.mkdtemp(dir=TMP), "_catalog.json")
    io.open(p, "w", encoding="utf-8").write(json.dumps(items, ensure_ascii=False))
    return p


def T(items):
    return E.load_catalog_titles(mkcat(items))


def tx(s):
    return {"type": "text", "text": s}


def chapters(sp):
    """跑完 close()，返回 [(标题, 正文串)]。"""
    return [(t or "未题名", "".join(b["text"] for b in seg if b["type"] == "text"))
            for t, seg in sp.close()]


used19 = {E.norm_title("第二节 汗")}          # 假设第二节已经写出去过
gap = E.ChapterSplitter(T(CAT), set(used19), gapfill=True)
chk("gapfill：目录项按「写过 / 没写过」分成两摞",
    len(gap.pairs) == 3 and len(gap.used_pairs) == 1, (len(gap.pairs), len(gap.used_pairs)))

gap.anchor("第四节 唾液", E.norm_title("第四节 唾液"))
gap.push([tx("第四节 唾液"), tx("唾液的成分"), tx("和口水不一样")])
gap.push([tx("第三节 泪"), tx("泪是盐水")])       # 没写过的标题：照常切出下一章
done = chapters(gap)
chk("gapfill：落点标题直接开章，不用等正文把标题画出来",
    done and done[0][0] == "第四节 唾液", done)
chk("gapfill：锚定的那一页正文完整收下",
    done and "唾液的成分" in done[0][1], done)

# 撞见「已经写出去过的标题」→ 关掉当前章，之后的一律丢掉，直到下一个没写过的标题
gap = E.ChapterSplitter(T(CAT), {E.norm_title("第二节 汗")}, gapfill=True)
gap.anchor("第一节 血", E.norm_title("第一节 血"))
gap.push([tx("第一节 血"), tx("血量五升")])
gap.push([tx("第二节 汗"), tx("汗是咸的"), tx("重复正文甲")])
gap.push([tx("第三节 泪"), tx("泪是盐水")])
done = chapters(gap)
got = {t: body for t, body in done}
chk("gapfill：已写过的章节被当作边界，把当前章就地封住",
    "第一节 血" in got and "第三节 泪" in got, done)
chk("gapfill：重复正文一个字都不许再抄进新章",
    "汗是咸的" not in "".join(b for _t, b in done) and "重复正文甲" not in "".join(b for _t, b in done),
    done)
chk("gapfill：下一个没写过的标题正常开章并继续收正文",
    got.get("第三节 泪", "").strip().startswith("泪是盐水") or
    any("泪是盐水" in b for _t, b in done), done)

# 假缺：标题从来没被单独画出来过 → 正文留在锚定的那一章里，内容绝不丢
gap = E.ChapterSplitter(T(CAT), set(), gapfill=True)
gap.anchor("第一节 血", E.norm_title("第一节 血"))
gap.push([tx("血量五升"), tx("血浆九成是水")])       # 第二节 汗 的排版里根本没画标题
gap.push([tx("第二节 汗"), tx("汗是咸的")])          # 这一行是唯一能认出边界的东西
done = chapters(gap)
chk("gapfill：认不出标题时正文仍落在锚定的那一章（不丢内容）",
    done and done[0][0] == "第一节 血" and "血浆九成是水" in done[0][1], done)

# 不带 gapfill 时行为与今天一模一样：已写过的标题既不封章也不丢弃
old = E.ChapterSplitter(T(CAT), {E.norm_title("第二节 汗")})
old.push([tx("第一节 血"), tx("血量五升")])
old.push([tx("汗是咸的")])
done = chapters(old)
chk("非 gapfill：老行为不变（已写过的标题不触发丢弃态）",
    done and len(done) == 1 and "汗是咸的" in done[0][1], done)

# anchor 不许把已经写出去过的章再开一遍
gap = E.ChapterSplitter(T(CAT), {E.norm_title("第二节 汗")}, gapfill=True)
chk("anchor：已经写过的标题钉不进去（避免同一章切两次）",
    gap.anchor("第二节 汗", E.norm_title("第二节 汗")) is False and gap.cuts == 0)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("续传锚点取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
