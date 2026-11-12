"""成本核算：把一次运行的 token 折算成「额度」和「金额」，并估计相对 A 的节省。

为什么单独一个模块：报告渲染（dispatch）和离线脚本（make_baseline）都要用同一套口径，
口径一旦分叉，报告里的数字就会和实验数据对不上。

三条口径约定（与 docs/实验日志.md E49 一致）：
  1. 额度用 H 侧输入 token 的比值 D/A，越小越省；不是计费单位，是订阅额度的代理。
  2. 金额按 config/prices.json 折算；deepseek 有峰谷差，gpt 没有——所以高峰只惩罚 D。
  3. A 的基线用**中位数 + 区间**，不用均值：A 的 CV 在 40%–60%，均值会被离群点带偏。
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICES = ROOT / "config" / "prices.json"
BASELINE = ROOT / "config" / "baseline.json"
H_MODEL = "gpt-6.1-sol"
L_MODEL = "deepseek-flash"

# 计划阶段固定输入的经验值：历史 11 个格子里 D 的计划输入中位 17.9k–
# 用它当底座，再加计划文件本身的 token，作为「本次 H 侧」的估计。
H_BASE = 17_800

# 计划阶段的输入里，约 76% 是命中缓存的系统提示（历史 11 格实测 13,696/17,969）。
# 不折这一刀，H 侧会被按全价算，D 的成本虚高 3 倍以上——报告会反过来。
H_CACHE_RATIO = 0.76


# ---------------------------------------------------------------- 价格
def load_prices() -> dict:
    try:
        return json.loads(PRICES.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_peak(ts: float | None, windows: list) -> bool:
    t = _dt.datetime.fromtimestamp(ts or time.time(), _dt.timezone.utc)
    if t.weekday() >= 5:
        return False
    return any(int(a) <= t.hour < int(b) for a, b in windows)


def price(usage: dict, model: str, prices: dict | None = None,
          ts: float | None = None) -> float | None:
    """token → 金额。拿不到用量或单价返回 None（不返回 0，避免「没测到」变成「不花钱」）。"""
    if not usage:
        return None
    prices = prices or load_prices()
    spec = (prices.get("models") or {}).get(model)
    if not spec:
        return None
    if "offpeak" in spec:
        rate = spec["peak"] if _is_peak(ts, spec.get("peak_utc_hours") or []) else spec["offpeak"]
    else:
        rate = spec
    pin, pout = rate.get("input_per_mtok"), rate.get("output_per_mtok")
    if pin is None or pout is None:
        return None
    pcache = rate.get("cached_input_per_mtok", pin)
    cached = usage.get("cached_input_tokens", 0) or 0
    fresh = max(0, (usage.get("input_tokens", 0) or 0) - cached)
    return round((fresh * pin + cached * pcache + (usage.get("output_tokens", 0) or 0) * pout) / 1e6, 6)


def is_peak_now(ts: float | None = None) -> bool:
    spec = (load_prices().get("models") or {}).get(L_MODEL) or {}
    return _is_peak(ts, spec.get("peak_utc_hours") or [])


# ---------------------------------------------------------------- 形态签名
def bucket(lines: float) -> str:
    if lines <= 20:
        return "tiny"
    if lines <= 60:
        return "small"
    if lines <= 150:
        return "mid"
    return "big"


def signature(k: int, unit_lines: float, shared: bool) -> str:
    """形态签名：单元数 + 单元规模档 + 是否共享全局约定。

    这三项是实验里唯一被证实会改变 D/A 的变量（E48），所以只用它们查表。
    """
    return f"k{int(k)}|{bucket(unit_lines)}|{'shared' if shared else 'indep'}"


def load_baseline() -> dict:
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except Exception:
        return {"signatures": {}}


def lookup(sig: str, k: int, base: dict | None = None) -> dict | None:
    """三级查表：精确命中 → 同 k 合并 → 没有基线。

    宁可说「无基线」也不外推：真实需求常常不在题库形态里，硬报一个数会骗人。
    """
    base = base if base is not None else load_baseline()
    sigs = base.get("signatures") or {}
    if sig in sigs:
        row = dict(sigs[sig])
        row["confidence"] = "高"
        return row
    same_k = [v for kk, v in sigs.items() if kk.startswith(f"k{int(k)}|")]
    if same_k:
        row = {
            "a_in": _median([r["a_in"] for r in same_k]),
            "a_cached": _median([r["a_cached"] for r in same_k]),
            "a_out": _median([r["a_out"] for r in same_k]),
            "n": sum(r.get("n", 1) for r in same_k),
            "lo": min(r.get("lo", 0) for r in same_k),
            "hi": max(r.get("hi", 0) for r in same_k),
            "confidence": "中（同 k 外推）",
        }
        return row
    return None


def _median(v: list) -> float:
    v = sorted(x for x in v if x is not None)
    if not v:
        return 0.0
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


# ---------------------------------------------------------------- 核算
def h_tokens(plan_path: Path | None) -> int:
    """H 侧 token 的估计：固定底座 + 计划文件本身的长度。

    orch 是子进程，看不到 Codex 会话的用量；这里只做一个可解释的估计，
    报告里必须标「估计」，不能和实测混在一起。
    """
    extra = 0
    if plan_path and Path(plan_path).exists():
        try:
            extra = int(len(Path(plan_path).read_text(encoding="utf-8")) / 2.6)
        except Exception:
            extra = 0
    return H_BASE + extra


def account(worker_usage: dict, h_used: int, sig: str, k: int,
            control: dict | None = None, run_ok: bool = True) -> dict:
    """把一次运行算成：实际花掉的、A 要花多少、省了多少。

    三条铁律（对应交接文档 E02 / R08）：
      1. 执行侧用量没采到 → 成本是**未知**，不是 0；
      2. 本次运行失败 → 不出任何"节省"结论；
      3. 单价或基线缺失 → 保持未知，不拿 0 顶替。
    control 不为空且带 usage 时，A 的数字是本轮实测；对照组失败/取消则作废。
    """
    prices = load_prices()
    win = (worker_usage or {}).get("input_tokens")
    worker_known = isinstance(win, int) and win > 0
    w_cost = price(worker_usage, L_MODEL, prices) if worker_known else None
    h_usage = {"input_tokens": h_used,
               "cached_input_tokens": int(h_used * H_CACHE_RATIO),
               "output_tokens": 300}
    h_cost = price(h_usage, H_MODEL, prices)
    d_cost = (h_cost + w_cost) if (h_cost is not None and w_cost is not None) else None

    control_ok = bool(control and control.get("usage")
                      and not control.get("cancelled")
                      and control.get("exit_code") in (0, None))
    if control_ok:
        a_usage = control["usage"]
        a_tokens = a_usage.get("input_tokens", 0) or 0
        a_cost = price(a_usage, H_MODEL, prices)
        row = {"a_tokens": a_tokens, "a_cost": a_cost, "n": 1,
               "lo": a_cost, "hi": a_cost, "confidence": "实测（本轮对照）"}
    else:
        row = lookup(sig, k) or {}
        a_tokens = row.get("a_in", 0) or 0
        a_cost = price({"input_tokens": a_tokens,
                        "cached_input_tokens": row.get("a_cached", 0) or 0,
                        "output_tokens": row.get("a_out", 0) or 0}, H_MODEL, prices) or 0.0

    unknown: list[str] = []
    if not worker_known:
        unknown.append("执行侧 token 未采集")
    if d_cost is None:
        unknown.append("D 的成本未知")
    if not run_ok:
        unknown.append("本次运行未完成")
    if control and not control_ok:
        unknown.append("对照组未成功完成，A 改用题库基线")
    if not a_tokens:
        unknown.append("缺 A 基线")

    # 只有"跑完了、两侧都有数、基线也在"才允许出节省结论
    can_claim = bool(run_ok and d_cost is not None and a_cost and a_tokens
                     and worker_known)
    save = (a_cost - d_cost) if can_claim else None
    return {
        "h_tokens": h_used, "worker_tokens": win if worker_known else None,
        "h_cost": h_cost, "worker_cost": w_cost, "d_cost": d_cost,
        "a_tokens": a_tokens, "a_cost": a_cost,
        "quota_ratio": (h_used / a_tokens) if (can_claim and a_tokens) else None,
        "save_money": save,
        "save_pct": (save / a_cost * 100) if (can_claim and a_cost) else None,
        "lo": row.get("lo"), "hi": row.get("hi"), "n": row.get("n"),
        "confidence": row.get("confidence") or "低（无基线）",
        "control": control_ok,
        "run_ok": run_ok, "unknown": unknown,
        "baseline": row,
    }


def _fmt_tok(n: int | float | None) -> str:
    if not n:
        return "—"
    return f"{int(n):,}"


def render(acc: dict, control_wall: float | None = None,
           control_note: str = "", enabled: bool = True) -> str:
    """渲染报告末尾的成本块。正负号直给，不美化。"""
    out = ["## 成本核算"]
    a_s = f"${acc['a_cost']:.4f}" if isinstance(acc["a_cost"], (int, float)) else "未知"
    if acc["control"]:
        out.append("  A 对照（实测）   {} token · {}".format(
            _fmt_tok(acc["a_tokens"]), a_s))
        if control_wall:
            out.append(f"  对照耗时         {control_wall:.0f}s")
    else:
        if acc["a_tokens"]:
            out.append("  A 基线（{}，n={}） {} token · {}".format(
                acc["confidence"], acc["n"] or "?", _fmt_tok(acc["a_tokens"]), a_s))
        else:
            out.append("  A 基线           缺失（形态未覆盖，本轮不报节省）")

    cost_s = f"${acc['d_cost']:.4f}" if acc["d_cost"] is not None else "未知"
    out.append("  本次 D           {} token（H 估计） + {} token（执行） · {}".format(
        _fmt_tok(acc["h_tokens"]), _fmt_tok(acc["worker_tokens"]), cost_s))

    if acc["quota_ratio"] is not None and acc["save_money"] is not None:
        q = 1 - acc["quota_ratio"]
        money = acc["save_money"]
        money_s = f"+${money:.4f}（省 {acc['save_pct']:.0f}%）" if money >= 0 \
            else f"−${-money:.4f}（多花 {-acc['save_pct']:.0f}%）"
        out.append("  估计节省         额度 {:.1%}（D/A {:.3f}）· 金额 {}".format(
            q, acc["quota_ratio"], money_s))
    else:
        why = "；".join(acc.get("unknown") or ["缺基线"])
        out.append("  估计节省         **不计算**（" + why + "）。"
                   "以上数字只作记账，不能当成收益。")

    if enabled:
        extra = ""
        if control_wall and isinstance(acc["a_cost"], (int, float)):
            extra = f"（本次 +${acc['a_cost']:.4f}、+{control_wall:.0f}s）"
        out.append("")
        out.append("  ⚠ 当前为试验模式：每次多跑一次对照组 A" + extra +
                   "。真实运行时请让我关闭对照组（`orch control-arm off`）。")
    else:
        out.append("  对照已关闭：A 的数字来自题库基线，不是本轮实测。")

    if control_note:
        out.append("  对照说明         " + control_note)
    return "\n".join(out) + "\n"
