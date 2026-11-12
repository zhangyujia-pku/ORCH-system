"""三臂原始数据表：A / B / D，每次运行单独列出，不做任何平均。

为什么不做平均：
    平均会把"哪一次特别贵"抹平。而这里 A 的三次波动可达 2 倍以上，
    均值给出的曲线会显得比实际更平滑、更可信。

D/B 怎么给：
    三次运行彼此独立，配对没有意义，所以给的是**区间**：
    下界 = D 最便宜那次 ÷ B 最贵那次；上界 = D 最贵那次 ÷ B 最便宜那次。
    真实比值一定落在这个区间里，区间窄说明结论稳。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import unicodedata

ARMS = ("A", "B", "D")


def pad(text, width: int, right: bool = False) -> str:
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))
    fill = " " * max(0, width - w)
    return fill + str(text) if right else str(text) + fill


def detail(r: dict) -> tuple[int, int]:
    d = (r.get("score") or {}).get("detail") or []
    if isinstance(d, list):
        return sum(1 for x in d if isinstance(x, dict) and x.get("rc") == 0), len(d)
    return 0, 0



def units_of(task: str, cell: dict) -> int:
    """单元数优先取记录里的验收条数；退路是从题号解析（sc16 → 16）。"""
    for a in ARMS:
        for r in cell.get((task, a), []):
            d = (r.get("score") or {}).get("detail") or []
            if isinstance(d, list) and d:
                return len(d)
    digits = "".join(c for c in task if c.isdigit())
    return int(digits) if digits else 1

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="runs/exp/ladder.jsonl")
    args = ap.parse_args()

    p = pathlib.Path(args.results)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if not r.get("invalid")]

    cell: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        cell.setdefault((r["task"], r["arm"]), []).append(r)
    for k in cell:
        cell[k].sort(key=lambda x: x["rep"])

    tasks = sorted({t for t, _ in cell}, key=lambda t: int(t[2:]))
    reps = 3

    print("每单元成本 · 每次运行单独列出（单位：美元，未平均）")
    head = pad("N", 4, True)
    for a in ARMS:
        for i in range(1, reps + 1):
            head += pad(f"{a}r{i}", 10, True)
    print(head)
    print("-" * len(head))

    for t in tasks:
        n = units_of(t, cell)
        line = pad(n, 4, True)
        for a in ARMS:
            rs = cell.get((t, a), [])
            vals = [r["cost_est"] / n if r.get("cost_est") else None for r in rs]
            for i in range(reps):
                v = vals[i] if i < len(vals) else None
                line += pad("—" if v is None else f"{v:.5f}", 10, True)
        print(line)

    print("\n总成本 · 每次运行单独列出")
    head2 = pad("N", 4, True)
    for a in ARMS:
        for i in range(1, reps + 1):
            head2 += pad(f"{a}r{i}", 10, True)
    print(head2)
    print("-" * len(head2))
    for t in tasks:
        n = units_of(t, cell)
        line = pad(n, 4, True)
        for a in ARMS:
            rs = cell.get((t, a), [])
            for i in range(reps):
                v = rs[i]["cost_est"] if i < len(rs) else None
                line += pad("—" if v is None else f"{v:.4f}", 10, True)
        print(line)

    print("\n通过单元数 · 每次运行单独列出")
    head3 = pad("N", 4, True)
    for a in ARMS:
        for i in range(1, reps + 1):
            head3 += pad(f"{a}r{i}", 10, True)
    print(head3)
    print("-" * len(head3))
    for t in tasks:
        n = units_of(t, cell)
        line = pad(n, 4, True)
        for a in ARMS:
            rs = cell.get((t, a), [])
            for i in range(reps):
                if i < len(rs):
                    ok, tot = detail(rs[i])
                    line += pad(f"{ok}/{tot}", 10, True)
                else:
                    line += pad("—", 10, True)
        print(line)

    print("\nD/B 与 B/A · 用区间表示（三次独立，配对无意义）")
    print(pad("N", 4, True) + pad("D/B 区间", 18, True) + pad("B/A 区间", 18, True)
          + pad("D/A 区间", 18, True))
    print("-" * 62)
    for t in tasks:
        n = units_of(t, cell)
        line = pad(n, 4, True)
        for x, y in (("D", "B"), ("B", "A"), ("D", "A")):
            xs = [r["cost_est"] for r in cell.get((t, x), []) if r.get("cost_est")]
            ys = [r["cost_est"] for r in cell.get((t, y), []) if r.get("cost_est")]
            if not xs or not ys:
                line += pad("—", 18, True)
            else:
                lo, hi = min(xs) / max(ys), max(xs) / min(ys)
                line += pad(f"{lo:.2f}–{hi:.2f}x", 18, True)
        print(line)

    print("\n读法：")
    print("  区间窄 → 结论稳；区间跨过 1.0 → 该档分不出谁便宜。")
    print("  质量列若出现非 N/N，先看质量再看钱。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
