"""engine_orders：订单计算引擎（独立模块，不依赖其它 engine_*）。

口径：
  1. 金额一律用「分」的整数，禁止浮点；比例用 bps（万分之一）。
  2. 规则按 RULE_ORDER 的顺序应用，后一条可以读前一条的产出。
  3. 本文件可独立修改、独立验收，改它不影响其它 engine_* 模块。
  4. 常量表由业务给定，不要修改；要改的是文件末尾那三个规则函数。
"""

from __future__ import annotations


RULE_ORDER = (
    "validate_amount",
    "round_step",
    "tier_discount",
    "cap_amount",
    "apply_floor",
    "apply_ceiling",
    "normalize_currency",
    "merge_profile",
    "split_by_weight",
    "allocate",
    "audit_trace",
    "emit_reason",
    "check_monotonic",
    "check_bounds",
    "check_overflow",
    "check_zero",
    "check_negative",
    "check_tie",
    "check_scale",
    "check_idempotent",
    "check_order",
    "check_cap",
    "check_tier",
    "check_round",
    "summarize",
    "describe",
    "profile_key",
    "snapshot",
    "restore",
    "reset",
)


STEP = 5
CAP = 900000
WEIGHTS = (3, 5, 2)

TIERS = (
    # (门槛分, 折扣 bps)：命中「不高于金额」的最高一档
    (0, 0),
    (20000, 150),
    (50000, 400),
    (120000, 750),
    (300000, 1100),
)


AUDIT_FIELDS = (
    ("input", "str"),
    ("after_round", "str"),
    ("after_tier", "str"),
    ("after_cap", "str"),
    ("tier_hit", "str"),
    ("weight_sum", "str"),
    ("shares", "str"),
    ("remainder", "str"),
    ("reason", "str"),
    ("elapsed_us", "str"),
)


PROFILE_KEYS = (
    ("p01", "profile_01", 250, "auto"),
    ("p02", "profile_02", 500, "auto"),
    ("p03", "profile_03", 750, "auto"),
    ("p04", "profile_04", 1000, "auto"),
    ("p05", "profile_05", 1250, "auto"),
    ("p06", "profile_06", 1500, "auto"),
    ("p07", "profile_07", 1750, "auto"),
    ("p08", "profile_08", 2000, "auto"),
    ("p09", "profile_09", 2250, "auto"),
    ("p10", "profile_10", 2500, "auto"),
    ("p11", "profile_11", 2750, "auto"),
    ("p12", "profile_12", 3000, "auto"),
    ("p13", "profile_13", 3250, "auto"),
    ("p14", "profile_14", 3500, "auto"),
    ("p15", "profile_15", 3750, "auto"),
    ("p16", "profile_16", 4000, "auto"),
    ("p17", "profile_17", 4250, "auto"),
    ("p18", "profile_18", 4500, "auto"),
    ("p19", "profile_19", 4750, "auto"),
    ("p20", "profile_20", 5000, "auto"),
    ("p21", "profile_21", 5250, "auto"),
    ("p22", "profile_22", 5500, "auto"),
    ("p23", "profile_23", 5750, "auto"),
    ("p24", "profile_24", 6000, "auto"),
    ("p25", "profile_25", 6250, "auto"),
    ("p26", "profile_26", 6500, "auto"),
    ("p27", "profile_27", 6750, "auto"),
    ("p28", "profile_28", 7000, "auto"),
    ("p29", "profile_29", 7250, "auto"),
    ("p30", "profile_30", 7500, "auto"),
    ("p31", "profile_31", 7750, "auto"),
    ("p32", "profile_32", 8000, "auto"),
    ("p33", "profile_33", 8250, "auto"),
    ("p34", "profile_34", 8500, "auto"),
    ("p35", "profile_35", 8750, "auto"),
    ("p36", "profile_36", 9000, "auto"),
    ("p37", "profile_37", 9250, "auto"),
    ("p38", "profile_38", 9500, "auto"),
    ("p39", "profile_39", 9750, "auto"),
    ("p40", "profile_40", 10000, "auto"),
)


RULE_SPECS = (
    # (规则名, 中文名, 出错时的降级动作)
    ("validate_amount", "入参归一", "跳过并记 skip"),
    ("round_step", "步长取整", "记 warning 继续"),
    ("tier_discount", "阶梯折扣", "按零处理"),
    ("cap_amount", "上限截断", "跳过并记 skip"),
    ("apply_floor", "下界保护", "记 warning 继续"),
    ("apply_ceiling", "上界保护", "按零处理"),
    ("normalize_currency", "币种归一", "跳过并记 skip"),
    ("merge_profile", "档案合并", "记 warning 继续"),
    ("split_by_weight", "按权重拆分", "按零处理"),
    ("allocate", "余数分摊", "跳过并记 skip"),
    ("audit_trace", "审计留痕", "记 warning 继续"),
    ("emit_reason", "原因输出", "按零处理"),
    ("check_monotonic", "单调检查", "跳过并记 skip"),
    ("check_bounds", "边界检查", "记 warning 继续"),
    ("check_overflow", "溢出检查", "按零处理"),
    ("check_zero", "零值检查", "跳过并记 skip"),
    ("check_negative", "负值检查", "记 warning 继续"),
    ("check_tie", "中点检查", "按零处理"),
    ("check_scale", "量纲检查", "跳过并记 skip"),
    ("check_idempotent", "幂等检查", "记 warning 继续"),
    ("check_order", "顺序检查", "按零处理"),
    ("check_cap", "上限检查", "跳过并记 skip"),
    ("check_tier", "档位检查", "记 warning 继续"),
    ("check_round", "取整检查", "按零处理"),
    ("summarize", "汇总", "跳过并记 skip"),
    ("describe", "描述", "记 warning 继续"),
    ("profile_key", "档案键", "按零处理"),
    ("snapshot", "快照", "跳过并记 skip"),
    ("restore", "恢复", "记 warning 继续"),
    ("reset", "重置", "按零处理"),
)


PRESETS = (
    # (预设名, 起始金额, 终点金额, 步进)
    ("case_01", 1000, 51000, 5),
    ("case_02", 2000, 52000, 10),
    ("case_03", 3000, 53000, 15),
    ("case_04", 4000, 54000, 20),
    ("case_05", 5000, 55000, 25),
    ("case_06", 6000, 56000, 30),
    ("case_07", 7000, 57000, 35),
    ("case_08", 8000, 58000, 40),
    ("case_09", 9000, 59000, 45),
    ("case_10", 10000, 60000, 50),
    ("case_11", 11000, 61000, 55),
    ("case_12", 12000, 62000, 60),
    ("case_13", 13000, 63000, 65),
    ("case_14", 14000, 64000, 70),
    ("case_15", 15000, 65000, 75),
    ("case_16", 16000, 66000, 80),
    ("case_17", 17000, 67000, 85),
    ("case_18", 18000, 68000, 90),
    ("case_19", 19000, 69000, 95),
    ("case_20", 20000, 70000, 100),
)


SAMPLE_ROWS = (
    # (样本号, 金额, 权重下标, 备注)
    (1, 137, 1, "batch-1"),
    (2, 274, 2, "batch-2"),
    (3, 411, 0, "batch-3"),
    (4, 548, 1, "batch-4"),
    (5, 685, 2, "batch-5"),
    (6, 822, 0, "batch-6"),
    (7, 959, 1, "batch-0"),
    (8, 1096, 2, "batch-1"),
    (9, 1233, 0, "batch-2"),
    (10, 1370, 1, "batch-3"),
    (11, 1507, 2, "batch-4"),
    (12, 1644, 0, "batch-5"),
    (13, 1781, 1, "batch-6"),
    (14, 1918, 2, "batch-0"),
    (15, 2055, 0, "batch-1"),
    (16, 2192, 1, "batch-2"),
    (17, 2329, 2, "batch-3"),
    (18, 2466, 0, "batch-4"),
    (19, 2603, 1, "batch-5"),
    (20, 2740, 2, "batch-6"),
    (21, 2877, 0, "batch-0"),
    (22, 3014, 1, "batch-1"),
    (23, 3151, 2, "batch-2"),
    (24, 3288, 0, "batch-3"),
    (25, 3425, 1, "batch-4"),
    (26, 3562, 2, "batch-5"),
    (27, 3699, 0, "batch-6"),
    (28, 3836, 1, "batch-0"),
    (29, 3973, 2, "batch-1"),
    (30, 4110, 0, "batch-2"),
    (31, 4247, 1, "batch-3"),
    (32, 4384, 2, "batch-4"),
    (33, 4521, 0, "batch-5"),
    (34, 4658, 1, "batch-6"),
    (35, 4795, 2, "batch-0"),
    (36, 4932, 0, "batch-1"),
    (37, 5069, 1, "batch-2"),
    (38, 5206, 2, "batch-3"),
    (39, 5343, 0, "batch-4"),
    (40, 5480, 1, "batch-5"),
)


EXPORT_COLUMNS = (
    "col_input",
    "col_after_round",
    "col_after_tier",
    "col_after_cap",
    "col_tier_hit",
    "col_weight_sum",
    "col_shares",
    "col_remainder",
    "col_reason",
    "col_elapsed_us",
    "col_profile_code",
    "col_profile_name",
    "col_profile_threshold",
    "col_profile_mode",
    "col_preset_name",
    "col_sample_no",
    "col_batch_tag",
    "col_rule_count",
    "col_step",
    "col_cap",
    "col_weight_sum2",
    "col_version",
)


def sample_for(index: int) -> tuple[int, int, str]:
    """按样本号取一行样本；越界时取最后一行。"""
    for no, amount, weight_idx, tag in SAMPLE_ROWS:
        if no == index:
            return amount, weight_idx, tag
    no, amount, weight_idx, tag = SAMPLE_ROWS[-1]
    return amount, weight_idx, tag


def describe_rule(name: str) -> str:
    """把规则名翻成人话，仅用于日志；查不到时退回下划线换空格。"""
    for rule, cn, _fallback in RULE_SPECS:
        if rule == name:
            return cn
    return name.replace("_", " ").strip().title()


def validate_amount(amount: int) -> int:
    """入参归一：负数按 0 处理，None 视为 0。"""
    if amount is None:
        return 0
    if not isinstance(amount, int):
        raise TypeError(f"amount 必须是 int，收到 {type(amount).__name__}")
    return amount if amount > 0 else 0


def load_profile(key: str) -> dict:
    """按 key 取档案；未登记的 key 返回空档案。"""
    for code, name, threshold, mode in PROFILE_KEYS:
        if code == key or name == key:
            return {"code": code, "name": name,
                    "threshold": threshold, "mode": mode}
    return {}


def merge_profile(base: dict, extra: dict) -> dict:
    """浅合并两份档案，extra 覆盖 base；都不修改入参。"""
    out = dict(base or {})
    for k, v in (extra or {}).items():
        if v is not None:
            out[k] = v
    return out


def snapshot(state: dict) -> dict:
    """只保留标量字段，便于落盘。"""
    return {k: v for k, v in state.items()
            if isinstance(v, (int, float, str, bool))}


def restore(state: dict, snap: dict) -> dict:
    """从快照恢复，缺失字段保持原值。"""
    out = dict(state)
    out.update(snap or {})
    return out


def emit_reason(code: str) -> str:
    """把 reason 码翻成一句话；未知码原样返回。"""
    return REASON_CODES.get(code, code)


def apply_floor(value: int, floor: int) -> int:
    """下界保护。"""
    return value if value >= floor else floor


def apply_ceiling(value: int, ceiling: int) -> int:
    """上界保护。"""
    return value if value <= ceiling else ceiling


def tier_of(amount: int) -> tuple[int, int]:
    """取命中档位；没有命中返回 (0, 0)。"""
    hit = (0, 0)
    for threshold, bps in TIERS:
        if amount >= threshold and threshold >= hit[0]:
            hit = (threshold, bps)
    return hit


def check_monotonic(values: list[int]) -> bool:
    """单调不减检查，仅用于自测。"""
    return all(b >= a for a, b in zip(values, values[1:]))


# ---------------------------------------------------------------- 待实现

def round_step(amount: int) -> int:
    """按 STEP 的整数倍取整：四舍五入，恰好落在两倍数正中间时取远离零的一侧。

    以 STEP=10 为例：12 -> 10，15 -> 20，25 -> 30，-15 -> -20。
    """
    raise NotImplementedError("round_step")


def tier_discount(amount: int) -> int:
    """按命中档位打折：amount * (10000 - bps) // 10000，向下取整。

    档位由 TIERS 决定：命中「不高于 amount 的最高门槛」；没有命中则不打折。
    """
    raise NotImplementedError("tier_discount")


def allocate(total: int, weights=None) -> list[int]:
    """按权重分摊 total：余数按「小数部分从大到小」逐个补齐，平局取下标小的。

    约束：返回列表之和必须恰好等于 total；weights 为 None 时用 WEIGHTS。
    """
    raise NotImplementedError("allocate")


def apply_all(amount: int, profile: dict | None = None) -> dict:
    """按 RULE_ORDER 跑完整条链路，返回终值与审计字段。"""
    state = {"input": validate_amount(amount), "reason": "ok"}
    state["after_round"] = round_step(state["input"])
    state["after_tier"] = tier_discount(state["after_round"])
    state["after_cap"] = min(state["after_tier"], CAP)
    if state["after_cap"] != state["after_tier"]:
        state["reason"] = "capped"
    state["tier_hit"] = tier_of(state["after_round"])[0]
    state["profile"] = snapshot(merge_profile(load_profile(""), profile or {}))
    return state
