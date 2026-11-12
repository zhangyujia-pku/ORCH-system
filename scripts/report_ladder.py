"""规模阶梯三臂汇总：A（Codex 单干）/ B（便宜模型单干）/ D（强制编排）。

按 (题, 臂) 聚合所有 rep，取均值并给极差与 CV。

三个比值各自的含义：
    D/A  继续用 Codex 时，把执行外包出去能省多少   → 回答「省钱」
    B/A  干脆换便宜模型能省多少                   → 上界：不用编排也能到
    D/B  编排比「直接换模型」多花了多少             → 回答「编排值不值」
          > 1 说明编排只是在换模型的路上多收了一道费；
          < 1 才说明拆开交给便宜模型，比便宜模型自己整块做更省。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import unicodedata

ARMS = ("A", "B", "D")


def pad(text, width: int, right: bool = False) -> str:
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))
    fill = " " * max(0, width - w)
    return fill + str(text) if right else str(text) + fill


def counts(r: dict) -> tuple[int, int]:
    detail = (r.get("score") or {}).get("detail") or []
    if isinstance(detail, list):
        return sum(1 for x in detail if isinstance(x, dict) and x.get("rc") == 0), len(detail)
    return 0, 0


def agg(rows: list[dict]) -> dict:
    costs = [r["cost_est"] for r in rows if r.get("cost_est") is not None]
    oks = [counts(r) for r in rows]
    n = max((o[1] for o in oks), default=0)
    mean = statistics.fmean(costs) if costs else None
    return {
        "n": len(rows),
        "cost": mean,
        "min": min(costs) if costs else None,
        "max": max(costs) if costs else None,
        "cv": (statistics.stdev(costs) / mean) if len(costs) > 1 and mean else None,
        "per": (mean / n) if (mean and n) else None,
        "ok": statistics.fmean(o[0] for o in oks) if oks else 0,
        "units": n,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="runs/exp/ladder.jsonl")
    args = ap.parse_args()

    p = pathlib.Path(args.results)
    if not p.exists():
        print(f"（{p} 不存在）")
        return 1
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    cell: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        cell.setdefault((r["task"], r["arm"]), []).append(r)

    tasks = sorted({t for t, _ in cell})
    arms = [a for a in ARMS if any((t, a) in cell for t in tasks)]

    print(f"规模阶梯（每格 {max(len(v) for v in cell.values())} 次，取均值）· 臂 {'/'.join(arms)}")
    col = lambda t, w: pad(t, w, True) + " "          # 列尾留一格，否则右对齐会贴死
    head = col("N", 4) + "".join(col(f"{a} 均值[min-max]", 25) for a in arms)
    head += col("D/A", 7) + col("B/A", 7) + col("D/B", 7)
    head += "".join(col(f"{a}/单元", 10) for a in arms) + col("通过(A/B/D)", 12)
    print(head)
    print("-" * 112)

    pts = []
    for t in tasks:
        S = {a: agg(cell[(t, a)]) for a in arms if (t, a) in cell}
        n = next((v["units"] for v in S.values() if v["units"]), 0)
        if not n:
            continue

        def ratio(x, y):
            if x in S and y in S and S[x]["cost"] and S[y]["cost"]:
                return S[x]["cost"] / S[y]["cost"]
            return None

        def fmt(v):
            return "—" if v is None else f"${v:.4f}"

        line = col(n, 4)
        for a in arms:
            v = S.get(a)
            line += col("—" if not v else f"{fmt(v['cost'])} [{fmt(v['min'])}-{fmt(v['max'])}]", 25)
        for x, y in (("D", "A"), ("B", "A"), ("D", "B")):
            r = ratio(x, y)
            line += col("—" if r is None else f"{r:.2f}x", 7)
        for a in arms:
            v = S.get(a)
            line += col("—" if not v or not v["per"] else f"{v['per']:.5f}", 10)
        line += col("/".join(f"{S[a]['ok']:.0f}" if a in S else "—" for a in ("A", "B", "D")), 12)
        print(line)
        pts.append((n, ratio("D", "A"), ratio("B", "A"), ratio("D", "B"), S))

    print("\n比值随规模（D/A = 外包省钱；D/B = 编排比直接换模型多花多少）")
    for n, da, ba, db, S in pts:
        seg = f"  N={n:<3}"
        seg += f" D/A={da:.2f}x" if da else " D/A=—"
        seg += f"  B/A={ba:.2f}x" if ba else "  B/A=—"
        seg += f"  D/B={db:.2f}x" if db else "  D/B=—"
        print(seg)

    print("\n怎么读：")
    print("  D/B > 1 → 编排是在「换便宜模型」的路上多收一道费；质量若也不更好，就没有存在理由。")
    print("  D/B ≈ 1 → 计划费与它省下的执行费打平。")
    print("  D/B < 1 → 拆开交给便宜模型，比便宜模型整块做还省——这才是编排自己的收益。")
    print("  质量有差时以上比值都不作数：先看通过率，再看钱。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
