#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并 PR 之前先摊开看：哪些会撞、哪些疑似同一件事做了两遍。

为什么单开一个：`auto/*` 分支按小时往外冒，一批五到十个。逐个读完 diff 再决定
合并顺序是现在最慢的一步 —— 而真正需要人做判断的其实只有两类：

  1. **会冲突的**：两边的改动落在同一段（同一个函数 / 同一批 `run "..."` 登记行），
     git 合到一起必然要人解；
  2. **疑似重复的**：两个分支从相隔几分钟的循环里各自修了同一处，实现不同、意图
     相同（#67 与 #68 就是：都去收 `export_precise.py` 开跑前的两个读盘口，一个
     复用 `book_layout.read_meta`、一个另造 `read_meta_dict`）。这种必须挑一个合，
     另一个关掉，否则 `main` 上留下两份做同一件事的代码。

这个脚本不替你做判断，只把证据摆出来分档：

  冲突      —— `git merge-tree` 真合一次，报出来的就是真会冲突（定论，不是猜的）
  同区间    —— 两边的 hunk 落在同一段老代码上（会撞在一起）；再按「新增内容像不像」
               给一个相似度，像的是重复、不像的是并存，交给你读那两处定夺
  同文件    —— 同文件但不同段（单独合都干净，链式合到中间才可能撞）
  独立      —— 文件都不重叠，随便合

除了「撞不撞」，合并前还有一类必须当面看的：**这批改动本身该不该进来**。冲突预演
只会告诉你「合起来干不干净」—— 一个把 `requirements.txt` 顺手改掉、或者把已有断言
改软的 PR，合起来可以一点冲突都没有。所以再补一栏红线体检（纯路径 / 行判定）：
禁改路径（锁文件与依赖清单 / `.github/` / 迁移 / Docker / 密钥 / `.autoloop/`）、
已有测试被改或删（只许加不许改弱）、新增行里像凭据的串、以及改动规模。它只摆证据，
不代替人判断：扫不到红线不等于安全，只说明这几类没有。

怎么算「同一段」：拿 `git diff -U0` 的 hunk 头 `@@ -老起点,老长度 +新起点,新长度 @@`
把每处改动折算成老文件上的一个区间，两边区间相交就算同段。`-U0` 很要紧 —— 默认的
三行上下文会把「隔得挺远」的两处也算成重叠，全是假警报。

用法: python3 tools/pr_triage.py [选项] [分支…]
      python3 tools/pr_triage.py                       # 扫全部未并入 main 的 auto/* 分支
      python3 tools/pr_triage.py --base main a b c      # 只扫指定分支
      python3 tools/pr_triage.py --api aritosi/weread-guizang   # 顺带把 PR 号/标题标上
      python3 tools/pr_triage.py --no-merge             # 跳过两两预演（不碰 git 合并）
"""
import argparse
import dataclasses
import difflib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

# `@@ -12,3 +12,5 @@ def feed_do(body):` —— 老/新起点与长度都可能省略长度（改写 1 行时），
# 尾部的上下文（git 会填上这一段所属的函数 / 结构名）也一并收进来当「区间标签」。
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
DIFF_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")

# 相似度到多少算「像」：0.45 是在 #67/#68 与 #85/#87 上试出来的分界 —— 前者的两份
# 实现（不同函数名、同一套兜底逻辑）落在 0.6 上下，后者的两条 `run "..."` 登记行
# 只是句式相同、内容是两回事，落在 0.3 上下。这个数只当提示，不当判决。
DUP_HINT = 0.45

# ── 红线体检的两张表（只判路径 / 行，判不了「意图」）────────────────────
#
# 这些路径一进来就要人当面看：它们是「环境的定义」而不是「产品的代码」—— 锁文件与
# 依赖清单改了，别的机器上装出来的东西就跟着变；`.github/` 改了，门禁的规矩就变了；
# 密钥文件进来了，仓库就脏了。与 worker 的验收门禁同一份口径。
FORBIDDEN_PATTERNS = (
    ("锁文件 / 依赖清单", re.compile(
        r"(^|/)(requirements[\w.-]*\.txt|Pipfile(\.lock)?|poetry\.lock|"
        r"package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|"
        r"Gemfile\.lock|go\.sum|composer\.lock|packages\.lock\.json)$")),
    (".github（CI / 模板）", re.compile(r"(^|/)\.github/")),
    ("数据库迁移", re.compile(r"(^|/)migrations?/")),
    ("Docker", re.compile(
        r"(^|/)Dockerfile(\..*)?$|(^|/)docker-compose[\w.-]*\.ya?ml$")),
    ("环境 / 密钥文件", re.compile(
        r"(^|/)\.env(\..*)?$|\.pem$|\.p12$|\.key$|"
        r"(^|/)id_(rsa|ed25519|ecdsa)(\.pub)?$|\.keystore$|\.jks$")),
    (".autoloop 控制目录", re.compile(r"(^|/)\.autoloop/")),
)

# 凭据只认有明确前缀的那几种，再加一条最保守的「键名 = 一长串随机值」。宁可漏一个，
# 不制造一堆假警报 —— 假警报多了，人就不看这一栏了，那比没有还坏。
SECRET_PATTERNS = (
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(
        r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("OpenAI 风格 key", re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")),
    ("AWS key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("疑似写死的口令", re.compile(
        r"(?i)\b(api[_-]?key|secret|access[_-]?token|auth[_-]?token|passwd|password)\b"
        r"\s*[:=]\s*[\"'][A-Za-z0-9_\-/+]{20,}[\"']")),
)

# 改动规模到多少行算「该多看两眼」：与 AutoLoop 那边 churn<50 算 auto-ready 同一把尺。
CHURN_REVIEW = 50


@dataclasses.dataclass
class Hunk:
    """diff 里的一处改动，折算成老文件上的一个区间。"""

    path: str
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    ctx: str
    added: list = dataclasses.field(default_factory=list)
    removed: list = dataclasses.field(default_factory=list)
    _body: object = dataclasses.field(default=None, repr=False, compare=False)

    @property
    def old_span(self):
        """老文件上的闭区间 [起, 止]。

        纯插入（老长度 0）也占一格：`-629,0` 说的是「老文件第 629 行之后插进来」，
        两个都插在同一个锚点的改动必然撞车，所以至少按一行算，否则 0 宽区间永远
        不相交、正好漏掉最常见的那种冲突。
        """
        lo = self.old_start
        return (lo, lo + max(self.old_len, 1) - 1)

    @property
    def region(self):
        """这段改动落在哪 —— 优先用 git 填的上下文（函数名 / 那一行 run），没有就报行号。"""
        ctx = (self.ctx or "").strip()
        if not ctx:
            return "第 %d 行附近" % self.old_start
        # `def feed_do(body):` / `class Handler(...)` → 只留名字，标签短一点好读。
        m = re.match(r"^(?:async\s+)?def\s+(\w+)", ctx)
        if m:
            return "%s()" % m.group(1)
        m = re.match(r"^class\s+(\w+)", ctx)
        if m:
            return "%s 类" % m.group(1)
        return ctx[:64]

    @property
    def body_text(self):
        """这处改动「加了什么」，用来算两边像不像。

        缓存住：这个属性在「两两比对」的双重循环里会被反复取（每个 hunk 会跟对面
        每一个撞上的 hunk 比一次），每次都重新 join 一遍列表是白烧 CPU。历史分支的
        diff 可能很大，这一下省得很实在。
        """
        if self._body is None:
            self._body = "\n".join(self.added)
        return self._body


def parse_hunks(diff_text):
    """把 `git diff -U0` 的输出切成 Hunk 列表。

    只看 hunk 头定区间、看 +/- 行收内容；`No newline at end of file` 这类
    反斜杠行跳过（它跟着上一行，不该算进内容）。
    """
    hunks = []
    path = None
    cur = None
    for line in diff_text.splitlines():
        m = DIFF_RE.match(line)
        if m:
            path = m.group(2)
            cur = None
            continue
        m = HUNK_RE.match(line)
        if m:
            if path is None:
                continue
            cur = Hunk(
                path=path,
                old_start=int(m.group(1)),
                old_len=int(m.group(2) or "1"),
                new_start=int(m.group(3)),
                new_len=int(m.group(4) or "1"),
                ctx=m.group(5),
            )
            hunks.append(cur)
            continue
        if cur is None or line.startswith("\\"):
            continue
        if line.startswith("+"):
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return hunks


def spans_intersect(a, b):
    """两个闭区间是否相交。"""
    return not (a[1] < b[0] or b[1] < a[0])


def similarity(a, b):
    """两处新增内容的相似度 0..1（空对空算 1）。

    `SequenceMatcher` 是 O(n·m) 的字符级比对，喂进去两段几千行的实现能跑到肉眼可见
    地卡。这里只用来回答「像不像同一件事」，看前几百字符足够定性 —— 而且先按长度差
    粗筛（差太多直接给个低分），省掉大量无谓的比对。
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    limit = 600
    quick_a = a[:limit]
    quick_b = b[:limit]
    shorter = min(len(a), len(b))
    longer = max(len(a), len(b))
    if longer > 4 * shorter + 40:
        # 长度差这么悬殊，结构上就不是同一件事（一句话 vs 一整个函数）。
        return min(0.2, difflib.SequenceMatcher(None, quick_a, quick_b).ratio())
    return difflib.SequenceMatcher(None, quick_a, quick_b).ratio()


def pair_verdict(hunks_a, hunks_b):
    """比两个 PR 的改动面。

    回一个列表，每项 `(档位, 路径, 区间标签, 相似度, 说明)`；档位取
    `同区间` / `同文件`。同一处改动只报一次（同一区间里多个 hunk 取最像的那个）。
    """
    by_path = {}
    for h in hunks_a:
        by_path.setdefault(h.path, ([], []))[0].append(h)
    for h in hunks_b:
        by_path.setdefault(h.path, ([], []))[1].append(h)

    out = []
    for path in sorted(by_path):
        ha, hb = by_path[path]
        if not ha or not hb:
            continue
        # 按起点排好，用两指针扫（而不是 ha×hb 全比一遍）。两边都是按行号递增的，
        # 一个 x 只需跟「落点可能相交」的那几个 y 比 —— 大 diff（几百个 hunk）下
        # 这个差别就是「秒回」和「跑不完」。
        sa = sorted(ha, key=lambda h: h.old_span[0])
        sb = sorted(hb, key=lambda h: h.old_span[0])
        hit = False
        lo = 0
        for x in sa:
            xs, xe = x.old_span
            # 把 y 的游标推过那些「在 x 左边且够不着」的：y 的结尾 < x 的开头就无缘。
            while lo < len(sb) and sb[lo].old_span[1] < xs:
                lo += 1
            best = None
            k = lo
            while k < len(sb) and sb[k].old_span[0] <= xe:
                y = sb[k]
                if spans_intersect(x.old_span, y.old_span):
                    ratio = similarity(x.body_text, y.body_text)
                    if best is None or ratio > best[0]:
                        best = (ratio, x, y)
                k += 1
            if best is None:
                continue
            hit = True
            ratio, x2, y2 = best
            how = ("两边都改在 %s 这一段" % x2.region) if x2.region == y2.region \
                else ("两边落在相邻/交错的行段（%s 与 %s）" % (x2.region, y2.region))
            out.append(("同区间", path, x2.region, ratio, how))
        if not hit:
            # 同文件、不同段：单独合各自都干净，但一批连着合时中间的改动会把上下文
            # 挪走，到某一步照样撞 —— 列出来是提醒「排在链子里的顺序有讲究」。
            out.append(("同文件", path, "", 0.0,
                        "同文件但区段不重叠（第 %d 处 / 第 %d 处）"
                        % (ha[0].old_start, hb[0].old_start)))
    return out


# ── 红线体检：纯路径 / 行判定，不跑 git、不联网 ──────────────────────


def churn(hunks):
    """这批改动的规模：`(加了几行, 删了几行)`。

    `-U0` 下没有上下文行，hunk 里的 `+` / `-` 就是真实改动量，直接数得出来。
    """
    return (sum(len(h.added) for h in hunks),
            sum(len(h.removed) for h in hunks))


def red_lines(hunks):
    """一个大 diff 的合入前体检。回 `[(档位, 路径, 说明)]`，空的就表示这几类没扫到。

    只看两样看得见的东西：**改了哪些路径**、**新增/删除的是哪些行**。所以它不会
    （也不该）替人下结论 —— 「该不该合」还得读那几行。四类：

      禁改路径   锁文件 / 依赖清单、`.github/`、迁移、Docker、密钥、`.autoloop/`
      改了已有测试  `tests/` 下的文件出现删除行（新加一个测试文件不算 —— 它没有删除行）
      疑似凭据   新增行里带明确前缀的 token / 私钥 / 写死的口令
      改动偏大   加了 + 删了 >= CHURN_REVIEW 行
    空 diff 单独报一条：一条没有任何改动的分支不该被当成一个待合的 PR。
    """
    if not hunks:
        return [("空改动", "", "这条分支相对 base 没有任何改动 —— 空 diff 不算数")]

    out = []
    touched_tests = {}
    for h in sorted(hunks, key=lambda x: (x.path, x.old_start)):
        path = h.path
        for why, rx in FORBIDDEN_PATTERNS:
            if rx.search(path):
                out.append(("禁改路径", path, why))
                break
        # 已有测试：出现删除行就是「改了 / 删了」。新测试文件整份是新增行，removed 为空。
        if path.startswith("tests/") or "/tests/" in path:
            if h.removed:
                touched_tests[path] = touched_tests.get(path, 0) + len(h.removed)
        for line in h.added:
            for why, rx in SECRET_PATTERNS:
                if rx.search(line):
                    out.append(("疑似凭据", path, "%s:%s" % (why, line.strip()[:80])))
                    break

    for path in sorted(touched_tests):
        out.append(("改了已有测试", path,
                    "删 / 改了 %d 行 —— 已有测试只许加，不许改弱" % touched_tests[path]))

    added, removed = churn(hunks)
    if added + removed >= CHURN_REVIEW:
        out.append(("改动偏大", "",
                    "加 %d 行 / 删 %d 行（>= %d 行，合之前读一遍）"
                    % (added, removed, CHURN_REVIEW)))
    return out


# ── 以下是要跑 git / 联网的部分，判定逻辑已经在上面了 ──────────────────


def git(*args, cwd=None):
    """跑一条 git，回 (退出码, 标准输出)。不抛。"""
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout


def diff_hunks(base, branch, cwd=None):
    """取 base...branch 的 hunk（三点：从共同祖先进来看，正是合并会带进来的那部分）。"""
    code, out = git("diff", "-U0", "--no-color", "%s...%s" % (base, branch), cwd=cwd)
    if code != 0:
        return None
    return parse_hunks(out) if out.strip() else []


def merged_into(branch, base, cwd=None):
    """branch 是否已经是 base 的祖先（合过就不用再看）。"""
    code, _ = git("merge-base", "--is-ancestor", branch, base, cwd=cwd)
    return code == 0


def default_branches(base, cwd=None):
    """默认扫「未并入 base 的远端 auto/* 分支」。"""
    code, out = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/auto/",
                    cwd=cwd)
    if code != 0:
        return []
    return [b for b in out.split() if b and not merged_into(b, base, cwd=cwd)]


def merge_would_conflict(a, b, cwd=None):
    """`git merge-tree --write-tree` 真合一次，回冲突文件列表（空 = 干净）。

    这是定论不是猜测：它就是 git 自己合的结果，只是不落盘、不动工作区。
    """
    code, out = git("merge-tree", "--write-tree", a, b, cwd=cwd)
    files = []
    for line in out.splitlines():
        if line.startswith("CONFLICT"):
            m = re.search(r"Merge conflict in (.+)$", line)
            if m:
                files.append(m.group(1).strip())
    if code != 0 and not files:
        # 极老的 git 不认 --write-tree，会打印 usage —— 那就当「没预演成」，不谎报干净。
        return None
    return files


def fetch_prs(slug):
    """从 GitHub 拿 {分支名: (号, 标题)}。

    回 `None` = 没取成（网络 / 限流 / 仓库名不对），回 `{}` = 取成了但一个 open 的
    也没有。这两件事必须分开：前者该退回按分支名扫，后者就该直接说「没有待扫的」——
    混在一起会把「今天没开 PR」错当成「API 不通」，然后退回去扫几十个历史分支。
    """
    url = "https://api.github.com/repos/%s/pulls?state=open&per_page=100" % slug
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "guizang-pr-triage",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return None
    out = {}
    for p in data:
        out[p["head"]["ref"]] = (p["number"], p.get("title") or "")
    return out


def short(branch):
    """`origin/auto/20261015-034224-1` → `auto/20261015-034224-1`。"""
    return branch[len("origin/"):] if branch.startswith("origin/") else branch


def label(branch, prs):
    """给分支配一个人能认的标签：有 PR 号就用 `#87`，没有就用分支名。"""
    info = (prs or {}).get(short(branch))
    if info:
        return "#%s" % info[0]
    name = short(branch)
    return name[len("auto/"):] if name.startswith("auto/") else name


def main(argv=None):
    ap = argparse.ArgumentParser(description="合并 PR 前的撞车 / 重复预扫描")
    ap.add_argument("branches", nargs="*", help="要扫的分支（默认：未并入 base 的 auto/*）")
    ap.add_argument("--base", default="main", help="基准分支（默认 main）")
    ap.add_argument("--api", default="", help="OWNER/REPO，用 GitHub API 补上 PR 号与标题")
    ap.add_argument("--no-merge", action="store_true", help="跳过两两预演（不跑 git merge-tree）")
    ap.add_argument("--cwd", default=None, help="仓库目录（默认当前目录）")
    args = ap.parse_args(argv)

    cwd = args.cwd
    base = args.base
    prs = fetch_prs(args.api) if args.api else {}
    api_down = args.api and prs is None

    if args.branches:
        branches = args.branches
    elif args.api:
        if api_down:
            # 别退回扫未并入的历史分支：远端那几十个 auto/* 里绝大多数是「PR 早关了 /
            # 早就合过没删」，不是要处理的工作集。扫它们既慢（两两预演是 n²/2 次真
            # 合并）又没意义，直接说清楚、让人显式给分支。
            print("GitHub API 没取到数据（网络 / 限流 / 仓库名不对）。"
                  "要么修好网络重跑，要么把分支名直接写在命令后面：")
            print("    python3 tools/pr_triage.py --base %s <分支1> <分支2> …" % base)
            return 0
        # 有 open PR 清单就以它为准：远端堆着一堆历史分支，全扫一遍既慢又吵。
        if not prs:
            print("没有 open PR 要扫（%s）。" % args.api)
            return 0
        branches = ["origin/%s" % ref for ref in prs
                    if not merged_into("origin/%s" % ref, base, cwd=cwd)]
        if not branches:
            print("open PR 里的分支都已并入 %s。" % base)
            return 0
    else:
        branches = default_branches(base, cwd=cwd)
        if not branches:
            print("没有待扫的分支（未并入 %s 的 origin/auto/* 是空的）。" % base)
            return 0
        if len(branches) > 15:
            print("未并入 %s 的 auto/* 有 %d 个（多数是关了 / 早合过没删的历史分支）。"
                  "两两预演是 n²/2 次真合并，这批会很慢 —— 建议只挑在办的："
                  % (base, len(branches)))
            print("    python3 tools/pr_triage.py --api OWNER/REPO")
            print("    （或显式列出分支名）\n")

    # 每个分支的改动面；取不到的（比如本地没有这个 ref）单独列出来，不静默跳过。
    faces, missing = {}, []
    for b in branches:
        hunks = diff_hunks(base, b, cwd=cwd)
        if hunks is None:
            missing.append(b)
        else:
            faces[b] = hunks

    print("PR 预扫描：base=%s，候选 %d 个%s"
          % (base, len(faces), ("，取不到 %d 个" % len(missing)) if missing else ""))
    for b in missing:
        print("  ! 取不到改动面：%s（ref 不在本地？先 git fetch）" % b)
    if not faces:
        return 1

    order = sorted(faces, key=lambda b: label(b, prs))
    for b in order:
        print("  %-10s %-34s %d 处改动 / %d 个文件"
              % (label(b, prs), short(b), len(faces[b]),
                 len({h.path for h in faces[b]})))
        info = prs.get(short(b))
        if info:
            print("             %s" % (info[1] or "")[:76])

    # ── 红线体检：先看「该不该进来」，再看「撞不撞」 ───────────────
    print("\n■ 合入前的红线体检（禁改路径 / 改已有测试 / 疑似凭据 / 改动规模）")
    flagged = 0
    for b in order:
        added, removed = churn(faces[b])
        head = "  %-10s %-34s +%d/-%d" % (label(b, prs), short(b), added, removed)
        findings = red_lines(faces[b])
        if not findings:
            print(head + "  干净")
            continue
        flagged += 1
        print(head)
        for tier, path, why in findings:
            print("      [%s] %s%s" % (tier, (path + " · ") if path else "", why))
    if flagged:
        print("  → %d 个分支带回线，合之前先把上面这几条读一遍。" % flagged)
    else:
        print("  → 这几类都没扫到（不等于安全，只说明红线表里这几条没有）。")

    # ── 两两预演：这是定论 ─────────────────────────────────────────
    # n 个分支就是 n(n-1)/2 次真合并。正常一批五到十个（<=45 次）没问题；真扫到几十个
    # 历史分支时这一步会拖很久，所以超量就整段跳过 —— 判定那一栏（同区间 / 同文件）是
    # 纯内存算的，不受影响，别为了「定论」把命令挂在那儿。
    PAIR_LIMIT = 12
    if not args.no_merge and len(order) <= PAIR_LIMIT:
        pairs = []
        for i, a in enumerate(order):
            for b in order[i + 1:]:
                conf = merge_would_conflict(a, b, cwd=cwd)
                if conf:
                    pairs.append((a, b, conf))
        print("\n■ 冲突预演（git merge-tree 真合一次，定论）")
        if pairs:
            for a, b, conf in pairs:
                print("  %s × %s  →  %s"
                      % (label(a, prs), label(b, prs), "、".join(conf)))
        else:
            print("  两两都干净（注意：这只说明「任取两个」不撞，不保证一整批连续合不撞）")
    elif not args.no_merge:
        print("\n■ 冲突预演：候选 %d 个，超过 %d 个就不两两预演了"
              "（%d 次真合并太慢）；看下面「同区间」那一栏即可，那栏是逐处算的"
              % (len(order), PAIR_LIMIT, len(order) * (len(order) - 1) // 2))

    # ── 分档 ───────────────────────────────────────────────────────
    dup, same_file = [], []
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            for tier, path, region, ratio, how in pair_verdict(faces[a], faces[b]):
                if tier == "同区间":
                    dup.append((a, b, path, region, ratio, how))
                else:
                    same_file.append((a, b, path, how))

    print("\n■ 同区间（改动落在同一段，合起来必然要人解）")
    if dup:
        for a, b, path, region, ratio, how in sorted(dup, key=lambda r: -r[4]):
            hint = "相似 %.2f" % ratio
            flag = "  ← 疑似重复，挑一个合" if ratio >= DUP_HINT else "  ← 只是同段，可并存"
            print("  %s × %s  %s · %s  [%s]%s" % (label(a, prs), label(b, prs), path, region, hint, flag))
            print("        %s" % how)
    else:
        print("  无 —— 这批没有撞在同一段的")

    print("\n■ 同文件不同段（单独合都干净；整个文件被多个 PR 碰过，连着合到中间会撞）")
    if same_file:
        hot = {}
        for a, b, path, how in same_file:
            hot.setdefault(path, set()).update((a, b))
        for path, who in sorted(hot.items(), key=lambda kv: -len(kv[1])):
            print("  %s：%d 个分支都碰了它（%s）"
                  % (path, len(who), "、".join(label(b, prs) for b in
                                              sorted(who, key=lambda b: label(b, prs)))))
        print("  → 注意：共享文件（尤其 tests/run_all.sh 的登记行）几乎每个 PR 都会碰，")
        print("    只要「同区间」那一栏是空的，通常仍可依次合；真撞了再解。")
    else:
        print("  无")

    files_a = {h.path for h in faces[order[0]]}
    independent = [b for b in order[1:] if not (files_a & {h.path for h in faces[b]})]
    print("\n■ 与 %s 完全不重叠的：%s"
          % (label(order[0], prs),
             "、".join(label(b, prs) for b in independent) or "无"))

    # 疑似重复是「必须人裁」的那类，单独再喊一声，免得埋在上面那堆行里被划过去。
    dups = [(a, b, path, region, ratio) for a, b, path, region, ratio, _ in dup
            if ratio >= DUP_HINT]
    if dups:
        print("\n▶ 需要你先裁的疑似重复 %d 对：" % len(dups))
        for a, b, path, region, ratio in dups:
            print("    %s × %s  都在 %s · %s（相似 %.2f）"
                  % (label(a, prs), label(b, prs), path, region, ratio))
    return 0


if __name__ == "__main__":
    sys.exit(main())
