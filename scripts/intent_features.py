#!/usr/bin/env python3
"""
把 runs/ 里的历史跑成一张表：**需求的写法** vs **它被返工了几次**。

为什么做这个：
    校验器能判"这句判据合不合格"，但判不了"这条需求好不好写"。
    后者不是判定问题，是预测问题——预测只要相关性显著就有用。
    而这个仓库里已经躺着一切原材料：intent 原文、plan 被拒的次数、
    每次被拒的错误码、任务成败。缺的只是把它们连起来。

只用标准库，不依赖 jieba：中文按字切就够了。
在这个粒度上分词反而引入噪声——"实现"是一个词还是两个字，
对"这句话含糊不含糊"没有影响。

用法：
    python scripts/intent_features.py
    python scripts/intent_features.py --csv features.csv
    python scripts/intent_features.py --json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from pathlib import Path

# --------------------------------------------------------------------------
# 语言学特征：都是"计数 / 密度"，不做句法分析
# --------------------------------------------------------------------------

REFERENTIAL = ("这", "该", "它", "其", "此", "上述", "以上", "该功能", "这个")
VAGUE = ("约", "基本", "尽量", "明显", "一些", "差不多", "做好", "合适", "适当",
         "大概", "可能", "良好", "完善", "优化一下",
         # 真实用户说"含糊"时用的词，比"良好/完善"常见得多
         "好一点", "好用", "不太", "方便", "简单点", "清楚", "合理", "正确", "顺手")
CONDITIONAL = ("如果", "当", "除非", "若", "只要", "一旦", "在…情况下")
ACCEPT_VERB = ("验证", "检查", "确认", "通过", "跑通", "不得", "确保", "断言",
               "退出码", "状态码", "包含", "等于")
COMPOUND = ("并且", "然后", "同时", "以及", "顺便")
ENUM = re.compile(r"(^|\n)\s*(?:\d+[)）.、]|[一二三四五六七八九十]+[、.]|[-*•])")
ANCHOR = re.compile(
    r"[\w-]+\.(?:py|js|ts|md|json|ya?ml|toml|txt)\b|`[^`]+`|/[\w.*-]+|\d+"
)


def _per100(n: int, total: int) -> float:
    return round(n * 100.0 / total, 1) if total else 0.0


def extract(text: str) -> dict:
    t = text.strip()
    total = len(t)
    sents = [s for s in re.split(r"[。！？；\n]+", t) if s.strip()]
    return {
        "字数": total,
        "句数": len(sents),
        "句均长": round(total / len(sents), 1) if sents else 0.0,
        "指称密度": _per100(sum(t.count(w) for w in REFERENTIAL), total),
        "模糊密度": _per100(sum(t.count(w) for w in VAGUE), total),
        "条件数": sum(t.count(w) for w in CONDITIONAL),
        "验收词": sum(t.count(w) for w in ACCEPT_VERB),
        "并列词": sum(t.count(w) for w in COMPOUND),
        "枚举项": len(ENUM.findall(t)),
        "锚点数": len(ANCHOR.findall(t)),
    }


# --------------------------------------------------------------------------
# 结果特征：从 trace 里读"被返工了几次"
# --------------------------------------------------------------------------

def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _last_block(events: list[dict], start: str, end: set[str]) -> list[dict]:
    """取最后一次"从 start 到 end"的连续片段。

    比按 session 分组更稳：早期事件没有 session 字段，全挤在 1 号里，
    直接统计会把多次重跑累加（一次 3 次重写会显示成 12 次）。
    """
    idx = [i for i, e in enumerate(events) if e.get("event") == start]
    if not idx:
        return []
    block: list[dict] = []
    for e in events[idx[-1]:]:
        block.append(e)
        if e.get("event") in end:
            break
    return block


def plan_stats(events: list[dict]) -> dict:
    evs = _last_block(events, "plan.start", {"plan.accepted", "plan.fallback"})
    if not evs:
        return {"重写": None, "计划": "未跑", "错误码": ""}
    rejected = [e for e in evs if e.get("event") == "plan.rejected"]
    accepted = any(e.get("event") == "plan.accepted" for e in evs)
    codes: list[str] = []
    for e in rejected:
        codes += e.get("data", {}).get("codes", [])
    return {
        "重写": len(rejected),
        "计划": "通过" if accepted else "未通过",
        "错误码": ",".join(sorted(set(codes))),
    }


def run_stats(events: list[dict]) -> dict:
    evs = _last_block(events, "run.start", {"run.end"})
    if not evs:
        return {"任务": None, "成功": None}
    verifies = [e for e in evs if e.get("event") == "task.verify"]
    ok = sum(1 for e in verifies if e.get("data", {}).get("ok"))
    return {"任务": len(verifies), "成功": ok}


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, width: int, right: bool = False) -> str:
    s = str(s)
    gap = " " * max(0, width - _w(s))
    return gap + s if right else s + gap


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return round(num / (dx * dy), 3) if dx and dy else None


def main() -> int:
    ap = argparse.ArgumentParser(description="intent 语言学特征 × 返工次数")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--csv", default=None, help="导出 CSV 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    args = ap.parse_args()

    root = Path(args.runs_dir)
    if not root.exists():
        print(f"没有 {root}")
        return 2

    rows = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        im = d / "intent.md"
        if not im.exists():
            continue
        body = im.read_text(encoding="utf-8")
        body = body.split("---\n", 2)[-1].strip() if body.startswith("---") else body.strip()
        ev = read_events(d / "trace.jsonl")
        rows.append({"run": d.name[:15], **extract(body), **plan_stats(ev), **run_stats(ev)})

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("没有可分析的 run（缺 intent.md）")
        return 1

    cols = ["run", "字数", "句均长", "指称密度", "模糊密度", "条件数", "验收词",
            "枚举项", "锚点数", "重写", "计划", "任务", "成功"]
    widths = {c: max(_w(c), max(_w(r.get(c, "")) for r in rows)) for c in cols}
    print("  ".join(pad(c, widths[c], right=c != "run") for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(pad(r.get(c, ""), widths[c], right=c != "run") for c in cols))

    # 相关性：只对"重写次数"算，样本少时明确标出不显著
    have = [r for r in rows if isinstance(r.get("重写"), int)]
    distinct = len({(r["字数"], r["句均长"], r["锚点数"]) for r in have})
    if distinct < 3:
        print(f"\n只有 {distinct} 种不同的需求文本，相关性无意义。"
              "\n换几种写法（不同长度、不同详细程度）再跑。")
    elif len(have) >= 3:
        print(f"\n与「重写次数」的相关系数（n={len(have)}）")
        ys = [r["重写"] for r in have]
        for feat in ["指称密度", "模糊密度", "条件数", "验收词", "字数", "锚点数", "枚举项"]:
            xs = [r[feat] for r in have]
            if len(set(xs)) == 1:
                continue
            r_ = pearson(xs, ys)
            if r_ is None:
                continue
            note = "  ← 样本太少，只看方向" if len(have) < 10 else ""
            print(f"  {pad(feat, 8)}{r_:+.3f}{note}")
        print("\n  n<10 时相关系数没有统计意义。要下结论，至少 30 条需求。")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wr.writeheader()
            wr.writerows(rows)
        print(f"\n已写入 {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
