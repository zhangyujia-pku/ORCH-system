"""
M0 · 任务契约与静态校验器
=========================

设计目标：在**花任何 token 之前**，把坏的拆解挡回去。

三个校验：
  1. 结构校验   —— 字段、类型、依赖、环
  2. 写集校验   —— 两个任务会不会写同一个文件（冲突的最早发现点）
  3. 判据校验   —— 验收条件是否可判定（能否落到测试命令 / 命令断言 / 文件断言）

纯标准库，无第三方依赖。所有失败都返回 Violation 对象而不是抛异常，
因为调用方需要把它们整批写进 trace.jsonl 的 plan.rejected 事件。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# 1. 违规代码表
#    代码是稳定的，报告文字是可改的 —— 实验统计只认代码。
# --------------------------------------------------------------------------

CODES: dict[str, tuple[str, str]] = {
    "E000": ("error", "JSON 语法错误"),
    "E001": ("error", "文档结构非法"),
    "E002": ("error", "任务 id 重复"),
    "E003": ("error", "缺少必填字段"),
    "E004": ("error", "存在未知字段"),
    "E005": ("error", "字段类型错误"),
    "E006": ("error", "goal 为空"),
    "E007": ("error", "goal 疑似复合任务"),
    "E008": ("error", "deps 指向不存在的任务"),
    "E009": ("error", "任务依赖自己"),
    "E010": ("error", "依赖成环"),
    "E011": ("error", "writes 为空"),
    "E012": ("error", "acceptance 为空"),
    "E013": ("error", "写集重叠"),
    "E014": ("error", "验收判据不可判定"),
    "E015": ("error", "路径越界"),
    "E016": ("error", "writes 与 forbidden 冲突"),
    "E017": ("error", "budget 非法"),
    "E018": ("error", "writes 覆盖整个仓库"),
    "E019": ("error", "读了别人正在写的文件却没有依赖"),
    "E020": ("error", "needs 含未知能力名"),
    "W001": ("warn", "goal 过长"),
    "W002": ("warn", "reads 未命中任何文件"),
    "W003": ("warn", "reads 覆盖整个仓库"),
    "W004": ("warn", "needs 声明与任务内容不符（虚声明）"),
    "W005": ("warn", "任务内容需要外部能力但未声明 needs（漏声明）"),
    "W006": ("warn", "所有判据都不是命令型，验收只能靠模型判断"),
    "E021": ("error", "未知的执行模式"),
    "E022": ("error", "声明 chain，但阶段依赖没声明清楚"),
    "E023": ("error", "声明 single，但计划里任务数 > 1"),
}

# 契约字段（顺序即文档顺序）
FIELDS: tuple[str, ...] = (
    "id",
    "goal",
    "deps",
    "reads",
    "writes",
    "acceptance",
    "forbidden",
    "context_refs",
    "needs",
    "inputs_from",
    "budget",
)
LIST_FIELDS = ("deps", "reads", "writes", "acceptance", "forbidden", "context_refs",
               "needs", "inputs_from")
REQUIRED_FIELDS = ("id", "goal")

# 外部能力名。这是「哪个 worker 能用」的**硬约束**，不是偏好。
# 加新能力必须同时改这里和 workers.json，否则路由会静默降级成「谁都不需要」。
CAPABILITIES: dict[str, str] = {
    "web": "实时联网（搜索 + 抓取页面正文），不是索引缓存",
    "db": "直连数据库（SQLite / PostgreSQL / MySQL）",
    "docs": "企业文档平台（腾讯文档 / 网盘 / 知识库）",
    "net-exec": "允许命令本身出网（pip install / curl / git push）",
}


def decode_bytes(data: bytes | None) -> str:
    """解码子进程输出。

    不能写死 utf-8：中文 Windows 上，子进程往管道里写的是 GBK
    （除非设了 PYTHONUTF8/PYTHONIOENCODING）。写死 utf-8 会把中文读成乱码，
    而这类错误只在别人的机器上出现——本机开发时看不出来。
    顺序：先 UTF-8（多数 agent CLI 都是），失败再退到本地编码。
    """
    import locale

    if not data:
        return ""
    if isinstance(data, str):  # 防御：调用方改成 text 模式时也不炸
        return data
    for enc in ("utf-8", locale.getpreferredencoding(False), "cp936", "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    task_id: str | None = None
    hint: str | None = None

    @property
    def severity(self) -> str:
        return CODES.get(self.code, ("error", ""))[0]

    def __str__(self) -> str:
        who = f"[{self.task_id}] " if self.task_id else ""
        head = f"{self.code} {who}{self.message}"
        return head + (f"\n        → {self.hint}" if self.hint else "")


@dataclass(frozen=True)
class Task:
    id: str
    goal: str
    deps: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    inputs_from: tuple[str, ...] = ()   # 读哪个任务的产物；chain 模式必须显式写
    budget: float = 1.0


@dataclass
class Report:
    """一次校验的完整结果。可直接序列化成 plan.rejected 事件。"""

    tasks: list[Task] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    layers: list[list[str]] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def max_width(self) -> int:
        return max((len(l) for l in self.layers), default=0)

    def codes(self) -> list[str]:
        return [v.code for v in self.violations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "n_tasks": len(self.tasks),
            "layers": self.layers,
            "max_width": self.max_width,
            "violations": [
                {
                    "code": v.code,
                    "severity": v.severity,
                    "task_id": v.task_id,
                    "message": v.message,
                    "hint": v.hint,
                }
                for v in self.violations
            ],
        }


# --------------------------------------------------------------------------
# 2. 解析：宽松收集，不抛异常
# --------------------------------------------------------------------------

def parse_document(doc: Any) -> tuple[list[Task], list[Violation]]:
    """把 (已解析的) JSON 对象转成 Task 列表 + 违规列表。"""
    v: list[Violation] = []

    if not isinstance(doc, dict):
        return [], [Violation("E001", "顶层必须是对象 {'tasks': [...]}")]
    tasks_raw = doc.get("tasks")
    if not isinstance(tasks_raw, list):
        return [], [Violation("E001", "缺少 tasks 数组，或 tasks 不是数组")]

    seen: set[str] = set()
    out: list[Task] = []

    for i, raw in enumerate(tasks_raw):
        tag = f"#{i}"
        if not isinstance(raw, dict):
            v.append(Violation("E005", f"{tag} 不是对象", hint="每个任务必须是 JSON 对象"))
            continue

        tid = raw.get("id")
        if isinstance(tid, str) and tid.strip():
            tag = tid.strip()

        # --- 未知字段（专抓 write / acceptance_criteria 这类笔误）---
        unknown = [k for k in raw if k not in FIELDS]
        for k in unknown:
            v.append(
                Violation(
                    "E004",
                    f"未知字段 '{k}'",
                    tag,
                    hint=f"合法字段：{', '.join(FIELDS)}",
                )
            )

        # --- 必填 ---
        missing = [k for k in REQUIRED_FIELDS if k not in raw]
        for k in missing:
            v.append(Violation("E003", f"缺少必填字段 '{k}'", tag))
        if missing:
            continue  # 缺 id/goal 无法继续构造成本对象

        # --- 类型 ---
        if not isinstance(raw["id"], str) or not raw["id"].strip():
            v.append(Violation("E005", "id 必须是非空字符串", tag))
            continue
        if not isinstance(raw["goal"], str):
            v.append(Violation("E005", "goal 必须是字符串", tid))
            continue

        bad_type = False
        for k in LIST_FIELDS:
            if k in raw and not (
                isinstance(raw[k], list) and all(isinstance(x, str) for x in raw[k])
            ):
                v.append(Violation("E005", f"{k} 必须是字符串数组", tid))
                bad_type = True
        if bad_type:
            continue

        budget = raw.get("budget", 1.0)
        if not isinstance(budget, (int, float)) or isinstance(budget, bool):
            v.append(Violation("E005", "budget 必须是数字", tid))
            budget = 1.0
        elif budget <= 0:
            v.append(
                Violation("E017", f"budget={budget} 非法", tid, hint="必须 > 0（美元）")
            )
            budget = 1.0

        # --- needs 值域：拼错会静默失效，比报错更危险 ---
        needs = [str(x).strip() for x in raw.get("needs", ())]
        unknown_needs = [x for x in needs if x not in CAPABILITIES]
        for x in unknown_needs:
            v.append(
                Violation(
                    "E020",
                    f"needs 含未知能力 '{x}'",
                    tid,
                    hint=f"可用能力：{', '.join(CAPABILITIES)}；拼错的能力名会被当成「不需要」",
                )
            )
        needs = [x for x in needs if x in CAPABILITIES]

        # --- 重复 id ---
        if tid in seen:
            v.append(
                Violation(
                    "E002",
                    f"id '{tid}' 重复",
                    tid,
                    hint="id 是 trace / 返工 / 冲突的唯一索引，必须唯一",
                )
            )
            continue
        seen.add(tid)

        goal = raw["goal"].strip()
        if not goal:
            v.append(Violation("E006", "goal 为空", tid))
        elif len(goal) > 60:
            v.append(
                Violation(
                    "W001",
                    f"goal 长度 {len(goal)} > 60，可能塞了多个动作",
                    tid,
                    hint="一个任务只做一件事；复合目标请拆开",
                )
            )

        out.append(
            Task(
                id=tid,
                goal=goal,
                deps=tuple(raw.get("deps", ())),
                reads=tuple(raw.get("reads", ())),
                writes=tuple(raw.get("writes", ())),
                acceptance=tuple(raw.get("acceptance", ())),
                forbidden=tuple(raw.get("forbidden", ())),
                context_refs=tuple(raw.get("context_refs", ())),
                needs=tuple(needs),
                inputs_from=tuple(raw.get("inputs_from", ())),
                budget=float(budget),
            )
        )

    return out, v


# --------------------------------------------------------------------------
# 3. 结构校验
# --------------------------------------------------------------------------

_COMPOUND_MARKERS = ("并且", "然后", "同时", "以及", "；", ";")
_ESCAPE_PATTERNS = re.compile(r"(^[/\\]|^[A-Za-z]:|(^|/)\.\.(/|$))")


def check_goal(task: Task) -> list[Violation]:
    v = []
    for m in _COMPOUND_MARKERS:
        if m in task.goal:
            v.append(
                Violation(
                    "E007",
                    f"goal 含并列标记 '{m}'：「{task.goal}」",
                    task.id,
                    hint="拆成两个任务；一个 goal 只留一个动词",
                )
            )
            break
    return v


def check_deps(tasks: Sequence[Task]) -> list[Violation]:
    v = []
    ids = {t.id for t in tasks}
    for t in tasks:
        if t.id in t.deps:
            v.append(Violation("E009", "任务依赖自己", t.id))
        for d in t.deps:
            if d not in ids:
                v.append(
                    Violation("E008", f"deps 指向不存在的任务 '{d}'", t.id)
                )
    v.extend(check_cycles(tasks))
    return v


def check_cycles(tasks: Sequence[Task]) -> list[Violation]:
    """三色 DFS；发现环时回报完整环路径，方便直接贴给 Planner 看。"""
    ids = {x.id for x in tasks}
    # 自依赖由 E009 单独负责，这里剔掉，避免同一问题报两种错
    graph = {t.id: [d for d in t.deps if d in ids and d != t.id] for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in graph}
    stack: list[str] = []
    out: list[Violation] = []

    def dfs(u: str) -> bool:
        color[u] = GRAY
        stack.append(u)
        for w in graph.get(u, ()):
            if color.get(w, BLACK) == GRAY:
                i = stack.index(w)
                cyc = " → ".join(stack[i:] + [w])
                out.append(
                    Violation("E010", f"依赖成环：{cyc}", w, hint="环上任意一条边删掉或反转")
                )
                return True
            if color.get(w, BLACK) == WHITE and dfs(w):
                return True
        stack.pop()
        color[u] = BLACK
        return False

    for k in graph:
        if color[k] == WHITE and dfs(k):
            break
    return out


def _reaches(deps: dict[str, list[str]], start: str, target: str) -> bool:
    stack, seen = [start], set()
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(deps.get(cur, ()))
    return False


def check_read_write(tasks: Sequence[Task], repo_files: Iterable[str] = ()) -> list[Violation]:
    """A 写 X、B 读 X，但 B 不依赖 A —— B 会读到旧值。

    写集冲突（E013）管的是"两个人同时写同一个文件"，这条管的是
    "一个人读另一个人还没写完的文件"。后者更隐蔽：文件不同、不冲突、
    合并也不报错，只是 B 基于一份过时的代码做了决定。
    """
    v: list[Violation] = []
    ids = {t.id for t in tasks}
    deps = {t.id: [d for d in t.deps if d in ids] for t in tasks}

    for a in tasks:
        for b in tasks:
            if a.id == b.id:
                continue
            hit, why = writes_overlap(a.writes, b.reads, repo_files)
            if hit and not _reaches(deps, b.id, a.id):
                v.append(
                    Violation(
                        "E019",
                        f"{b.id} 读了 {a.id} 要写的文件（{why}），但没有依赖它",
                        b.id,
                        hint=f"给 {b.id} 的 deps 加上 {a.id}，否则会基于旧版本做判断",
                    )
                )
    return v


@dataclass(frozen=True)
class Repair:
    """代码自动补的修正。记进 trace，避免"系统悄悄改了计划"这种不可归因。"""

    code: str
    task_id: str
    added_dep: str

    def __str__(self) -> str:
        return f"{self.code} {self.task_id} += deps[{self.added_dep}]"


def repair_dependencies(
    tasks: Sequence[Task], repo_files: Iterable[str] = ()
) -> tuple[list[Task], list[Repair]]:
    """B 读了 A 要写的文件却没依赖 A —— 直接补上这条边。

    这不是判断，是推导：既然 B 要读到 A 的结果，它就必须排在 A 后面。
    让模型为这种事重试三次，既浪费又不可靠；代码能做对的事就该代码做。
    唯一需要放弃的情况：补上会成环（那说明拆解本身有问题，交给校验器拒）。
    """
    import dataclasses

    out = list(tasks)
    repairs: list[Repair] = []

    for _ in range(len(tasks) * 2 + 2):
        deps = {t.id: list(t.deps) for t in out}
        fixed = False
        for i, b in enumerate(out):
            for a in out:
                if a.id == b.id:
                    continue
                hit, _why = writes_overlap(a.writes, b.reads, repo_files)
                if not hit or _reaches(deps, b.id, a.id):
                    continue
                # 如果反向也缺一条边（互相读对方的产物），补任何一条都不完整，
                # 剩下的那条必然成环 —— 这种交给校验器报，别做半吊子修复。
                reverse_missing = writes_overlap(b.writes, a.reads, repo_files)[0] and not _reaches(
                    deps, a.id, b.id
                )
                if reverse_missing:
                    continue
                cand = dataclasses.replace(b, deps=b.deps + (a.id,))
                trial = out[:i] + [cand] + out[i + 1 :]
                if check_cycles(trial):
                    continue  # 会成环，别修，让校验器报出来
                out = trial
                repairs.append(Repair("E019", b.id, a.id))
                fixed = True
                break
            if fixed:
                break
        if not fixed:
            break
    return out, repairs


def topo_layers(tasks: Sequence[Task]) -> list[list[str]]:
    """按最长路径分层；层内可并发，层间是屏障。有环则返回 []。"""
    by_id = {t.id: t for t in tasks}
    depth: dict[str, int] = {}

    def d(tid: str, seen: frozenset[str] = frozenset()) -> int:
        if tid in depth:
            return depth[tid]
        if tid in seen:  # 环
            return 0
        deps = [x for x in by_id[tid].deps if x in by_id]
        depth[tid] = 0 if not deps else max(d(x, seen | {tid}) for x in deps) + 1
        return depth[tid]

    for t in tasks:
        d(t.id)
    if not depth:
        return []
    layers: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
    for tid, k in depth.items():
        layers[k].append(tid)
    return [sorted(l) for l in layers]


# --------------------------------------------------------------------------
# 4. 写集校验
# --------------------------------------------------------------------------

def _norm(p: str) -> str:
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/") or "."


def has_wildcard(p: str) -> bool:
    return any(c in p for c in "*?[")


def dir_prefix(p: str) -> str:
    """通配符之前的目录部分。'src/auth/**' -> 'src/auth'；'**/*.py' -> ''"""
    p = _norm(p)
    cut = len(p)
    for i, c in enumerate(p):
        if c in "*?[":
            cut = i
            break
    head = p[:cut]
    return head.rsplit("/", 1)[0] if "/" in head else ""


def prefixes_compatible(a: str, b: str) -> bool:
    if a == b or a == "" or b == "":
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def expand_writes(patterns: Iterable[str], repo_files: Iterable[str]) -> set[str]:
    """把写集展开成具体文件集合；通配符**同时**保留哨兵。

    为什么命中后还要留哨兵：`src/*.py` 命中已有的 old.py，不代表它不会命中
    将来才创建的 new.py。只要丢掉哨兵，两个任务就会被判成不冲突，
    然后同时去写同一批文件——这正是原基线漏判的根因。
    """
    files = {_norm(f) for f in repo_files}
    out: set[str] = set()
    for p in patterns:
        p = _norm(p)
        if has_wildcard(p):
            out |= {f for f in files if fnmatchcase(f, p)}
            out.add(f"~glob~{p}")
        else:
            out.add(p)
    return out


def writes_overlap(
    a: Sequence[str], b: Sequence[str], repo_files: Iterable[str] = ()
) -> tuple[bool, str]:
    """两两判断写集是否可能重叠。宁可误报，不可漏报。"""
    ea, eb = expand_writes(a, repo_files), expand_writes(b, repo_files)

    concrete = {x for x in ea if not x.startswith("~glob~")} & {
        y for y in eb if not y.startswith("~glob~")
    }
    if concrete:
        return True, f"同一文件：{sorted(concrete)[0]}"

    # 目录写 vs 目录内的文件：'src' 与 'src/a.py' 必须算冲突。
    # 原实现只比字面量相等，目录写法会漏判，结果两个任务被放进同一层。
    for x in ea:
        if x.startswith("~glob~"):
            continue
        for y in eb:
            if y.startswith("~glob~"):
                continue
            if x == y:
                continue
            if y.startswith(x.rstrip("/") + "/") or x.startswith(y.rstrip("/") + "/"):
                return True, f"目录包含：{x} ⊃ {y}"

    pa = [x[6:] for x in ea if x.startswith("~glob~")]
    pb = [x[6:] for x in eb if x.startswith("~glob~")]
    # 通配符 vs 通配符 / 通配符 vs 字面量：比较目录前缀
    for x in list(ea) + pa:
        for y in list(eb) + pb:
            xr = x[6:] if x.startswith("~glob~") else x
            yr = y[6:] if y.startswith("~glob~") else y
            if not (has_wildcard(xr) or has_wildcard(yr)):
                continue
            if prefixes_compatible(dir_prefix(xr), dir_prefix(yr)):
                return True, f"目录前缀重叠：{xr} × {yr}"
    return False, ""


def check_writes(tasks: Sequence[Task], repo_files: Iterable[str] = ()) -> list[Violation]:
    v: list[Violation] = []
    files = tuple(repo_files)

    for t in tasks:
        if not t.writes:
            v.append(
                Violation(
                    "E011",
                    "writes 为空",
                    t.id,
                    hint="写不出 writes 的任务不该被拆出来；至少声明要新建/修改的文件",
                )
            )
        for p in list(t.writes) + list(t.reads) + list(t.forbidden):
            if _ESCAPE_PATTERNS.search(_norm(p)):
                v.append(
                    Violation(
                        "E015",
                        f"路径越界：'{p}'",
                        t.id,
                        hint="只允许仓库内相对路径；绝对路径和 .. 一律拒绝",
                    )
                )
        # 写集是整个仓库 = 声明"我会改任意文件"，隔离和冲突检测都失去意义。
        # 这是真实运行里最常见的偷懒写法（Planner 想少写几个路径）。
        for p in t.writes:
            if has_wildcard(p) and dir_prefix(p) == "":
                v.append(
                    Violation(
                        "E018",
                        f"writes 覆盖整个仓库：'{p}'",
                        t.id,
                        hint="写成具体路径或具体子目录（如 app.py、src/auth/**），否则后续任务无法判冲突",
                    )
                )
        for p in t.reads:
            if has_wildcard(p) and dir_prefix(p) == "":
                v.append(
                    Violation(
                        "W003",
                        f"reads 覆盖整个仓库：'{p}'",
                        t.id,
                        hint="读全部文件会挤爆上下文预算；只读真正需要的",
                    )
                )
        for w in t.writes:
            for f in t.forbidden:
                wn, fn_ = _norm(w), _norm(f)
                hit = fnmatchcase(wn, fn_) or wn == fn_
                # 写一整个目录，而该目录里有被禁文件：等于把 forbidden 声明架空。
                if not hit and fn_.startswith(wn.rstrip("/") + "/") and not has_wildcard(wn):
                    hit = True
                if hit:
                    v.append(
                        Violation(
                            "E016",
                            f"writes 的 '{w}' 命中 forbidden 的 '{f}'",
                            t.id,
                            hint="目标文件被自己禁了，契约自相矛盾；收窄 writes 或去掉 forbidden",
                        )
                    )

    for i, a in enumerate(tasks):
        for b in tasks[i + 1 :]:
            hit, why = writes_overlap(a.writes, b.writes, files)
            if hit:
                v.append(
                    Violation(
                        "E013",
                        f"{a.id} × {b.id} 写集重叠（{why}）",
                        a.id,
                        hint="合并重叠写入为同一任务，或调整文件边界后重拆；当前加依赖仍不放行同文件多任务",
                    )
                )

    if files:
        for t in tasks:
            if t.reads and not any(
                fnmatchcase(f, _norm(p)) for p in t.reads for f in files
            ):
                v.append(
                    Violation(
                        "W002",
                        f"reads 未命中任何文件：{list(t.reads)}",
                        t.id,
                        hint="路径写错，或读了不存在的文件；这会让 worker 拿到空上下文",
                    )
                )
    return v


# --------------------------------------------------------------------------
# 5. 判据校验：能否落到「可执行的谓词」
# --------------------------------------------------------------------------

_CMD = re.compile(
    r"^\s*(pytest|python3?\s+-m\s+\S+|python3?\s+-c\b|python3?\s+\S+\.py|npm\s+(run\s+)?\S+|pnpm\s+\S+|"
    r"yarn\s+\S+|go\s+test|cargo\s+(test|build)|jest|vitest|make\s+[\w.-]+|bash\s+\S+\.sh|"
    r"ruff|eslint|tsc|mypy|git\s+\S+)\b"
)
# "命令 `git diff --name-only` 的输出仅含 storage.py" —— 命令 + 期望输出。
# 放在契约层而不是验收器里，是因为"可不可判定"和"怎么执行"必须用同一个定义，
# 否则会出现：校验通过、执行时却当成断言型交给模型（正是这个 bug 的来源）。
_OUT_SPEC = re.compile(r"`([^`]+)`[^`]*?输出\s*(仅|只)?\s*(含|包含|为|等于|是)\s*(.+?)\s*[。.]?$")

_PREDICATE = (
    "返回", "含", "包含", "不包含", "不含", "等于", "==", "!=", ">=", "<=",
    "通过", "存在", "不存在", "未修改", "未被修改", "不得", "没有",
    "退出码", "状态码", "响应码", "行数", "不超过", "至少", "最多", "大于", "小于",
)
# 锚点 = 判据里那个"能被机器定位"的东西。
# 除了数字/反引号/函数调用/路径，还要认**裸文件名**和**引号里的字面量**——
# 人写判据时经常写 `storage.py 含 …`，这完全可查，不该判成不可判定。
_ANCHOR = re.compile(
    r"\d"
    r"|`[^`]+`"
    r"|[A-Za-z_][\w.]*\(\)"
    r"|/[\w.*-]+"
    r"|::"
    r"|[\w-]+\.(?:py|js|ts|tsx|jsx|mjs|md|json|ya?ml|toml|txt|cfg|ini|html|css|go|rs|java|sql|sh)\b"
    r"|「[^」]{1,60}」"
    r"|\"[^\"]{1,60}\""
)
_VAGUE = (
    "正常", "合理", "良好", "友好", "完善", "美观", "优雅", "流畅",
    "尽量", "适当", "明显", "稳定", "健壮", "清晰", "简洁", "一致", "可用",
)


def is_decidable(criterion: str) -> tuple[bool, str]:
    """判据可判定 = 能落到一条可执行谓词上。返回 (是否可判定, 理由)。"""
    text = criterion.strip()
    if not text:
        return False, "空判据"

    if _OUT_SPEC.search(text):
        return True, "命令+期望输出型"

    if _CMD.match(text):
        return True, "命令型"

    has_pred = any(p in text for p in _PREDICATE)
    has_anchor = bool(_ANCHOR.search(text))
    if has_pred and has_anchor:
        return True, "断言型"

    vague = [w for w in _VAGUE if w in text]
    if vague:
        return False, f"含模糊词「{vague[0]}」且无阈值/路径锚点"
    if not has_pred:
        return False, "没有可判定的谓词（返回/含/等于/退出码/未被修改…）"
    return False, "有谓词但没有锚点（数字、路径、`符号`）"


_CMD_PREFIX = re.compile(r"^\s*(命令|运行|执行)\s*[:：]?\s*")


def _looks_command(criterion: str) -> bool:
    """这条判据是不是"跑一下就知道"的那类。

    和 verifier.command_of 保持同一口径：命令型与"命令+期望输出"型都算。
    放在这里是因为"计划强度对比"必须在契约层就能做，不该依赖验收器。
    """
    text = criterion.strip()
    if _OUT_SPEC.search(text):
        return True
    # 带引号的绝对路径也是命令（文档任务的判据常见这种）
    if re.match(r'^\s*"[^"]+\.(?:exe|bat|cmd|ps1|py)"', text, re.I):
        return True
    m = re.search(r"`([^`]+)`", text)
    if m:
        text = m.group(1).strip()
    else:
        text = _CMD_PREFIX.sub("", text)
    return bool(_CMD.match(text))


def compare_plan_strength(before: Sequence[Task], after: Sequence[Task]) -> list[Violation]:
    """修补前后对比：**只许变强，不许变松**。

    这道门的存在理由很具体：修补由便宜模型做，它最省力的路径是
    "把判据改松来让自己通过"。所以修补不能只看"能不能过校验"，
    还要看"是不是靠降低标准才过的"。

    三条都用**总量**比较，不做任务级对齐——修补允许拆分任务，逐 id 比会误报：
      E024  判据总数不得减少
      E025  写入集并集不得扩大
      E026  命令型判据不得减少（不能把"跑一下就知道"降级成"模型判"）
    """
    vs: list[Violation] = []

    b_crit = sum(len(t.acceptance) for t in before)
    a_crit = sum(len(t.acceptance) for t in after)
    if a_crit < b_crit:
        vs.append(Violation(
            "E024", f"修补后判据总数从 {b_crit} 降到 {a_crit}",
            hint="修补只能补锚点或拆任务，不能删判据、也不能把多条合成一条"))

    b_writes = {p for t in before for p in t.writes}
    a_writes = {p for t in after for p in t.writes}
    extra = sorted(a_writes - b_writes)
    if extra:
        vs.append(Violation(
            "E025", f"修补后扩大写入范围：{'、'.join(extra[:3])}",
            hint="writes 只能不变或变小；要改更多文件就让 Codex 重新出计划"))

    b_cmd = sum(1 for t in before for c in t.acceptance if _looks_command(c))
    a_cmd = sum(1 for t in after for c in t.acceptance if _looks_command(c))
    if b_cmd and a_cmd < b_cmd:
        vs.append(Violation(
            "E026", f"修补后命令型判据从 {b_cmd} 降到 {a_cmd}",
            hint="命令型不能改写成断言型；那会把可机器核对的判据变成模型判断"))
    return vs


def check_acceptance(tasks: Sequence[Task]) -> list[Violation]:
    v: list[Violation] = []
    for t in tasks:
        if not t.acceptance:
            v.append(
                Violation(
                    "E012",
                    "acceptance 为空",
                    t.id,
                    hint="写不出可判定判据 = 还没想清楚这个任务要交付什么",
                )
            )
            continue
        for c in t.acceptance:
            ok, why = is_decidable(c)
            if not ok:
                v.append(
                    Violation(
                        "E014",
                        f"判据不可判定：「{c}」（{why}）",
                        t.id,
                        hint="改写成：测试命令 / 命令+期望输出 / 文件断言 三者之一",
                    )
                )
    return v


# --------------------------------------------------------------------------
# 6. 总入口
# --------------------------------------------------------------------------

MODES = ("auto", "single", "parallel", "chain")

MODE_HINT = "mode 只能取 auto / single / parallel / chain"


def check_mode(mode: str | None, tasks: Sequence[Task]) -> list[Violation]:
    """执行模式是**承诺**，不是标签。

    auto  = 按依赖分层，能并行就并行（默认）
    single = 只有一件事，一个任务
    chain  = 严格一串，后一步必须看得见前一步

    校验的意义在于：模式选错了，执行形态就会错。声明 single 却给 4 个任务，
    说明需求没想清楚；声明 chain 却同层铺开，说明没读懂"必须串行"。
    """
    if mode in (None, ""):
        return []
    if mode not in MODES:
        return [Violation("E021", f"未知 mode：{mode}", hint=MODE_HINT)]
    if mode == "auto":
        return []
    if not tasks:
        return []

    v: list[Violation] = []
    # E023：single 是"只有一件事"的承诺，多给任务就是没想清楚
    if mode == "single" and len(tasks) > 1:
        v.append(Violation(
            "E023", f"mode=single 却有 {len(tasks)} 个任务",
            hint="单交付物就该是一个任务；确实要分开做就改 parallel 或 chain"))
    # E022：chain 是"后一步要看前一步产物"的承诺。
    # 光有 deps（先后）不够——deps 只保证顺序，不保证它读了什么。
    if mode == "chain":
        try:
            layers = topo_layers(tasks)
        except Exception:            # noqa: BLE001  环由 E009 负责报
            return v
        wide = [l for l in layers if len(l) > 1]
        if wide:
            v.append(Violation(
                "E022", f"mode=chain 但第 {layers.index(wide[0]) + 1} 层有 {len(wide[0])} 个任务",
                hint="串联模式每层只能一个任务；能并行的部分请改成 parallel"))
        roots = [t for t in tasks if not t.deps]
        for t in tasks:
            if roots and t is roots[0]:
                continue                     # 起点没有前一步可读
            if not t.inputs_from:
                v.append(Violation(
                    "E022", f"{t.id} 在 chain 模式里没有 inputs_from",
                    t.id, hint="串联必须写明读哪个任务的产物，否则等于没接上"))
    return v


def validate_tasks(
    tasks: Sequence[Task], base_violations: Iterable[Violation] = (),
    repo_files: Iterable[str] = (), mode: str | None = None
) -> Report:
    """对已解析（可能已被修复）的任务列表做全套校验。"""
    tasks = list(tasks)
    v: list[Violation] = list(base_violations)
    if tasks:
        v += check_goal_all(tasks)
        v += check_deps(tasks)
        v += check_writes(tasks, repo_files)
        v += check_read_write(tasks, repo_files)
        v += check_acceptance(tasks)
        v += check_needs(tasks)
    # mode 的合法性不依赖任务数：写了拼错的模式名，任务为空也要报
    v += check_mode(mode, tasks)
    rep = Report(tasks=tasks, violations=v)
    if rep.ok and tasks:
        rep.layers = topo_layers(tasks)
        # 判据强度：一条能跑的命令都没有 → 验收只能"看着像对"。
        # 只在计划本身合法时提示：计划已经错了，再多一条提示只会淹没错误码。
        # 实测过一次代价——判据只查 docstring，worker 直接反问"到底要什么行为"，
        # 任务停在待澄清。弱判据不是"也能过"，是"过不了也看不出来"。
        if not any(_looks_command(c) for t in tasks for c in t.acceptance):
            rep.violations.append(Violation(
                "W006",
                "所有判据都不是命令型：验收只能靠模型判断，无法确认行为",
                hint="至少给一条能跑的命令（如 pytest / 脚本断言），否则改错了也会 PASS"))
    return rep


# --------------------------------------------------------------------------
# 4.5 needs 校验：双向
#      forward —— 声明了但任务内容不需要（虚声明）→ 白付一个能力约束，可能把任务
#                 挤到贵的 worker 上，也可能被用来绕过路由
#      backward —— 任务内容明显需要但没有声明 → 执行时才发现跑不通，
#                 而这时候钱已经花了
#     两条都只 warn：needs 是提示不是证明，误报的代价是看一行字。
# --------------------------------------------------------------------------

_NEED_HINTS: dict[str, re.Pattern[str]] = {
    "web": re.compile(
        r"https?://|\b(search|fetch|crawl|scrape|browse)\b|搜索|检索|抓取|爬取|联网|官网|"
        r"最新版本|发行说明|release[ _-]?notes|官方文档|支持哪些|有哪些|调研|查阅",
        re.I,
    ),
    # db 的线索必须指向**真操作**：单独出现「数据库」三个字往往是描述对象，
    # 不是「我要连它」。这里误报一次，用户就会开始无视所有 needs 警告。
    "db": re.compile(
        r"\bsql(?:ite3?)?\b|\bpostgres(?:ql)?\b|\bmysql\b|\bsqlalchemy\b|\bjdbc\b|\bodbc\b|"
        r"(?:查询|连接|读取|写入|汇总|聚合|导|灌).{0,8}(?:数据库|数据表|库表)|"
        r"(?:数据库|数据表|表结构).{0,6}(?:查询|连接|导出|schema)|表结构",
        re.I,
    ),
    "docs": re.compile(r"腾讯文档|知识库|网盘|共享文档|在线文档|docs\.qq\.com", re.I),
    "net-exec": re.compile(
        r"\b(pip install|uv add|npm install|pnpm add|curl|wget|git (?:push|fetch|clone|pull)|gh )", re.I
    ),
}


def _need_evidence(task: Task, need: str) -> str | None:
    """返回第一处支持该 need 的证据；没有则 None。"""
    pat = _NEED_HINTS.get(need)
    if pat is None:
        return None
    for field_name, texts in (
        ("goal", (task.goal,)),
        ("acceptance", task.acceptance),
        ("context_refs", task.context_refs),
    ):
        for t in texts:
            if pat.search(t or ""):
                snippet = (t or "").strip()
                return f"{field_name}: {snippet[:50]}"
    return None


def check_needs(tasks: Sequence[Task]) -> list[Violation]:
    out: list[Violation] = []
    for t in tasks:
        declared = set(t.needs)

        # forward：声明了但找不到任何证据
        for need in sorted(declared):
            if _need_evidence(t, need) is None:
                out.append(
                    Violation(
                        "W004",
                        f"needs 声明了 '{need}'，但 goal / acceptance / context_refs 里没有对应线索",
                        t.id,
                        hint="虚声明会把任务挤到有能力的那档；确认真的需要，或删掉这个 needs",
                    )
                )

        # backward：内容需要但没声明
        for need in CAPABILITIES:
            if need in declared:
                continue
            ev = _need_evidence(t, need)
            if ev is not None:
                out.append(
                    Violation(
                        "W005",
                        f"任务看起来需要 '{need}'（{ev}），但没有在 needs 里声明",
                        t.id,
                        hint=f"补上 \"needs\": [\"{need}\"]，否则路由会按「不需要外部能力」分配",
                    )
                )
    return out





def validate_document(doc: Any, repo_files: Iterable[str] = ()) -> Report:
    tasks, v = parse_document(doc)
    mode = doc.get("mode") if isinstance(doc, dict) else None
    if mode is not None and not isinstance(mode, str):
        v.append(Violation("E021", f"mode 必须是字符串，收到 {type(mode).__name__}", hint=MODE_HINT))
        mode = None
    return validate_tasks(tasks, v, repo_files, mode=mode)


def check_goal_all(tasks: Sequence[Task]) -> list[Violation]:
    out: list[Violation] = []
    for t in tasks:
        out += check_goal(t)
    return out


def validate_file(path: str | Path, repo_files: Iterable[str] = ()) -> Report:
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return Report(
            violations=[
                Violation(
                    "E000",
                    f"JSON 解析失败：第 {e.lineno} 行第 {e.colno} 列 {e.msg}",
                    hint="LLM 常输出带尾逗号 / 注释的 JSON；先跑一次宽松修复或让它重写",
                )
            ]
        )
    return validate_document(doc, repo_files)


def repo_snapshot(root: str | Path) -> list[str]:
    """取仓库文件清单（相对路径），供写集展开用。"""
    root = Path(root)
    if not root.exists():
        return []
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        out.append(rel)
    return out
