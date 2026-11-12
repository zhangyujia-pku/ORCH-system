"""异构 worker 路由：谁有能力做，以及谁做最省。

关键立场：**这不是强弱排序，是能力差集。**

Codex 在纯代码上最强，但在联网任务上它不是「贵」，是**不可用**——
`workspace-write` 沙箱默认关网络，web search 默认只查 OpenAI 维护的索引缓存，
要实时抓取得显式 `--search`。把联网调研派给 Codex 不是浪费额度，
是拿一个自信的错答案回来。

反过来，DSH 的数据库插件（SQLite / PostgreSQL / MySQL，只读查询 + 写审批 + 审计）
和 WorkBuddy 的 `WebFetch`（真抓正文）是 Codex 根本没有的东西。
所以路由必须先做**能力过滤（硬约束）**，再在有能力的那几个里比**成本（次约束）**。
顺序颠倒就会写成「便宜优先、能力将就」——那正是最贵的路由。

三层依据，按可靠性排：
  1. `task.needs`  —— Planner 显式声明「需要什么」，但**不能声明给谁**。
     声明需求是事实判断，分配 worker 是花钱决策，两种权力必须分开。
  2. 写集 / 判据   —— 契约里已有的客观字段，不依赖任何人的自我评估。
  3. 注册表 caps   —— 每个 harness 实测出来的能力等级：native / plugin / none。

`plugin` 默认**不算可用**：装了/配了才置 `available: true`。
「假设它应该有」是这类系统最常见的翻车方式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .contract import CAPABILITIES, Task
from .verifier import is_command_criterion

# 只写这些后缀的任务，算「文档类」——小模型能接。
DOC_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc"}

# 机械类任务的写集上限。超过这个数就不该按「简单任务」处理——
# 多文件改动需要全局观，小模型和便宜档都会漏改。
MECHANICAL_MAX_FILES = 3

# 能力等级。none < plugin < native，数字只用于比较，不用于排序 worker。
LEVEL_RANK = {"none": 0, "plugin": 1, "native": 2}

REASONS: dict[str, str] = {
    "C01": "需要实时联网：只有 native 联网档能拿回真实页面",
    "C02": "需要数据库：只有接了 db 的档能查",
    "C03": "需要企业文档平台",
    "C04": "需要命令出网（装依赖 / curl / push）",
    "C05": "需要多项外部能力，按能力集交集选",
    "C09": "能力缺口：没有任何 worker 具备这些能力，计划必须改",
    "C10": "能力有、但没有已核验的档：argv 没实跑过的 harness 不进估算",
    "R01": "文档类：写集全是文本文件，且不超过 2 个",
    "R02": "机械类：代码任务、写集 ≤ 3 个文件、判据全是可执行命令",
    "R03": "需语义判断：存在非命令型判据（文件断言 / 命令+期望）",
    "R04": "写集较大：超过机械任务上限，需要能扛多文件的一档",
    "R05": "无匹配规则，退回末位",
}

# 单项能力 → 对应的主理由码；两项以上走 C05。
_SINGLE_CODE = {"web": "C01", "db": "C02", "docs": "C03", "net-exec": "C04"}

# 能力 → 必须保留的工具。工具裁剪和 needs 用同一张表，
# 这样「声明需要什么」和「给它开什么」不会各说一套。
TOOLS_BY_NEED: dict[str, tuple[str, ...]] = {
    "web": ("WebSearch", "WebFetch"),
    "db": (),          # MCP 工具名随服务器而定，不在这里硬编码
    "docs": (),        # 同上：走连接器，工具名由平台注入
    "net-exec": ("Bash",),
}

_BASE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep")


@dataclass(frozen=True)
class Worker:
    name: str
    harness: str
    model: str | None
    quota: float                              # 相对 codex 的额度权重，codex = 1.0
    max_files: int
    handles_docs: bool
    handles_code: bool
    handles_semantic: bool
    caps: dict[str, str] = field(default_factory=dict)   # 能力 → native/plugin/none
    available: dict[str, bool] = field(default_factory=dict)  # plugin 能力是否已配好
    tools_selectable: bool = False            # harness 是否支持 --tools 裁剪
    argv: tuple[str, ...] = ()
    argv_verified: bool = True
    note: str = ""

    def level(self, cap: str) -> str:
        """某项能力的实际可用等级。plugin 未配置即视为 none。"""
        lv = self.caps.get(cap, "none")
        if lv == "plugin" and not self.available.get(cap, False):
            return "none"
        return lv

    def has(self, cap: str) -> bool:
        return self.level(cap) != "none"

    @property
    def cap_list(self) -> tuple[str, ...]:
        return tuple(c for c in CAPABILITIES if self.has(c))


@dataclass(frozen=True)
class Assignment:
    task_id: str
    worker: str
    reason: str                       # C0x / R0x
    detail: str = ""
    needs: tuple[str, ...] = ()
    gap: tuple[str, ...] = ()         # 无人能满足的能力（非空即必须改计划）
    alternatives: tuple[str, ...] = ()  # 同能力的备选，失败时按顺序换，不按价格升级
    blocked_reason: str = ""

    @property
    def reason_text(self) -> str:
        return REASONS.get(self.reason, self.reason)

    @property
    def routable(self) -> bool:
        """能不能直接发车。C10 是「有能力但没核验」，也不算能发。"""
        return bool(self.worker) and not self.gap and self.reason != "C10" and not self.blocked_reason


DEFAULT_LADDER = ("local", "dsh", "cbc", "codex")

# 没有 workers.json 时的兜底，保证模块可独立导入和测试。
_FALLBACK: dict[str, Worker] = {
    "local": Worker("local", "ollama", "qwen3:4b", 0.0, 2, True, False, False,
                    caps={"web": "none", "db": "none", "docs": "none", "net-exec": "none"}),
    "dsh": Worker("dsh", "deepseek-harness", "deepseek-v4-pro", 0.2, 3, True, True, True,
                  caps={"web": "plugin", "db": "plugin", "docs": "none", "net-exec": "native"},
                  available={"web": True, "db": False}),
    "cbc": Worker("cbc", "workbuddy-cli", "deepseek-v4.1-flash", 0.3, 8, True, True, True,
                  caps={"web": "native", "db": "plugin", "docs": "native", "net-exec": "native"},
                  available={"db": False}, tools_selectable=True),
    "codex": Worker("codex", "codex-cli", None, 1.0, 999, True, True, True,
                    caps={"web": "plugin", "db": "none", "docs": "none", "net-exec": "plugin"},
                    available={"web": False, "net-exec": False}),
}


def load_workers(path: str | Path | None = None, strict: bool = False) -> tuple[dict[str, Worker], tuple[str, ...]]:
    """读 workers.json；读不到就用内置兜底，不抛异常。"""
    if path is None:
        path = Path(__file__).resolve().parent.parent / "workers.json"
    p = Path(path)
    if not p.exists():
        if strict:
            raise ValueError(f"后端配置不存在：{p}")
        return dict(_FALLBACK), DEFAULT_LADDER

    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if strict:
            raise ValueError(f"后端配置无法解析：{p}")
        return dict(_FALLBACK), DEFAULT_LADDER

    if not isinstance(doc, dict) or not isinstance(doc.get("workers"), dict):
        raise ValueError("后端配置 workers 必须为对象")

    workers: dict[str, Worker] = {}
    for name, raw in (doc.get("workers") or {}).items():
        if not isinstance(raw, dict):
            continue
        argv = raw.get("argv") or []
        caps = {str(k): str(v) for k, v in (raw.get("caps") or {}).items()}
        avail = {str(k): bool(v) for k, v in (raw.get("available") or {}).items()}
        workers[name] = Worker(
            name=name,
            harness=str(raw.get("harness") or name),
            model=raw.get("model"),
            quota=float(raw.get("quota", 1.0)),
            max_files=int(raw.get("max_files", 2)),
            handles_docs=bool(raw.get("handles_docs", False)),
            handles_code=bool(raw.get("handles_code", True)),
            handles_semantic=bool(raw.get("handles_semantic", False)),
            caps=caps,
            available=avail,
            tools_selectable=bool(raw.get("tools_selectable", False)),
            argv=tuple(str(a) for a in argv),
            argv_verified=bool(raw.get("argv_verified", False)),
            note=str(raw.get("note") or ""),
        )

    ladder = tuple(str(x) for x in (doc.get("ladder") or DEFAULT_LADDER))
    if not workers:
        if strict:
            raise ValueError("后端配置为空")
        return dict(_FALLBACK), DEFAULT_LADDER
    if strict and any(name not in workers for name in ladder):
        raise ValueError("后端顺序表引用了未注册后端")
    return workers, ladder


def _suffix(path: str) -> str:
    return Path(path).suffix.lower()


def is_docs_only(task: Task) -> bool:
    return bool(task.writes) and all(_suffix(w) in DOC_SUFFIXES for w in task.writes)


def count_command_criteria(task: Task) -> int:
    return sum(1 for a in task.acceptance if is_command_criterion(a))


def has_semantic_criteria(task: Task) -> bool:
    return count_command_criteria(task) < len(task.acceptance)


def covers(worker: Worker, needs: Iterable[str]) -> bool:
    return all(worker.has(c) for c in needs)


def capable_workers(workers: dict[str, Worker], ladder: Sequence[str],
                    needs: Iterable[str], require_verified: bool = True) -> list[Worker]:
    """具备全部所需能力的 worker，按成本升序（同价按梯队顺序，保证结果稳定）。

    `require_verified` 默认开：argv 没在本机实跑过的 harness 不进候选。
    估算里混进一个可能根本起不来的档，省下来的额度是纸面上的。
    """
    order = {n: i for i, n in enumerate(ladder)}
    pool = [w for w in workers.values() if covers(w, needs)]
    if require_verified:
        pool = [w for w in pool if w.argv_verified]
    return sorted(pool, key=lambda w: (w.quota, order.get(w.name, len(order)), w.name))


def missing_caps(workers: dict[str, Worker], needs: Iterable[str]) -> tuple[str, ...]:
    """整个注册表都满足不了的能力。非空 = 计划必须改，不是换个 worker 能解决的。"""
    return tuple(c for c in needs if not any(w.has(c) for w in workers.values()))


def _reason_for(needs: Sequence[str]) -> str:
    if not needs:
        return ""
    if len(needs) == 1:
        return _SINGLE_CODE.get(needs[0], "C05")
    return "C05"


def _detail_for_caps(needs: Sequence[str], pick: Worker) -> str:
    parts = [f"{c}={pick.level(c)}" for c in needs]
    return " ".join(parts)


def route_one(task: Task, workers: dict[str, Worker], ladder: Sequence[str],
              require_verified: bool = True) -> Assignment:
    """单任务路由。

    两步，不可颠倒：
      1. 能力过滤 —— 硬约束，不满足就出局，跟价格无关。
      2. 成本排序 —— 只在剩下的人里比便宜。
    """
    needs = tuple(c for c in CAPABILITIES if c in task.needs)
    n_files = len(task.writes)
    docs_only = is_docs_only(task)
    semantic = has_semantic_criteria(task)

    # ---- 第一步：能力（硬约束）----
    gap = missing_caps(workers, needs)
    if gap:
        return Assignment(task.id, "", "C09", f"缺失：{', '.join(gap)}", needs=needs, gap=gap)

    pool = capable_workers(workers, ladder, needs, require_verified)
    if not pool and needs:
        # 有能力，但没有已核验的档——不能拿没跑过的 harness 出估算
        raw = capable_workers(workers, ladder, needs, require_verified=False)
        if raw:
            return Assignment(task.id, raw[0].name, "C10",
                              f"{raw[0].name} 具备 {', '.join(needs)}，但 argv 未在本机核验",
                              needs=needs)
        return Assignment(task.id, "", "C09", "无可用档", needs=needs, gap=needs)
    if not pool:  # 不需要外部能力，但一个已核验的档都没有
        return Assignment(task.id, ladder[-1] if ladder else "codex", "C10",
                          "没有已核验的档", needs=needs)

    # ---- 规格过滤：能扛写集、能改代码、能判语义 ----
    # 这三条和「能力」一样是硬约束，但它们是「做不到」而不是「没有」，
    # 所以理由码要分开报——R04 说的是规格不够，不是能力缺口。
    def _fits(w: Worker) -> bool:
        if w.max_files < n_files:
            return False
        if docs_only and not w.handles_docs:
            return False
        if (not docs_only or needs) and not w.handles_code:
            return False
        # 语义判据卡的是「产出要能满足需要理解的验收」，所以只对代码任务生效。
        # 文档类任务的判据形式是「文件里含某个词」，worker 不需要理解它——
        # 真正需要理解的是 verifier。在这一格卡小模型，等于用错了约束。
        if semantic and not w.handles_semantic and not docs_only:
            return False
        return True

    fits = [w for w in pool if _fits(w)]
    size_binding = any(w.max_files < n_files for w in pool) and bool(fits)

    if not fits:
        detail = f"无候选同时满足任务规格（写集 ×{n_files}、代码支持、语义支持）"
        return Assignment(task.id, "", "R04", detail, needs=needs, blocked_reason=detail)

    # ---- 成本（次约束）----
    pick = fits[0]
    alts = tuple(w.name for w in fits[1:])

    if needs:
        return Assignment(task.id, pick.name, _reason_for(needs),
                          _detail_for_caps(needs, pick), needs=needs, alternatives=alts)

    # 无外部能力 → 按本地任务形态分档。理由要反映**真正起作用的那个约束**：
    # 因为写集太大才落到 codex 的，就不该报「语义判据」。
    if size_binding:
        return Assignment(task.id, pick.name, "R04",
                          f"写集 ×{n_files}，只有 {pick.name} 扛得住", alternatives=alts)
    if docs_only and n_files <= 2:
        return Assignment(task.id, pick.name, "R01", f"文档 ×{n_files}", alternatives=alts)
    if not semantic and n_files <= MECHANICAL_MAX_FILES:
        return Assignment(task.id, pick.name, "R02",
                          f"命令判据 ×{count_command_criteria(task)}", alternatives=alts)
    if semantic:
        return Assignment(task.id, pick.name, "R03",
                          f"语义判据 ×{len(task.acceptance) - count_command_criteria(task)}",
                          alternatives=alts)
    return Assignment(task.id, pick.name, "R04", f"写集 ×{n_files}", alternatives=alts)


def route(tasks: Iterable[Task], workers: dict[str, Worker] | None = None,
          ladder: Sequence[str] | None = None,
          require_verified: bool = True) -> list[Assignment]:
    if workers is None or ladder is None:
        w, l = load_workers()
        workers = workers or w
        ladder = ladder or l
    return [route_one(t, workers, ladder, require_verified) for t in tasks]


def alternatives(name: str, needs: Iterable[str], workers: dict[str, Worker],
                 ladder: Sequence[str], require_verified: bool = True) -> tuple[str, ...]:
    """失败恢复：换一个**具备同样能力**的 worker，按成本升序。

    这一条替代了原来的「梯队升档」。方向不一样：
      升档 —— 假设贵的就是更强的，于是把联网任务升给 Codex，越升越错；
      换能力 —— 假设失败可能是这家 harness 的问题，换一家同样能做的。
    Codex 不在联网任务的候选里，这不是降级，这是它本来就没有这个能力。
    """
    pool = capable_workers(workers, ladder, needs, require_verified)
    return tuple(w.name for w in pool if w.name != name)


def next_worker(name: str, ladder: Sequence[str]) -> str | None:
    """旧的梯队升档。保留给不带 needs 的调用方，新代码请用 alternatives()。"""
    try:
        i = list(ladder).index(name)
    except ValueError:
        return ladder[-1] if ladder else None
    return ladder[i + 1] if i + 1 < len(ladder) else None


def suggest_tools(task: Task, worker: Worker) -> tuple[str, ...] | None:
    """能裁工具就裁：给文档任务开 Bash，等于给不需要的人配钥匙。

    返回 None 表示该 harness 不支持裁剪，别假装能管。
    """
    if not worker.tools_selectable:
        return None
    tools = set(_BASE_TOOLS)
    for c in task.needs:
        tools.update(TOOLS_BY_NEED.get(c, ()))
    if not is_docs_only(task) or worker.handles_code:
        tools.add("Bash")
    if is_docs_only(task):
        tools.discard("Bash")
    return tuple(sorted(tools))


def quota_report(assignments: Sequence[Assignment],
                 workers: dict[str, Worker],
                 ladder: Sequence[str]) -> dict[str, Any]:
    """按 codex 为 1.0 的额度权重估算这版计划的开销。

    额度省了多少是次要的指标；`unroutable` 才是硬指标——
    有任务没人能做，这笔账算得再省也是白算。
    """
    n = len(assignments)
    unroutable = [a.task_id for a in assignments if not a.routable]
    if n == 0:
        return {"n": 0, "baseline": 0.0, "planned": 0.0, "saved_pct": 0.0,
                "by_worker": {}, "unroutable": []}

    by_worker: dict[str, int] = {}
    planned = 0.0
    for a in assignments:
        if not a.routable:
            continue
        by_worker[a.worker] = by_worker.get(a.worker, 0) + 1
        w = workers.get(a.worker)
        planned += w.quota if w else _FALLBACK["codex"].quota

    baseline = float(n)  # 全给 codex
    saved = 0.0 if baseline == 0 else (baseline - planned) / baseline
    return {
        "n": n,
        "baseline": round(baseline, 2),
        "planned": round(planned, 2),
        "saved_pct": round(saved * 100, 1),
        "by_worker": by_worker,
        "unroutable": unroutable,
    }


def format_routes(tasks: Sequence[Task],
                  assignments: Sequence[Assignment],
                  workers: dict[str, Worker],
                  ladder: Sequence[str],
                  title: str = "") -> str:
    by_id = {t.id: t for t in tasks}
    out: list[str] = []
    if title:
        out.append(title)
        out.append("")

    head = (f"  {'任务':<8}{'文件':>4}  {'所需能力':<16}{'worker':<10}"
            f"{'份额':>6}  理由")
    out.append(head)
    out.append("  " + "-" * (len(head) - 2))

    for a in assignments:
        t = by_id.get(a.task_id)
        nf = len(t.writes) if t else 0
        need_txt = ",".join(a.needs) if a.needs else "—"
        w = workers.get(a.worker)
        q = f"{w.quota:.2f}" if w else "—"
        name = a.worker or "GAP"
        out.append(f"  {a.task_id:<8}{nf:>4}  {need_txt:<16}{name:<10}{q:>6}  "
                   f"{a.reason}")
        if a.gap:
            out.append(f"           └─ 没有任何 worker 具备 {', '.join(a.gap)}——"
                       f"改计划或先给某个档接线")
        elif a.detail:
            out.append(f"           └─ {a.reason_text}（{a.detail}）")

    rep = quota_report(assignments, workers, ladder)
    out.append("")
    out.append(f"  额度估算（codex = 1.00）：基线 {rep['baseline']:.2f} → "
               f"本计划 {rep['planned']:.2f}　省 {rep['saved_pct']:.1f}%")
    if rep["by_worker"]:
        parts = "　".join(f"{k}×{v}" for k, v in sorted(rep["by_worker"].items()))
        out.append(f"  分布：{parts}")
    if rep["unroutable"]:
        out.append(f"  ⚠ 不可路由：{', '.join(rep['unroutable'])}——"
                   f"这几条现在算不出账，必须先解决能力缺口")
    return "\n".join(out)


def format_matrix(workers: dict[str, Worker], ladder: Sequence[str]) -> str:
    """能力矩阵。选 worker 之前先看这张表，比对价格表有用。"""
    caps = list(CAPABILITIES)
    out: list[str] = []
    head = "  " + f"{'worker':<8}" + "".join(f"{c:>10}" for c in caps) + f"{'额度':>7}"
    out.append(head)
    out.append("  " + "-" * (len(head) - 2))

    mark = {"native": "native", "plugin": "plugin", "none": "—"}
    for name in ladder:
        w = workers.get(name)
        if w is None:
            continue
        row = f"  {name:<8}"
        for c in caps:
            row += f"{mark.get(w.level(c), '—'):>10}"
        row += f"{w.quota:>7.2f}"
        out.append(row)

    plug = [(w.name, c) for w in workers.values() for c in caps if w.caps.get(c) == "plugin"]
    if plug:
        out.append("")
        out.append("  plugin = 需要装插件或配 key，未配好之前按「没有」处理：")
        for name, c in plug:
            done = workers[name].available.get(c, False)
            out.append(f"    {name}.{c:<9} {'已配置' if done else '未配置 → 当前按 none 算'}")
    return "\n".join(out)
