"""轨道 B（批量同构）专用汇总：按单元算成本与通过率。

为什么不用 exp_report：那道脚本以"一次 trial = 一个结果"为单位，
而轨道 B 的一格里含 N 个单元，必须拆到单元级才能回答"每单元多少钱、过了几个"。

用法：
    python scripts/report_b.py                       # 默认 runs/exp/track_b.jsonl
    python scripts/report_b.py --results <文件>
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pad(text: str, width: int, right: bool = False) -> str:
    """按显示宽度补齐。中文占两格，用 len() 对齐会错位。"""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    fill = " " * max(0, width - w)
    return fill + text if right else text + fill


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def units_of(row: dict) -> tuple[int, int]:
    """返回（通过单元数, 单元总数）。

    单元数以验收命令条数为准——比 meta 里的 batch_size 更可信：
    accept 少一条，就等于少验收一个单元。
    """
    detail = (row.get("score") or {}).get("detail")
    if not isinstance(detail, list):
        return 0, 0
    return sum(1 for d in detail if d.get("rc") == 0), len(detail)


def h_tokens(row: dict) -> int:
    """Codex 侧吃掉的输入 token：A 全算，D 只算规划。"""
    if row.get("arm") == "A":
        return (row.get("usage") or {}).get("input_tokens", 0)
    return (row.get("plan_usage") or {}).get("input_tokens", 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "runs" / "exp" / "track_b.jsonl"))
    args = ap.parse_args(argv)

    rows = [r for r in load(Path(args.results)) if r.get("task", "").startswith("b")]
    if not rows:
        print(f"（{args.results} 里没有轨道 B 的记录）")
        return 0

    print("按格看（一格 = 一批 N 个单元）\n")
    print(pad("批", 6) + pad("臂", 4) + pad("通过", 10, True) + pad("单元数", 8, True)
          + pad("总成本", 10, True) + pad("每单元", 10, True)
          + pad("墙钟", 10, True) + pad("H 输入", 13, True))
    for r in sorted(rows, key=lambda x: (x["task"], x["arm"])):
        ok, total = units_of(r)
        cost = r.get("cost_est")
        per = (cost / total) if (cost is not None and total) else None
        print(pad(r["task"], 6) + pad(r["arm"], 4)
              + pad(str(ok), 10, True) + pad(str(total), 8, True)
              + pad("未配价" if cost is None else f"{cost:.4f}", 10, True)
              + pad("—" if per is None else f"{per:.4f}", 10, True)
              + pad(f"{r.get('total_wall_s') or 0:.0f}s", 10, True)
              + pad(f"{h_tokens(r):,}", 13, True))

    print("\n按批对照（D ÷ A）\n")
    print(pad("批", 6) + pad("A 每单元", 12, True) + pad("D 每单元", 12, True)
          + pad("D/A", 9, True) + pad("A 通过率", 11, True)
          + pad("D 通过率", 11, True) + pad("D 的 H 额度比", 15, True))
    by = {(r["task"], r["arm"]): r for r in rows}
    for task in sorted({r["task"] for r in rows}):
        a, d = by.get((task, "A")), by.get((task, "D"))
        if not (a and d):
            continue
        aok, at = units_of(a)
        dok, dt = units_of(d)
        ac, dc = a.get("cost_est"), d.get("cost_est")
        a_per = (ac / at) if (ac and at) else None
        d_per = (dc / dt) if (dc and dt) else None
        ratio = (d_per / a_per) if (a_per and d_per) else None
        hai, dhi = h_tokens(a), h_tokens(d)
        hr = f"{dhi / hai:.0%}" if hai else "—"
        print(pad(task, 6)
              + pad("—" if a_per is None else f"{a_per:.4f}", 12, True)
              + pad("—" if d_per is None else f"{d_per:.4f}", 12, True)
              + pad("—" if ratio is None else f"{ratio:.2f}x", 9, True)
              + pad(f"{(aok / at if at else 0):.0%}", 11, True)
              + pad(f"{(dok / dt if dt else 0):.0%}", 11, True)
              + pad(hr, 15, True))

    print("\n判定线（预注册）：D 每单元成本 < A 的 0.5 且通过率不低 → 批量场景成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
