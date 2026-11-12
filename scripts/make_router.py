"""生成路由预实验用的题：r1yes（该编排）与 r1no1（不该编排）。

为什么要单独造这两道：路由的判据是"k 个独立单元、每个单元要读多少"，
而现有题库里没有一对"除判据外其他都相同"的对照。这两道题只改一个维度：

  r1yes  4 个独立 engine 模块，每个约 400 行，互不引用  → k=4，单元大，C1/C2 都满足
  r1no1  1 个文件、一处改动                            → k=1，C1 不满足

单元之间同构（同样三个规则、不同常量表），所以"换的只有规模"。

产物：
  tasks/<id>/meta.json   目标原话 + 验收命令
  tasks/<id>/seed/       初始仓库（含 checks/ 副本，供执行期跑验收）
  tasks/<id>/checks/     权威验收（评分时覆盖仓库内副本，防篡改）
  tasks/<id>/ref/        参考解（只用于自检，不进仓库）

自检（--verify）：seed 必败（0/N）、ref 必过（N/N）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"

GITIGNORE = """__pycache__/
*.pyc
.pytest_cache/
.coverage
"""


def w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 领域参数
DOMAINS = [
    ("orders", "订单",
     {"round": 5, "tiers": ((0, 0), (20000, 150), (50000, 400), (120000, 750), (300000, 1100)),
      "cap": 900000, "weights": (3, 5, 2)}),
    ("billing", "计费",
     {"round": 10, "tiers": ((0, 0), (15000, 200), (45000, 450), (100000, 800), (250000, 1250)),
      "cap": 750000, "weights": (4, 1, 3)}),
    ("refund", "退款",
     {"round": 1, "tiers": ((0, 0), (8000, 100), (30000, 350), (80000, 700), (200000, 1000)),
      "cap": 500000, "weights": (2, 7, 1)}),
    ("ship", "履约",
     {"round": 50, "tiers": ((0, 0), (25000, 250), (60000, 500), (150000, 900), (400000, 1400)),
      "cap": 1200000, "weights": (5, 2, 4)}),
]

RULE_ORDER = [
    "validate_amount", "round_step", "tier_discount", "cap_amount",
    "apply_floor", "apply_ceiling", "normalize_currency", "merge_profile",
    "split_by_weight", "allocate", "audit_trace", "emit_reason",
    "check_monotonic", "check_bounds", "check_overflow", "check_zero",
    "check_negative", "check_tie", "check_scale", "check_idempotent",
    "check_order", "check_cap", "check_tier", "check_round",
    "summarize", "describe", "profile_key", "snapshot", "restore", "reset",
]

RULE_CN = {
    "validate_amount": "入参归一",
    "round_step": "步长取整",
    "tier_discount": "阶梯折扣",
    "cap_amount": "上限截断",
    "apply_floor": "下界保护",
    "apply_ceiling": "上界保护",
    "normalize_currency": "币种归一",
    "merge_profile": "档案合并",
    "split_by_weight": "按权重拆分",
    "allocate": "余数分摊",
    "audit_trace": "审计留痕",
    "emit_reason": "原因输出",
    "check_monotonic": "单调检查",
    "check_bounds": "边界检查",
    "check_overflow": "溢出检查",
    "check_zero": "零值检查",
    "check_negative": "负值检查",
    "check_tie": "中点检查",
    "check_scale": "量纲检查",
    "check_idempotent": "幂等检查",
    "check_order": "顺序检查",
    "check_cap": "上限检查",
    "check_tier": "档位检查",
    "check_round": "取整检查",
    "summarize": "汇总",
    "describe": "描述",
    "profile_key": "档案键",
    "snapshot": "快照",
    "restore": "恢复",
    "reset": "重置",
}

REASON_CODES = {
    "ok": "计算完成，无降级",
    "round": "金额被步长取整",
    "tier": "命中阶梯折扣",
    "cap": "触达上限被截断",
    "zero": "金额为零，跳过后续规则",
    "neg": "金额为负，按零处理",
    "tie": "取整落在中点，按远离零方向",
    "overflow": "中间结果超过安全上限",
    "alloc": "分摊产生余数，按最大余数补齐",
    "audit": "审计轨迹已记录",
    "skip": "规则被跳过",
    "multi": "多条规则同时生效",
    "none": "无匹配规则",
    "partial": "部分权重为零，退化为按剩余权重分摊",
    "empty": "输入为空集合",
    "single": "单元素集合，直接返回",
    "capped": "阶梯后仍超过上限",
    "floored": "应用了下界",
    "ceiled": "应用了上界",
    "profile": "读取了客户档案",
    "restore": "从快照恢复",
    "reset": "状态已重置",
    "trace": "轨迹已落盘",
    "final": "终值已产出",
}


def engine_module(fname: str, label: str, cfg: dict, brief: bool = False) -> str:
    """生成一个约 400 行的独立模块：常量表 + 若干助手 + 3 个待实现规则。

    brief=True 时把三个规则的口径说明压成一句"见 CONVENTIONS.md"，
    用来做"必须读共享约定"的题（单元自身不再自带口径）。
    """
    L: list[str] = []
    a = L.append

    a(f'"""{fname}：{label}计算引擎（独立模块，不依赖其它 engine_*）。')
    a("")
    a("口径：")
    a("  1. 金额一律用「分」的整数，禁止浮点；比例用 bps（万分之一）。")
    a("  2. 规则按 RULE_ORDER 的顺序应用，后一条可以读前一条的产出。")
    a("  3. 本文件可独立修改、独立验收，改它不影响其它 engine_* 模块。")
    a("  4. 常量表由业务给定，不要修改；要改的是文件末尾那三个规则函数。")
    a('"""')
    a("")
    a("from __future__ import annotations")
    a("")
    a("")

    a("RULE_ORDER = (")
    for r in RULE_ORDER:
        a(f'    "{r}",')
    a(")")
    a("")
    a("")

    a(f'STEP = {cfg["round"]}')
    a(f'CAP = {cfg["cap"]}')
    a(f'WEIGHTS = {cfg["weights"]}')
    a("")
    a("TIERS = (")
    a("    # (门槛分, 折扣 bps)：命中「不高于金额」的最高一档")
    for th, bps in cfg["tiers"]:
        a(f"    ({th}, {bps}),")
    a(")")
    a("")
    a("")

    a("AUDIT_FIELDS = (")
    for f in ("input", "after_round", "after_tier", "after_cap", "tier_hit",
              "weight_sum", "shares", "remainder", "reason", "elapsed_us"):
        a(f'    ("{f}", "str"),')
    a(")")
    a("")
    a("")

    a("PROFILE_KEYS = (")
    for i in range(1, 41):
        a(f'    ("p{i:02d}", "profile_{i:02d}", {i * 250}, "auto"),')
    a(")")
    a("")
    a("")

    a("RULE_SPECS = (")
    a("    # (规则名, 中文名, 出错时的降级动作)")
    for i, r in enumerate(RULE_ORDER):
        cn = RULE_CN.get(r, r)
        fallback = "跳过并记 skip" if i % 3 == 0 else (
            "记 warning 继续" if i % 3 == 1 else "按零处理")
        a(f'    ("{r}", "{cn}", "{fallback}"),')
    a(")")
    a("")
    a("")

    a("PRESETS = (")
    a("    # (预设名, 起始金额, 终点金额, 步进)")
    for i in range(1, 21):
        a(f'    ("case_{i:02d}", {i * 1000}, {i * 1000 + 50000}, {i * 5}),')
    a(")")
    a("")
    a("")

    a("SAMPLE_ROWS = (")
    a("    # (样本号, 金额, 权重下标, 备注)")
    for i in range(1, 41):
        a(f'    ({i}, {i * 137}, {i % 3}, "batch-{i % 7}"),')
    a(")")
    a("")
    a("")

    a("EXPORT_COLUMNS = (")
    for name in ("input", "after_round", "after_tier", "after_cap", "tier_hit",
                 "weight_sum", "shares", "remainder", "reason", "elapsed_us"):
        a(f'    "col_{name}",')
    for name in ("profile_code", "profile_name", "profile_threshold",
                 "profile_mode", "preset_name", "sample_no", "batch_tag",
                 "rule_count", "step", "cap", "weight_sum2", "version"):
        a(f'    "col_{name}",')
    a(")")
    a("")
    a("")

    a("def sample_for(index: int) -> tuple[int, int, str]:")
    a('    """按样本号取一行样本；越界时取最后一行。"""')
    a("    for no, amount, weight_idx, tag in SAMPLE_ROWS:")
    a("        if no == index:")
    a("            return amount, weight_idx, tag")
    a("    no, amount, weight_idx, tag = SAMPLE_ROWS[-1]")
    a("    return amount, weight_idx, tag")
    a("")
    a("")
    a("def describe_rule(name: str) -> str:")
    a('    """把规则名翻成人话，仅用于日志；查不到时退回下划线换空格。"""')
    a("    for rule, cn, _fallback in RULE_SPECS:")
    a("        if rule == name:")
    a("            return cn")
    a('    return name.replace("_", " ").strip().title()')
    a("")
    a("")
    a("def validate_amount(amount: int) -> int:")
    a('    """入参归一：负数按 0 处理，None 视为 0。"""')
    a("    if amount is None:")
    a("        return 0")
    a("    if not isinstance(amount, int):")
    a('        raise TypeError(f"amount 必须是 int，收到 {type(amount).__name__}")')
    a("    return amount if amount > 0 else 0")
    a("")
    a("")
    a("def load_profile(key: str) -> dict:")
    a('    """按 key 取档案；未登记的 key 返回空档案。"""')
    a("    for code, name, threshold, mode in PROFILE_KEYS:")
    a("        if code == key or name == key:")
    a('            return {"code": code, "name": name,')
    a('                    "threshold": threshold, "mode": mode}')
    a("    return {}")
    a("")
    a("")
    a("def merge_profile(base: dict, extra: dict) -> dict:")
    a('    """浅合并两份档案，extra 覆盖 base；都不修改入参。"""')
    a("    out = dict(base or {})")
    a("    for k, v in (extra or {}).items():")
    a("        if v is not None:")
    a("            out[k] = v")
    a("    return out")
    a("")
    a("")
    a("def snapshot(state: dict) -> dict:")
    a('    """只保留标量字段，便于落盘。"""')
    a("    return {k: v for k, v in state.items()")
    a("            if isinstance(v, (int, float, str, bool))}")
    a("")
    a("")
    a("def restore(state: dict, snap: dict) -> dict:")
    a('    """从快照恢复，缺失字段保持原值。"""')
    a("    out = dict(state)")
    a("    out.update(snap or {})")
    a("    return out")
    a("")
    a("")
    a("def emit_reason(code: str) -> str:")
    a('    """把 reason 码翻成一句话；未知码原样返回。"""')
    a("    return REASON_CODES.get(code, code)")
    a("")
    a("")
    a("def apply_floor(value: int, floor: int) -> int:")
    a('    """下界保护。"""')
    a("    return value if value >= floor else floor")
    a("")
    a("")
    a("def apply_ceiling(value: int, ceiling: int) -> int:")
    a('    """上界保护。"""')
    a("    return value if value <= ceiling else ceiling")
    a("")
    a("")
    a("def tier_of(amount: int) -> tuple[int, int]:")
    a('    """取命中档位；没有命中返回 (0, 0)。"""')
    a("    hit = (0, 0)")
    a("    for threshold, bps in TIERS:")
    a("        if amount >= threshold and threshold >= hit[0]:")
    a("            hit = (threshold, bps)")
    a("    return hit")
    a("")
    a("")
    a("def check_monotonic(values: list[int]) -> bool:")
    a('    """单调不减检查，仅用于自测。"""')
    a("    return all(b >= a for a, b in zip(values, values[1:]))")
    a("")
    a("")
    a("# ---------------------------------------------------------------- 待实现")
    a("")
    a("def round_step(amount: int) -> int:")
    if brief:
        a('    """取整口径见 CONVENTIONS.md §3。"""')
    else:
        a('    """按 STEP 的整数倍取整：四舍五入，恰好落在两倍数正中间时取远离零的一侧。')
        a("")
        a("    以 STEP=10 为例：12 -> 10，15 -> 20，25 -> 30，-15 -> -20。")
        a('    """')
    a('    raise NotImplementedError("round_step")')
    a("")
    a("")
    a("def tier_discount(amount: int) -> int:")
    if brief:
        a('    """折扣口径见 CONVENTIONS.md §4。"""')
    else:
        a('    """按命中档位打折：amount * (10000 - bps) // 10000，向下取整。')
        a("")
        a("    档位由 TIERS 决定：命中「不高于 amount 的最高门槛」；没有命中则不打折。")
        a('    """')
    a('    raise NotImplementedError("tier_discount")')
    a("")
    a("")
    a("def allocate(total: int, weights=None) -> list[int]:")
    if brief:
        a('    """分摊口径见 CONVENTIONS.md §5。"""')
    else:
        a('    """按权重分摊 total：余数按「小数部分从大到小」逐个补齐，平局取下标小的。')
        a("")
        a("    约束：返回列表之和必须恰好等于 total；weights 为 None 时用 WEIGHTS。")
        a('    """')
    a('    raise NotImplementedError("allocate")')
    a("")
    a("")
    a("def apply_all(amount: int, profile: dict | None = None) -> dict:")
    a('    """按 RULE_ORDER 跑完整条链路，返回终值与审计字段。"""')
    a('    state = {"input": validate_amount(amount), "reason": "ok"}')
    a('    state["after_round"] = round_step(state["input"])')
    a('    state["after_tier"] = tier_discount(state["after_round"])')
    a('    state["after_cap"] = min(state["after_tier"], CAP)')
    a('    if state["after_cap"] != state["after_tier"]:')
    a('        state["reason"] = "capped"')
    a('    state["tier_hit"] = tier_of(state["after_round"])[0]')
    a('    state["profile"] = snapshot(merge_profile(load_profile(""), profile or {}))')
    a("    return state")
    return "\n".join(L) + "\n"


ROUND_IMPL = '''    step = STEP
    if step <= 1:
        return amount
    q, r = divmod(abs(amount), step)
    if r * 2 >= step:
        q += 1
    return (1 if amount >= 0 else -1) * q * step'''

TIER_IMPL = '''    _, bps = tier_of(amount)
    return amount * (10000 - bps) // 10000'''

ALLOC_IMPL = '''    ws = list(weights if weights is not None else WEIGHTS)
    if not ws:
        return []
    s = sum(ws)
    if s <= 0:
        return [0] * len(ws)
    base = [total * x // s for x in ws]
    rest = total - sum(base)
    order = sorted(range(len(ws)), key=lambda i: (-(total * ws[i] % s), i))
    for i in order[:rest]:
        base[i] += 1
    return base'''


def engine_ref(fname: str, label: str, cfg: dict, brief: bool = False) -> str:
    """参考解：把三个 raise 换成实现，其余原样。"""
    src = engine_module(fname, label, cfg, brief=brief)
    src = src.replace('    raise NotImplementedError("round_step")', ROUND_IMPL)
    src = src.replace('    raise NotImplementedError("tier_discount")', TIER_IMPL)
    src = src.replace('    raise NotImplementedError("allocate")', ALLOC_IMPL)
    return src


# ---------------------------------------------------------------- 期望值（独立实现）
def ref_round(amount: int, step: int) -> int:
    if step <= 1:
        return amount
    q, r = divmod(abs(amount), step)
    if r * 2 >= step:
        q += 1
    return (1 if amount >= 0 else -1) * q * step


def ref_tier(amount: int, tiers) -> int:
    hit = (0, 0)
    for threshold, bps in tiers:
        if amount >= threshold and threshold >= hit[0]:
            hit = (threshold, bps)
    return amount * (10000 - hit[1]) // 10000


def ref_alloc(total: int, ws) -> list[int]:
    s = sum(ws)
    base = [total * x // s for x in ws]
    rest = total - sum(base)
    order = sorted(range(len(ws)), key=lambda i: (-(total * ws[i] % s), i))
    for i in order[:rest]:
        base[i] += 1
    return base


def cases_for(cfg: dict) -> dict:
    step = cfg["round"]
    c_round = []
    for base in (0, 1, 3, 12, 13, 137, 250, 999, 2502):
        c_round.append((base, ref_round(base, step)))
        half = base + step // 2
        if half != base:
            c_round.append((half, ref_round(half, step)))
    c_round.append((-37, ref_round(-37, step)))
    c_tier = [(x, ref_tier(x, cfg["tiers"])) for x in
              (0, 1, 7999, 8000, 19999, 20000, 45000, 50000, 120000, 300000, 999999)]
    ws = cfg["weights"]
    c_alloc = [(100, ws, ref_alloc(100, ws)),
               (101, ws, ref_alloc(101, ws)),
               (7, ws, ref_alloc(7, ws)),
               (100, None, ref_alloc(100, ws)),
               (0, ws, ref_alloc(0, ws))]
    return {"round": c_round, "tier": c_tier, "alloc": c_alloc}


def check_script(fname: str, cfg: dict, cases: dict) -> str:
    L = []
    a = L.append
    a(f'"""验收：{fname} 的三个规则函数口径正确。"""')
    a("import sys")
    a("")
    a('sys.path.insert(0, "src")')
    a(f"from {fname} import round_step, tier_discount, allocate  # noqa: E402")
    a("")
    a("bad = 0")
    a("")
    a("")
    a("def eq(actual, expected, what):")
    a("    global bad")
    a("    if actual != expected:")
    a('        print(f"[FAIL] {what}: 得到 {actual!r}，应为 {expected!r}")')
    a("        bad += 1")
    a("")
    a("")
    a("# round_step：四舍五入，中点远离零")
    for inp, exp in cases["round"]:
        a(f'eq(round_step({inp}), {exp}, "round_step({inp})")')
    a("")
    a("# tier_discount：命中档位后按 bps 打折，向下取整")
    for inp, exp in cases["tier"]:
        a(f'eq(tier_discount({inp}), {exp}, "tier_discount({inp})")')
    a("")
    a("# allocate：分摊之和必须恰好等于总额")
    for total, weights, exp in cases["alloc"]:
        wtxt = "None" if weights is None else repr(tuple(weights))
        a(f'eq(allocate({total}, {wtxt}), {exp}, "allocate({total}, {wtxt})")')
    a("")
    a('print("全部合格" if not bad else f"{bad} 处不合格")')
    a("sys.exit(1 if bad else 0)")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 构建
def build_r1yes(root: Path) -> dict:
    tid = "r1yes"
    dest = root / tid
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

    accept = []
    for name, label, cfg in DOMAINS:
        fname = f"engine_{name}"
        w(seed / "src" / f"{fname}.py", engine_module(fname, label, cfg))
        w(ref / "src" / f"{fname}.py", engine_ref(fname, label, cfg))
        body = check_script(fname, cfg, cases_for(cfg))
        w(checks / f"check_{name}.py", body)
        w(seed / "checks" / f"check_{name}.py", body)
        accept.append(f"python checks/check_{name}.py")

    names = "、".join(f"engine_{n}.py" for n, _, _ in DOMAINS)
    goal = (
        f"在 src 下 4 个计算引擎模块（{names}）里补齐三个未实现的规则函数："
        "round_step（按本模块 STEP 的整数倍取整，四舍五入、恰好中点时取远离零的一侧）、"
        "tier_discount（命中本模块 TIERS 里不高于金额的最高档，按 bps 打折后向下取整）、"
        "allocate（按权重分摊总额，余数按小数部分从大到小逐个补齐、平局取下标小的，分摊之和必须恰好等于总额）。"
        "每个模块一个文件、常量表各不相同，互不依赖，可以并行改。"
        "验收：依次跑 python checks/check_orders.py、check_billing.py、check_refund.py、check_ship.py，"
        "全部退出码 0。"
    )
    w(dest / "meta.json", json.dumps({
        "id": tid, "tier": "M", "goal": goal, "timeout_s": 2400,
        "accept": accept, "batch_size": 4, "kind": "独立多模块（该编排）",
    }, ensure_ascii=False, indent=2))
    return {"id": tid, "n": len(accept)}


PRICING_SEED = '''"""pricing：零售价计算（单文件，改动集中在一处）。"""

from __future__ import annotations

BASE_CENTS = 12900
CAP_CENTS = 500000
MEMBER_TIERS = (
    (0, 0),
    (30000, 300),
    (80000, 800),
    (200000, 1300),
)


def load_member(level: str) -> dict:
    """按等级取会员档案；未知等级按普通会员处理。"""
    table = {
        "none": {"level": "none", "months": 0},
        "silver": {"level": "silver", "months": 6},
        "gold": {"level": "gold", "months": 18},
        "platinum": {"level": "platinum", "months": 30},
    }
    return table.get(level or "none", table["none"])


def tier_bps(amount: int) -> int:
    """按金额取折扣 bps：命中不高于 amount 的最高门槛，没有命中返回 0。"""
    if amount <= 0:
        return 0
    bps = 0
    for threshold, rate in MEMBER_TIERS:
        if amount >= threshold:
            bps = rate
    return bps


def final_price(base: int = BASE_CENTS, level: str = "none", qty: int = 1) -> int:
    """计算最终总价（分）。

    规则按顺序：
      1. 总价 = base * qty；qty 小于 1 时按 1 处理；
      2. 会员折扣：按 MEMBER_TIERS 命中不高于总价的最高档，用该档 bps 打折；
      3. 单笔上限 CAP_CENTS，超过就截断；
      4. 结果向下取整到分。
    """
    raise NotImplementedError("final_price")


def format_price(cents: int) -> str:
    """把分格式化成 ￥x.yy。"""
    return f"￥{cents // 100}.{cents % 100:02d}"
'''

PRICING_IMPL = '''    qty = qty if qty and qty > 0 else 1
    total = base * qty
    bps = tier_bps(total)
    total = total * (10000 - bps) // 10000
    if total > CAP_CENTS:
        total = CAP_CENTS
    return int(total)'''

PRICING_REF = PRICING_SEED.replace(
    '    raise NotImplementedError("final_price")', PRICING_IMPL)


def ref_final(base: int, qty: int) -> int:
    qty = qty if qty and qty > 0 else 1
    total = base * qty
    bps = 0
    for threshold, rate in ((0, 0), (30000, 300), (80000, 800), (200000, 1300)):
        if total >= threshold:
            bps = rate
    total = total * (10000 - bps) // 10000
    return min(total, 500000)


def pricing_check() -> str:
    cases = [(12900, 1), (12900, 3), (12900, 0), (12900, -5), (1000, 1),
             (10000, 3), (10000, 8), (100000, 2), (100000, 10)]
    L = ['"""验收：final_price 的四条规则按顺序生效。"""',
         "import sys", "", 'sys.path.insert(0, "src")',
         "from pricing import final_price, format_price  # noqa: E402", "",
         "bad = 0", "", "",
         "def eq(actual, expected, what):",
         "    global bad",
         "    if actual != expected:",
         '        print(f"[FAIL] {what}: 得到 {actual!r}，应为 {expected!r}")',
         "        bad += 1", "", ""]
    for base, qty in cases:
        L.append(f'eq(final_price({base}, "gold", {qty}), {ref_final(base, qty)}, '
                 f'"final_price({base}, qty={qty})")')
    L += ['eq(format_price(12900), "￥129.00", "格式化")', "",
          'print("全部合格" if not bad else f"{bad} 处不合格")',
          "sys.exit(1 if bad else 0)"]
    return "\n".join(L) + "\n"


def build_r1no1(root: Path) -> dict:
    tid = "r1no1"
    dest = root / tid
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    w(seed / "src" / "pricing.py", PRICING_SEED)
    w(ref / "src" / "pricing.py", PRICING_REF)
    body = pricing_check()
    w(checks / "check_pricing.py", body)
    w(seed / "checks" / "check_pricing.py", body)
    goal = (
        "在 src/pricing.py 里实现 final_price：总价 = base * qty（qty 小于 1 按 1）；"
        "按 MEMBER_TIERS 命中不高于总价的最高档，用该档 bps 打折（向下取整）；"
        "结果超过 CAP_CENTS 就截断到 CAP_CENTS。只有这一个文件、一处改动。"
        "验收：跑 python checks/check_pricing.py，退出码 0。"
    )
    accept = ["python checks/check_pricing.py"]
    w(dest / "meta.json", json.dumps({
        "id": tid, "tier": "S", "goal": goal, "timeout_s": 1200,
        "accept": accept, "batch_size": 1, "kind": "单文件小改（不该编排）",
    }, ensure_ascii=False, indent=2))
    return {"id": tid, "n": len(accept)}


# ---------------------------------------------------------------- R-no-short
def engine_module_short(fname: str, label: str, cfg: dict) -> str:
    """r1yes 的压缩版：同样的常量、同样的三个规则，只是没有大表与助手函数。

    与 r1yes 只差"单元规模"这一个变量（372 行 → 约 40 行）。
    """
    L = [
        f'"""{fname}：{label}计算引擎（独立模块，不依赖其它 engine_*）。',
        "",
        "口径：金额用「分」的整数，比例用 bps；常量表由业务给定，不要改。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f'STEP = {cfg["round"]}',
        f'CAP = {cfg["cap"]}',
        f'WEIGHTS = {cfg["weights"]}',
        "TIERS = (",
    ]
    for th, bps in cfg["tiers"]:
        L.append(f"    ({th}, {bps}),")
    L += [
        ")",
        "",
        "",
        "def tier_of(amount: int) -> tuple[int, int]:",
        '    """取命中档位；没有命中返回 (0, 0)。"""',
        "    hit = (0, 0)",
        "    for threshold, bps in TIERS:",
        "        if amount >= threshold and threshold >= hit[0]:",
        "            hit = (threshold, bps)",
        "    return hit",
        "",
        "",
        "def round_step(amount: int) -> int:",
        '    """按 STEP 取整：四舍五入，恰好落在中点时取远离零的一侧。"""',
        '    raise NotImplementedError("round_step")',
        "",
        "",
        "def tier_discount(amount: int) -> int:",
        '    """命中档位后按 bps 打折：amount * (10000 - bps) // 10000，向下取整。"""',
        '    raise NotImplementedError("tier_discount")',
        "",
        "",
        "def allocate(total: int, weights=None) -> list[int]:",
        '    """按权重分摊：余数按小数部分从大到小补齐，平局取下标小的；和必须等于 total。"""',
        '    raise NotImplementedError("allocate")',
    ]
    return "\n".join(L) + "\n"


def build_r2short(root: Path) -> dict:
    tid = "r2short"
    dest = root / tid
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

    accept = []
    for name, label, cfg in DOMAINS:
        fname = f"engine_{name}"
        w(seed / "src" / f"{fname}.py", engine_module_short(fname, label, cfg))
        w(ref / "src" / f"{fname}.py",
          engine_module_short(fname, label, cfg)
          .replace('    raise NotImplementedError("round_step")', ROUND_IMPL)
          .replace('    raise NotImplementedError("tier_discount")', TIER_IMPL)
          .replace('    raise NotImplementedError("allocate")', ALLOC_IMPL))
        body = check_script(fname, cfg, cases_for(cfg))
        w(checks / f"check_{name}.py", body)
        w(seed / "checks" / f"check_{name}.py", body)
        accept.append(f"python checks/check_{name}.py")

    names = "、".join(f"engine_{n}.py" for n, _, _ in DOMAINS)
    goal = (
        f"在 src 下 4 个计算引擎模块（{names}）里补齐三个未实现的规则函数："
        "round_step（按本模块 STEP 取整，四舍五入、恰好中点时取远离零的一侧）、"
        "tier_discount（命中不高于金额的最高档，按 bps 打折后向下取整）、"
        "allocate（按权重分摊总额，余数按小数部分从大到小补齐、平局取下标小的）。"
        "每个模块一个文件、常量各不相同，互不依赖，可以并行改。"
        "验收：依次跑 python checks/check_orders.py、check_billing.py、check_refund.py、check_ship.py，"
        "全部退出码 0。"
    )
    w(dest / "meta.json", json.dumps({
        "id": tid, "tier": "S", "goal": goal, "timeout_s": 1800,
        "accept": accept, "batch_size": 4, "kind": "四个短单元（只改规模）",
    }, ensure_ascii=False, indent=2))
    return {"id": tid, "n": len(accept)}


# ---------------------------------------------------------------- R-no-shared
CONVENTIONS = '''# 计算引擎统一口径（CONVENTIONS）

本文件是 `src/engine_*.py` 四个模块的**唯一口径来源**。
模块里不再重复说明规则，只保留常量表；任何行为上的疑问都以本文件为准。

版本：r1-2026 · 生效日期：2026-11-12 · 维护：结算平台组

---

## §1 适用范围

适用于 `src/engine_orders.py`、`engine_billing.py`、`engine_refund.py`、`engine_ship.py`
四个模块里 `round_step`、`tier_discount`、`allocate` 三个函数。

不适用于：报表导出、对账批处理、历史数据回放（这三处沿用旧口径，见 §8）。

## §2 术语

| 术语 | 含义 |
|---|---|
| 分 | 最小货币单位，整数，禁止浮点 |
| bps | 万分之一，`400` 表示 4% |
| 档位 | `TIERS` 里的一行 `(门槛分, bps)` |
| 命中 | 金额 >= 门槛，且是所有命中行里门槛最高的那个 |
| 余数 | `total - sum(按权重整除的结果)` |

## §3 取整（round_step）

1. 取到 `STEP` 的整数倍，**四舍五入**；
2. 恰好落在两个倍数正中间时，**取远离零的一侧**（12 与 13 互为镜像，不允许银行家舍入）；
3. `STEP <= 1` 时原样返回；
4. 负数按对称规则处理（`-15` 在 `STEP=10` 下取 `-20`）；
5. 不允许返回浮点，不允许用 `round()`（Python 的 `round` 是银行家舍入）。

## §4 阶梯折扣（tier_discount）

1. 命中档位 = 所有 `amount >= 门槛` 的行里门槛最大的那一个；
2. 折扣公式 `amount * (10000 - bps) // 10000`，**向下取整**；
3. 一档都没命中时不打折，原样返回；
4. 折扣只作用于本模块入参，不跨模块累加；
5. 上限截断在 `apply_all` 里做，不在本函数内。

## §5 分摊（allocate）

1. 基础份额 = `total * w // sum(weights)`；
2. 余数逐个补齐，顺序是**小数部分从大到小**（即 `total * w % sum` 从大到小）；
3. 小数部分相同时，**下标小的优先**（这条是硬约定，四个模块必须一致）；
4. 返回列表之和必须**恰好等于** `total`；
5. `weights` 为 `None` 时用模块内的 `WEIGHTS`；权重全为 0 时返回全 0。

## §6 上限（CAP）

1. `CAP` 是单笔上限，超过即截断；
2. 截断只发生在折扣之后；
3. 截断不改变 reason 之外的任何字段。

## §7 禁止事项

1. 禁止使用 `float` 或 `Decimal` 参与计算；
2. 禁止修改常量表（`STEP` / `CAP` / `WEIGHTS` / `TIERS`）；
3. 禁止跨模块 `import`（四个模块必须能独立运行、独立验收）；
4. 禁止把 `CONVENTIONS.md` 的规则复制进代码注释后自行改写；
5. 禁止用"看起来差不多"的实现替代（例如用 `int(x + 0.5)` 代替取整规则）。

## §8 变更记录

| 日期 | 变更 | 影响模块 |
|---|---|---|
| 2026-09-01 | 余数平局从"随机"改为"下标小的优先" | 全部 |
| 2026-09-14 | 取整从银行家舍入改为四舍五入 | 全部 |
| 2026-09-28 | 折扣公式加入 bps 口径 | orders / billing |
| 2026-10-06 | 明确负数对称处理 | refund |
| 2026-10-19 | CAP 从模块常量改为统一常量 | 全部 |
| 2026-10-27 | 禁止跨模块 import | 全部 |
| 2026-11-02 | 报表导出沿用旧口径 | 报表 |
| 2026-11-05 | 对账批处理沿用旧口径 | 对账 |
| 2026-11-08 | 术语统一为"分 / bps / 档位" | 全部 |
| 2026-11-12 | 本版生效 | 全部 |

## 附录 A 条款清单

| 编号 | 条款 | 适用 |
|---|---|---|
| C-01 | 金额一律为整数分 | 全部 |
| C-02 | 比例一律为 bps | 全部 |
| C-03 | 取整四舍五入 | 全部 |
| C-04 | 中点远离零 | 全部 |
| C-05 | 步长小于等于 1 时原样返回 | 全部 |
| C-06 | 不允许银行家舍入 | 全部 |
| C-07 | 负数对称处理 | refund |
| C-08 | 命中门槛取最高 | 全部 |
| C-09 | 折扣向下取整 | 全部 |
| C-10 | 未命中不打折 | 全部 |
| C-11 | 折扣不跨模块累加 | 全部 |
| C-12 | 截断在折扣之后 | 全部 |
| C-13 | 基础份额整除 | 全部 |
| C-14 | 余数按小数部分降序 | 全部 |
| C-15 | 平局取下标小 | 全部 |
| C-16 | 分摊之和等于总额 | 全部 |
| C-17 | 权重为 None 用模块常量 | 全部 |
| C-18 | 权重全零返回全零 | 全部 |
| C-19 | 禁止 float | 全部 |
| C-20 | 禁止改常量表 | 全部 |
| C-21 | 禁止跨模块 import | 全部 |
| C-22 | 禁止复制规则后改写 | 全部 |
| C-23 | 常量表随模块发布 | 全部 |
| C-24 | 验收按模块独立执行 | 全部 |
| C-25 | 全局一致性单独验收 | 全部 |
| C-26 | 报表沿用旧口径 | 报表 |
| C-27 | 对账沿用旧口径 | 对账 |
| C-28 | 回放沿用旧口径 | 回放 |
| C-29 | 变更必须登记 §8 | 全部 |
| C-30 | 口径冲突以本文件为准 | 全部 |
'''

GLOBAL_CHECK = '''"""全局一致性：四个模块必须遵守同一套 CONVENTIONS.md 口径。"""
import sys

sys.path.insert(0, "src")

bad = 0
MODULES = ("orders", "billing", "refund", "ship")


def eq(actual, expected, what):
    global bad
    if actual != expected:
        print(f"[FAIL] {what}: 得到 {actual!r}，应为 {expected!r}")
        bad += 1


for name in MODULES:
    mod = __import__(f"engine_{name}")
    step = mod.STEP
    # C-03 / C-04：整倍数不得被改动（不能过度取整）
    for k in (1, 2, 3, 5, 8):
        eq(mod.round_step(k * step), k * step, f"{name} 整倍数 {k}*STEP 被改动")
    # C-15：小数部分相同时下标小的优先（四个模块必须一致）
    eq(mod.allocate(1, (1, 1, 1)), [1, 0, 0], f"{name} 平局未按小下标优先")
    eq(mod.allocate(2, (1, 1, 1)), [1, 1, 0], f"{name} 平局未按小下标优先(2)")
    # C-16：分摊之和必须等于总额
    eq(sum(mod.allocate(97, mod.WEIGHTS)), 97, f"{name} 分摊和不等于总额")

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
'''


def build_r2shared(root: Path) -> dict:
    tid = "r2shared"
    dest = root / tid
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (seed / "CONVENTIONS.md").write_text(CONVENTIONS, encoding="utf-8")

    accept = []
    for name, label, cfg in DOMAINS:
        fname = f"engine_{name}"
        w(seed / "src" / f"{fname}.py", engine_module(fname, label, cfg, brief=True))
        w(ref / "src" / f"{fname}.py", engine_ref(fname, label, cfg, brief=True))
        body = check_script(fname, cfg, cases_for(cfg))
        w(checks / f"check_{name}.py", body)
        w(seed / "checks" / f"check_{name}.py", body)
        accept.append(f"python checks/check_{name}.py")
    w(checks / "check_global.py", GLOBAL_CHECK)
    w(seed / "checks" / "check_global.py", GLOBAL_CHECK)
    accept.append("python checks/check_global.py")

    names = "、".join(f"engine_{n}.py" for n, _, _ in DOMAINS)
    goal = (
        f"在 src 下 4 个计算引擎模块（{names}）里补齐三个未实现的规则函数"
        "（round_step / tier_discount / allocate）。"
        "**四个模块的取整、折扣、分摊口径统一写在仓库根的 CONVENTIONS.md，模块里不再重复说明**，"
        "动手前必须先读它；四个模块必须遵守同一套口径。"
        "验收：依次跑 python checks/check_orders.py、check_billing.py、check_refund.py、check_ship.py、"
        "check_global.py，全部退出码 0。"
    )
    w(dest / "meta.json", json.dumps({
        "id": tid, "tier": "M", "goal": goal, "timeout_s": 2400,
        "accept": accept, "batch_size": 4, "kind": "四个大单元+共享约定（只改共享性）",
    }, ensure_ascii=False, indent=2))
    return {"id": tid, "n": len(accept)}


def verify(tid: str) -> tuple[int, int]:
    dest = TASKS / tid
    meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
    accept = meta["accept"]

    def run_with(ref: bool) -> int:
        tmp = Path(tempfile.mkdtemp(prefix=f"chk-{tid}-"))
        try:
            shutil.copytree(dest / "seed", tmp / "repo")
            if ref and (dest / "ref").is_dir():
                shutil.copytree(dest / "ref", tmp / "repo", dirs_exist_ok=True)
            ok = 0
            for cmd in accept:
                r = subprocess.run(cmd, cwd=str(tmp / "repo"), shell=True,
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                ok += r.returncode == 0
                if r.returncode != 0:
                    tag = "ref" if ref else "seed"
                    print(f"    [{tag}] 未过 {cmd.split()[-1]}：{(r.stdout or '')[-160:]}")
            return ok
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return run_with(False), run_with(True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="r1yes,r1no1")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    want = [x.strip() for x in args.only.split(",") if x.strip()] or [
        "r1yes", "r1no1", "r2short", "r2shared"]
    builders = {"r1yes": build_r1yes, "r1no1": build_r1no1,
                "r2short": build_r2short, "r2shared": build_r2shared}
    made = []
    for tid in want:
        dest = TASKS / tid
        if dest.exists():
            if not args.force:
                print(f"[skip] {tid} 已存在（--force 覆盖）")
                continue
            shutil.rmtree(dest)
        info = builders[tid](TASKS)
        made.append(info)
        print(f"[ok] {tid}：{info['n']} 个单元，{info['n']} 条验收")

    if args.verify:
        bad = 0
        for info in made:
            tid = info["id"]
            meta = json.loads((TASKS / tid / "meta.json").read_text(encoding="utf-8"))
            n = len(meta["accept"])
            seed_ok, ref_ok = verify(tid)
            good = seed_ok == 0 and ref_ok == n
            print(f"[verify] {tid}：seed 通过 {seed_ok}/{n}，ref 通过 {ref_ok}/{n} "
                  f"→ {'合格' if good else '不合格'}")
            bad += 0 if good else 1
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
