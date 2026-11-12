"""把 results.jsonl 汇总成四张表。只做算术，不做判断。

用法：
    python scripts/exp_report.py                       # 默认 runs/exp/results.jsonl
    python scripts/exp_report.py --by-tier             # 再按 S/M/L 分层
    python scripts/exp_report.py --paired A,D          # 配对四格
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "runs" / "exp" / "results.jsonl"


def load(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def fmt_int(x) -> str:
    return f"{int(x):,}" if isinstance(x, (int, float)) else "—"


def fmt_money(x) -> str:
    return "未配价" if x is None else f"{x:.4f}"


def summarize(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        a = r["arm"]
        d = out.setdefault(a, {"n": 0, "ok": 0, "walls": [], "tin": [], "tout": [],
                               "turns": [], "cost": [], "errors": 0})
        d["n"] += 1
        d["ok"] += 1 if r.get("resolved") else 0
        if r.get("error"):
            d["errors"] += 1
        d["walls"].append(r.get("total_wall_s") or 0)
        u = r.get("usage_all") or {}
        d["tin"].append(u.get("input_tokens", 0))
        d["tout"].append(u.get("output_tokens", 0))
        d["turns"].append(u.get("turns", 0))
        if r.get("cost_est") is not None:
            d["cost"].append(r["cost_est"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(DEFAULT))
    ap.add_argument("--by-tier", action="store_true")
    ap.add_argument("--paired", default="", help="两臂配对，如 A,D")
    args = ap.parse_args(argv)

    rows = load(Path(args.results))
    if not rows:
        print("没有结果。先跑 scripts/exp_runner.py")
        return 1

    s = summarize(rows)
    print(f"结果条数 {len(rows)}　（{Path(args.results).name}）\n")

    print("臂　　n   resolved   中位墙钟   输入token   输出token  轮次   CPS(估算)")
    print("-" * 78)
    for arm in sorted(s):
        d = s[arm]
        med = statistics.median(d["walls"]) if d["walls"] else 0
        tin = sum(d["tin"]) / d["n"] if d["n"] else 0
        tout = sum(d["tout"]) / d["n"] if d["n"] else 0
        turns = sum(d["turns"]) / d["n"] if d["n"] else 0
        rate = d["ok"] / d["n"] if d["n"] else 0
        if d["cost"]:
            total = sum(d["cost"])
            cps = (total / d["ok"]) if d["ok"] else None
            cps_s = "不可定义" if cps is None else fmt_money(cps)
        else:
            cps_s = "未配价"
        print(f"{arm:<4} {d['n']:>2}   {d['ok']:>2}/{d['n']:<2} ({rate:.0%})  "
              f"{med:>7.0f}s  {fmt_int(tin):>10}  {fmt_int(tout):>9}  "
              f"{turns:>4.1f}   {cps_s}")

    if args.by_tier:
        print("\n按规模分层（resolved / n）")
        tiers = sorted({r.get("tier", "?") for r in rows})
        arms = sorted(s)
        print("档位  " + "  ".join(f"{a:>8}" for a in arms))
        for t in tiers:
            cells = []
            for a in arms:
                sub = [r for r in rows if r["arm"] == a and r.get("tier") == t]
                ok = sum(1 for r in sub if r.get("resolved"))
                cells.append(f"{ok}/{len(sub)}" if sub else "—")
            print(f"{t:<5} " + "  ".join(f"{c:>8}" for c in cells))

    if args.paired:
        a, b = [x.strip().upper() for x in args.paired.split(",")][:2]
        key = lambda r: r["task"]
        A = {key(r): r for r in rows if r["arm"] == a}
        B = {key(r): r for r in rows if r["arm"] == b}
        both = sum(1 for k in A if k in B and A[k].get("resolved") and B[k].get("resolved"))
        only_a = sum(1 for k in A if k in B and A[k].get("resolved") and not B[k].get("resolved"))
        only_b = sum(1 for k in A if k in B and not A[k].get("resolved") and B[k].get("resolved"))
        neither = sum(1 for k in A if k in B and not A[k].get("resolved") and not B[k].get("resolved"))
        n = both + only_a + only_b + neither
        print(f"\n配对 {a} vs {b}（共同题 {n}）")
        print(f"  都过 {both}　只 {a} 过 {only_a}　只 {b} 过 {only_b}　都不过 {neither}")
        if n:
            print(f"  差 Δ({b}−{a}) = {(only_b - only_a) / n:+.1%}")
        if only_a + only_b == 0:
            print("  注意：不一致对为 0，区间会退化——不能据此宣称无差异。")

    errs = [r for r in rows if r.get("error")]
    if errs:
        print(f"\n异常 {len(errs)} 条：")
        for r in errs[:8]:
            print(f"  {r['task']} {r['arm']}: {r['error'][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
