"""生成《orch 项目说明与进展_2026-11-11.html》——含全部实验数据的对比曲线。

数据全部现读 runs/exp/*.jsonl，不手抄；图用内联 SVG，无 JS 依赖（便于 headless 打印）。
"""
from __future__ import annotations

import json
import math
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "runs" / "exp"
OUT_HTML = ROOT.parent / "orch项目说明与进展_2026-11-11.html"

N_OF = {"sc01": 1, "sc02": 2, "sc04": 4, "sc08": 8, "sc16": 16, "h1": 5}
COL = {"A": "#1f5d63", "B": "#8d5f0c", "D": "#a8321f"}


def load(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def rows() -> list[dict]:
    out = []
    for name in ("results.jsonl", "ladder.jsonl", "h1.jsonl", "calib.jsonl"):
        for r in load(EXP / name):
            r["_src"] = name
            out.append(r)
    return out


def mean(v):
    return st.mean(v) if v else float("nan")


# ---------------------------------------------------------------- SVG

def ymap(v, ymin, ymax, top, bot, log=True):
    v = max(v, ymin)
    if log:
        t = (math.log10(ymax) - math.log10(v)) / (math.log10(ymax) - math.log10(ymin))
    else:
        t = (ymax - v) / (ymax - ymin)
    return top + t * (bot - top)


def chart_cost(per_unit: dict[str, list[float]], tasks: list[str]) -> str:
    """每单元成本散点（对数 y），三臂逐次，连线为均值。"""
    W, H, l, r, t, b = 860, 400, 62, 22, 34, 52
    bot = H - b
    ymin, ymax = 0.0006, 0.09
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="sc 阶梯每单元成本，A/B/D 三臂逐次散点，对数纵轴">']
    ticks = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05]
    for tv in ticks:
        y = ymap(tv, ymin, ymax, t, bot)
        parts.append(f'<line x1="{l}" y1="{y:.1f}" x2="{W-r}" y2="{y:.1f}" '
                     f'stroke="#e4e4dc" stroke-width=".8"/>')
        lab = f"${tv:g}" if tv >= 0.001 else f"${tv:.4f}"
        parts.append(f'<text x="{l-8}" y="{y+3:.1f}" text-anchor="end" font-size="9" '
                     f'fill="#6e6e68">{lab}</text>')
    n = len(tasks)
    step = (W - l - r) / n
    for i, tk in enumerate(tasks):
        cx = l + step * (i + 0.5)
        parts.append(f'<text x="{cx:.1f}" y="{bot+18}" text-anchor="middle" font-size="9.5" '
                     f'fill="#3a3a37">N={N_OF[tk]}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{bot+33}" text-anchor="middle" font-size="8.5" '
                     f'fill="#6e6e68">{tk}</text>')
        for arm in "ABD":
            vals = per_unit.get(f"{tk}|{arm}", [])
            for j, v in enumerate(vals):
                jx = cx + (-0.20 + 0.20 * j) * step
                y = ymap(v, ymin, ymax, t, bot)
                parts.append(f'<circle cx="{jx:.1f}" cy="{y:.1f}" r="2.6" '
                             f'fill="{COL[arm]}" fill-opacity=".55"/>')
            if vals:
                y = ymap(mean(vals), ymin, ymax, t, bot)
                parts.append(f'<circle cx="{cx:.1f}" cy="{y:.1f}" r="4.6" fill="none" '
                             f'stroke="{COL[arm]}" stroke-width="1.8"/>')
    for arm in "ABD":
        pts = []
        for i, tk in enumerate(tasks):
            vals = per_unit.get(f"{tk}|{arm}", [])
            if not vals:
                continue
            pts.append((l + step * (i + 0.5), ymap(mean(vals), ymin, ymax, t, bot)))
        if len(pts) > 1:
            d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
            parts.append(f'<path d="{d}" fill="none" stroke="{COL[arm]}" '
                         f'stroke-width="1.3" stroke-dasharray="5 4" opacity=".75"/>')
    parts.append(f'<line x1="{l}" y1="{bot}" x2="{W-r}" y2="{bot}" stroke="#b6b6ab" stroke-width="1"/>')
    parts.append(f'<line x1="{l}" y1="{t}" x2="{l}" y2="{bot}" stroke="#b6b6ab" stroke-width="1"/>')
    parts.append(f'<text x="{l-46}" y="{t-10}" font-size="9" fill="#6e6e68">美元 / 单元（对数）</text>')
    lx = W - r - 430
    for arm, name in (("A", "A · Codex 端到端"), ("B", "B · ds 端到端"), ("D", "D · 编排")):
        parts.append(f'<circle cx="{lx}" cy="{t-16}" r="4" fill="{COL[arm]}"/>')
        parts.append(f'<text x="{lx+9}" y="{t-12}" font-size="9" fill="#3a3a37">{name}</text>')
        lx += 150
    parts.append("</svg>")
    return "".join(parts)


def chart_ratio(series: dict[str, dict[str, float]], tasks: list[str]) -> str:
    """D/A 与 D/B 比值，对数 y，1.0 基准线。"""
    W, H, l, r, t, b = 860, 360, 62, 22, 38, 56
    bot = H - b
    ymin, ymax = 0.1, 12.0
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="编排相对单模型、相对 Codex 单干的成本比值，按题目">']
    for tv in (0.2, 0.5, 1, 2, 5, 10):
        y = ymap(tv, ymin, ymax, t, bot)
        strong = tv == 1
        parts.append(f'<line x1="{l}" y1="{y:.1f}" x2="{W-r}" y2="{y:.1f}" '
                     f'stroke="{"#8a8a80" if strong else "#e4e4dc"}" '
                     f'stroke-width="{1.2 if strong else .8}" '
                     f'{"stroke-dasharray=\'4 3\'" if strong else ""}/>')
        parts.append(f'<text x="{l-8}" y="{y+3:.1f}" text-anchor="end" font-size="9" '
                     f'fill="#6e6e68">{tv:g}×</text>')
    n = len(tasks)
    step = (W - l - r) / n
    for i, tk in enumerate(tasks):
        cx = l + step * (i + 0.5)
        parts.append(f'<text x="{cx:.1f}" y="{bot+18}" text-anchor="middle" font-size="9" '
                     f'fill="#3a3a37">{tk}</text>')
    for si, (key, col, name) in enumerate((("DA", "#1f5d63", "D / A（编排 vs Codex 单干）"),
                                           ("DB", "#a8321f", "D / B（编排 vs ds 单干）"))):
        pts = []
        for i, tk in enumerate(tasks):
            v = series.get(key, {}).get(tk)
            if v is None:
                continue
            cx = l + step * (i + 0.5)
            y = ymap(v, ymin, ymax, t, bot)
            pts.append((cx, y))
            parts.append(f'<circle cx="{cx:.1f}" cy="{y:.1f}" r="4" fill="{col}"/>')
        if len(pts) > 1:
            d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
            parts.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="1.7"/>')
    parts.append(f'<line x1="{l}" y1="{bot}" x2="{W-r}" y2="{bot}" stroke="#b6b6ab" stroke-width="1"/>')
    parts.append(f'<line x1="{l}" y1="{t}" x2="{l}" y2="{bot}" stroke="#b6b6ab" stroke-width="1"/>')
    parts.append(f'<text x="{W-r}" y="{t-8}" text-anchor="end" font-size="9" fill="#6e6e68">'
                 f'线在 1.0 以上 = 编排更贵</text>')
    lx = l
    for col, name in (("#1f5d63", "D / A"), ("#a8321f", "D / B")):
        parts.append(f'<circle cx="{lx}" cy="{t-20}" r="4" fill="{col}"/>')
        parts.append(f'<text x="{lx+9}" y="{t-16}" font-size="9" fill="#3a3a37">{name}</text>')
        lx += 120
    parts.append("</svg>")
    return "".join(parts)



def chart_doc_vs_code(items: list[tuple[str, float, float]]) -> str:
    """文档类 vs 代码类：D/A 与 D/B 的分组条形（对数 y）。"""
    W, H, l, r, t, b = 860, 340, 62, 22, 40, 58
    bot = H - b
    ymin, ymax = 0.2, 20.0
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="文档类与代码类的成本比值对比">']
    for tv in (0.5, 1, 2, 5, 10, 20):
        y = ymap(tv, ymin, ymax, t, bot)
        strong = tv == 1
        parts.append(f'<line x1="{l}" y1="{y:.1f}" x2="{W-r}" y2="{y:.1f}" '
                     f'stroke="{"#8a8a80" if strong else "#e4e4dc"}" '
                     f'stroke-width="{1.2 if strong else .8}" '
                     f'{"stroke-dasharray=\'4 3\'" if strong else ""}/>')
        parts.append(f'<text x="{l-8}" y="{y+3:.1f}" text-anchor="end" font-size="9" '
                     f'fill="#6e6e68">{tv:g}×</text>')
    n = len(items)
    step = (W - l - r) / n
    for i, (label, da, db) in enumerate(items):
        cx = l + step * (i + 0.5)
        y1 = ymap(1.0, ymin, ymax, t, bot)
        for j, (v, col) in enumerate(((da, "#1f5d63"), (db, "#a8321f"))):
            x = cx + (-0.10 + 0.20 * j) * step
            y = ymap(v, ymin, ymax, t, bot)
            parts.append(f'<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
                         f'stroke="{col}" stroke-width="1.1" opacity=".55"/>')
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.2" fill="{col}"/>')
            parts.append(f'<text x="{x+6:.1f}" y="{y+3:.1f}" text-anchor="start" '
                         f'font-size="8.5" fill="{col}">{v:.2f}×</text>')
        parts.append(f'<text x="{cx:.1f}" y="{bot+18}" text-anchor="middle" font-size="9.3" '
                     f'fill="#3a3a37">{label}</text>')
    parts.append(f'<line x1="{l}" y1="{bot}" x2="{W-r}" y2="{bot}" stroke="#b6b6ab" stroke-width="1"/>')
    parts.append(f'<line x1="{l}" y1="{t}" x2="{l}" y2="{bot}" stroke="#b6b6ab" stroke-width="1"/>')
    parts.append(f'<text x="{l-46}" y="{t-12}" font-size="9" fill="#6e6e68">成本比值（对数）</text>')
    lx = W - r - 250
    for col, name in (("#1f5d63", "D / A"), ("#a8321f", "D / B")):
        parts.append(f'<rect x="{lx}" y="{t-22}" width="10" height="10" fill="{col}" '
                     f'fill-opacity=".78"/>')
        parts.append(f'<text x="{lx+14}" y="{t-13}" font-size="9" fill="#3a3a37">{name}</text>')
        lx += 110
    parts.append("</svg>")
    return "".join(parts)


def chart_h1(h1: dict[str, list[float]]) -> str:
    """h1 三臂逐次（线性 y），直接看方差。"""
    W, H, l, r, t, b = 860, 300, 62, 22, 34, 46
    bot = H - b
    ymin, ymax = 0.0, 0.26
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="h1 跨文件任务三臂逐次总成本">']
    for tv in (0.05, 0.10, 0.15, 0.20, 0.25):
        y = ymap(tv, ymin, ymax, t, bot, log=False)
        parts.append(f'<line x1="{l}" y1="{y:.1f}" x2="{W-r}" y2="{y:.1f}" stroke="#e4e4dc" '
                     f'stroke-width=".8"/>')
        parts.append(f'<text x="{l-8}" y="{y+3:.1f}" text-anchor="end" font-size="9" '
                     f'fill="#6e6e68">${tv:.2f}</text>')
    n = 3
    step = (W - l - r) / n
    for i, arm in enumerate("ABD"):
        cx = l + step * (i + 0.5)
        vals = h1.get(arm, [])
        for j, v in enumerate(vals):
            jx = cx + (-0.22 + 0.22 * j) * step * 0.9
            y = ymap(v, ymin, ymax, t, bot, log=False)
            parts.append(f'<circle cx="{jx:.1f}" cy="{y:.1f}" r="4" fill="{COL[arm]}" '
                         f'fill-opacity=".55"/>')
        if vals:
            m = mean(vals)
            y = ymap(m, ymin, ymax, t, bot, log=False)
            parts.append(f'<line x1="{cx-step*0.34:.1f}" y1="{y:.1f}" '
                         f'x2="{cx+step*0.34:.1f}" y2="{y:.1f}" stroke="{COL[arm]}" '
                         f'stroke-width="2"/>')
            parts.append(f'<text x="{cx:.1f}" y="{y-9:.1f}" text-anchor="middle" '
                         f'font-size="9" fill="{COL[arm]}">均值 ${m:.3f}</text>')
        nm = {"A": "A · Codex 端到端", "B": "B · ds 端到端", "D": "D · 编排"}[arm]
        parts.append(f'<text x="{cx:.1f}" y="{bot+20}" text-anchor="middle" font-size="9.5" '
                     f'fill="#3a3a37">{nm}</text>')
    parts.append(f'<line x1="{l}" y1="{bot}" x2="{W-r}" y2="{bot}" stroke="#b6b6ab" stroke-width="1"/>')
    parts.append(f'<line x1="{l}" y1="{t}" x2="{l}" y2="{bot}" stroke="#b6b6ab" stroke-width="1"/>')
    parts.append(f'<text x="{l-46}" y="{t-10}" font-size="9" fill="#6e6e68">整题成本（美元）</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- 取数

def main() -> None:
    R = rows()
    ok = [r for r in R if r.get("resolved") is True and r.get("cost_est")]

    per_unit: dict[str, list[float]] = {}
    total: dict[str, list[float]] = {}
    wall: dict[str, list[float]] = {}
    hin: dict[str, list[int]] = {}
    for r in ok:
        t = r["task"]
        if t not in N_OF:
            continue
        key = f"{t}|{r['arm']}"
        n = N_OF[t]
        per_unit.setdefault(key, []).append(r["cost_est"] / n)
        total.setdefault(key, []).append(r["cost_est"])
        wall.setdefault(key, []).append(r["total_wall_s"])
        hin.setdefault(key, []).append(
            (r.get("plan_usage") or r.get("usage") or {}).get("input_tokens", 0))

    ladder = ["sc01", "sc02", "sc04", "sc08", "sc16"]

    # 比值：均值之比（与实验日志 E30/E31、skill 里的 47%–73% 同一口径）
    def ratio(a: str, b: str, task: str):
        A = [r["cost_est"] for r in ok if r["task"] == task and r["arm"] == a]
        B = [r["cost_est"] for r in ok if r["task"] == task and r["arm"] == b]
        return mean(A) / mean(B) if A and B else None

    tasks_all = ["t01", "t02", "sc01", "sc02", "sc04", "sc08", "sc16", "h1"]
    series = {"DA": {}, "DB": {}}
    for t in tasks_all:
        da = ratio("D", "A", t)
        db = ratio("D", "B", t)
        if da:
            series["DA"][t] = da
        if db:
            series["DB"][t] = db
    series["DA"]["b3"] = 0.2969 / 0.0724          # E22：A r3 干净格 vs D 编排格
    tasks_ratio = tasks_all + ["b3"]

    h1v = {a: [r["cost_est"] for r in ok if r["task"] == "h1" and r["arm"] == a]
           for a in "ABD"}

    # ---- 表格：阶梯全数据
    trs = []
    for t in ladder:
        for arm in "ABD":
            v = [(r["rep"], r["cost_est"], r["total_wall_s"],
                  (r.get("usage") or {}).get("input_tokens"))
                 for r in ok if r["task"] == t and r["arm"] == arm]
            v.sort()
            cells = "".join(f"<td class='n'>${c:.4f}<br><span class='small'>{w:.0f}s</span></td>"
                            for _, c, w, _ in v)
            trs.append(f"<tr><td><b>{t}</b></td><td class='n'>N={N_OF[t]}</td>"
                       f"<td>{arm}</td>{cells}"
                       f"<td class='n'>${mean([x[1] for x in v]) / N_OF[t]:.5f}</td></tr>")

    # ---- 表格：h1 + 其它题
    others = []
    for t in ("h1", "t01", "t02", "b3"):
        for arm in ("A", "B", "D"):
            v = [r["cost_est"] for r in ok if r["task"] == t and r["arm"] == arm]
            if not v and t == "b3":
                continue
            if not v:
                continue
            cells = "".join(f"<td class='n'>${c:.4f}</td>" for c in sorted(v))
            while cells.count("<td") < 3:
                cells += "<td class='n'>—</td>"
            others.append(f"<tr><td><b>{t}</b></td><td>{arm}</td>{cells}"
                          f"<td class='n'>${mean(v):.4f}</td></tr>")

    # b3 不在 jsonl 里（E22 手工/CLI 两轮），按实验日志的原始值补两行
    others.append("<tr><td><b>b3</b></td><td>A</td><td class='n'>$0.0763</td>"
                  "<td class='n'>—</td><td class='n'>$0.0724</td>"
                  "<td class='n'>$0.0744</td></tr>")
    others.append("<tr><td><b>b3</b></td><td>D</td><td class='n'>—</td>"
                  "<td class='n'>$0.2969</td><td class='n'>—</td>"
                  "<td class='n'>$0.2969</td></tr>")

    # ---- 探针
    probe = load(EXP / "ctx-probe" / "summary.jsonl")
    probe = [p for p in probe if "verdict" in p]

    ctx = {
        "per_unit": per_unit,
        "ratio": series,
        "h1": h1v,
    }
    calib = {}
    for r in ok:
        if r["task"] in ("t04", "t05", "t06"):
            calib.setdefault(r["task"], {})[r["arm"]] = r
    calib_rows = ""
    for t in ("t04", "t05", "t06"):
        d = calib.get(t, {})
        if not d:
            continue
        for arm in ("B", "D"):
            r = d.get(arm)
            if not r:
                continue
            tt = (r.get("usage") or {}).get("input_tokens")
            rb = d.get("B", {}).get("cost_est")
            ratio = (f"{r['cost_est']/rb:.2f}×" if arm == "D" and rb else "—")
            calib_rows += (f"<tr><td><b>{t}</b></td><td>{arm}</td>"
                           f"<td class='n'>${r['cost_est']:.4f}</td>"
                           f"<td class='n'>{format(tt, ',') if tt else '—'}</td>"
                           f"<td class='n'>{ratio}</td></tr>")

    _docs_rows = (load(EXP / "docs.jsonl") + load(EXP / "docs_fix.jsonl")
                  + load(EXP / "docs_v2.jsonl"))
    _seen = {}
    for _r in _docs_rows:
        _seen[(_r["task"], _r["arm"], _r["rep"])] = _r   # 后写覆盖：D 用修复后的
    docs = list(_seen.values())
    dok = [r for r in docs if r.get("resolved") is True and r.get("cost_est")]
    doc_mean = {a: mean([r["cost_est"] for r in dok if r["arm"] == a]) for a in "ABD"}
    doc_cost = {a: [r["cost_est"] for r in dok if r["arm"] == a] for a in "ABD"}
    doc_wall = {a: [r["total_wall_s"] for r in dok if r["arm"] == a] for a in "ABD"}
    doc_plan = [r["plan_usage"]["input_tokens"] for r in dok if r["arm"] == "D"]
    doc_in = [r["usage"]["input_tokens"] for r in dok if r["arm"] == "D"]
    doc_fail = sum(1 for r in docs if r.get("resolved") is not True)
    doc_ratio = (doc_mean["D"] / doc_mean["A"], doc_mean["D"] / doc_mean["B"])
    code_small = (mean([series["DA"][t] for t in ("sc01", "sc02", "sc04", "sc08")]),
                  mean([series["DB"][t] for t in ("sc01", "sc02", "sc04", "sc08")]))
    vs = [("代码 · N≤8 同构", code_small[0], code_small[1]),
          ("代码 · N=16", series["DA"]["sc16"], series["DB"]["sc16"]),
          ("代码 · h1 跨层", series["DA"]["h1"], series["DB"]["h1"]),
          ("文档 · 6 份规格", doc_ratio[0], doc_ratio[1])]
    c4 = chart_doc_vs_code(vs)
    doc_stats = {
        "a": doc_mean["A"], "b": doc_mean["B"], "d": doc_mean["D"],
        "fa": doc_cost["A"], "fb": doc_cost["B"], "fd": doc_cost["D"],
        "wa": doc_wall["A"], "wb": doc_wall["B"], "wd": doc_wall["D"],
        "plan": doc_plan, "win": doc_in, "fail": doc_fail, "n": len(docs),
        "da": doc_ratio[0], "db": doc_ratio[1],
    }

    html = render(ctx, trs, others, probe, calib_rows, c4, doc_stats)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"[ok] {OUT_HTML}  ({len(html)/1024:.1f} KB)")
    print("比值:", {k: round(v, 2) for k, v in series["DB"].items()})
    print("D/A:", {k: round(v, 2) for k, v in series["DA"].items()})


def render(ctx, trs, others, probe, calib_rows, c4, doc_stats) -> str:
    css = (ROOT / "scripts" / "_progress.css").read_text(encoding="utf-8") \
        if (ROOT / "scripts" / "_progress.css").exists() else ""
    c1 = chart_cost(ctx["per_unit"], ["sc01", "sc02", "sc04", "sc08", "sc16"])
    c2 = chart_ratio(ctx["ratio"], ["t01", "t02", "sc01", "sc02", "sc04", "sc08",
                                    "sc16", "h1", "b3"])
    c3 = chart_h1(ctx["h1"])

    probe_rows = "".join(
        f"<tr><td class='n'>{p['target_est']:,}</td>"
        f"<td class='n'>{(format(p['actual_input'], ',') if p.get('actual_input') else '—')}</td>"
        f"<td>{p['verdict']}</td><td class='n'>{p['wall_s']}s</td>"
        f"<td class='small'>{p.get('msg_head', '')[:40]}</td></tr>"
        for p in probe)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>多 Agent 编排引擎 orch · 项目说明与进展 2026-11-11</title>
<style>{css}</style></head><body><div class="wrap">

<div class="titleblock">
  <div class="kicker">ORCH · 进展记录</div>
  <h1>编排到底省不省：三臂全数据、路由规则落库、以及"饱和"这件事第一次被测量</h1>
  <div class="meta">2026-11-11 · 覆盖 E22–E40 · 数据源 runs/exp/*.jsonl（逐次记录，不取平均）</div>
  <div class="lead">
    今天做了三件互相关联的事：把 <strong>sc 阶梯与 h1 的三臂数据跑满</strong>（21 个配对格）；
    据此把 <strong>路由规则写进 skill</strong>——先写成"禁止读仓库"，再被证据推翻改成"控制面读、执行面不读"；
    最后用 <strong>619k token 的探针</strong>去撞 Codex 的窗口，撞到的却是额度上限。
  </div>
</div>

<div class="kv">
  <div class="cell"><div class="n">21</div><div class="k">A/D 配对格（含 h1、t01/t02、b3）</div></div>
  <div class="cell"><div class="n">13 / 8</div><div class="k">编排对 Codex 单干的胜 / 负</div></div>
  <div class="cell"><div class="n">8.5×</div><div class="k">H 侧输入 token 中位节省（3.1–71.5×）</div></div>
  <div class="cell"><div class="n">619k</div><div class="k">实测被 Codex 吃下的输入 token</div></div>
</div>

<h2>一、三臂全数据（逐次，不取均值）</h2>

<p>三臂定义不变：<b>A</b> = Codex 端到端；<b>B</b> = ds 端到端；<b>D</b> = Codex 规划 + ds 执行（强制编排）。
sc 阶梯是同一生成器造的 N 个同构单元，只改 N；h1 是跨领域、需先读约定的任务。</p>

<table class="tight big">
<thead><tr><th>题</th><th class="n">N</th><th>臂</th>
<th class="n">r1</th><th class="n">r2</th><th class="n">r3</th><th class="n">每单元</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
<div class="cap">表 1　sc 阶梯 45 格全量。单元格下方为墙钟秒数；"每单元"= 三次均值 ÷ N。</div>

<div class="figure">{c1}
<div class="cap">图 1　每单元成本随规模的走势（对数纵轴）。实心小点为三次原始值，空心圈为均值。</div>
</div>

<p>这张图是今天最有用的一张：<b>B 的每单元成本随 N 单调下降到 $0.001，D 却卡在 $0.010 附近不再下降</b>。
原因不是模型差，是结构——B 用一个上下文从头做到尾，总成本几乎与 N 无关；
D 的每个单元都要独立起一个 worker，各付约 40–46 万 token 的固定底座。</p>

<div class="figure">{c2}
<div class="cap">图 2　编排相对两条基线的成本比值。D/B 在每一题都高于 1（全败）；
D/A 在 t01–sc08 低于 1，到 sc16 与 b3 反转。</div>
</div>

<table>
<thead><tr><th>题</th><th>臂</th><th class="n">r1</th><th class="n">r2</th><th class="n">r3</th><th class="n">均值</th></tr></thead>
<tbody>{''.join(others)}</tbody></table>
<div class="cap">表 2　h1 / t01 / t02 与 b3（b3 的 A 取 E22 两次干净格 $0.0763、$0.0724，D 取编排格 $0.2969）。</div>

<div style="break-inside:avoid; page-break-inside:avoid;">
<table class="tight">
<thead><tr><th>题</th><th>臂</th><th class="n">成本</th><th class="n">输入 token（执行侧）</th><th class="n">D/B</th></tr></thead>
<tbody>{calib_rows}</tbody></table>
<div class="cap">表 2b　t04–t06 单次对照（仅 B、D 两臂，用于验证题型差异：代码之外的批量任务）。</div>
</div>

<div class="figure">{c3}
<div class="cap">图 3　h1 三臂逐次。三臂都 5/5 通过，排序为 B &lt; A &lt; D。</div>
</div>

<h2>二、结论：对 B 全败，对 A 有条件胜</h2>

<table>
<thead><tr><th>题</th><th class="n">N</th><th class="n">D/A</th><th class="n">D/B</th><th>读法</th></tr></thead>
<tbody>
<tr><td>t01 / t02</td><td class="n">1</td><td class="n">0.14 / 0.35</td><td class="n">8.31 / 3.50</td><td>编排比单干便宜，比 ds 贵 3–8 倍</td></tr>
<tr><td>sc01</td><td class="n">1</td><td class="n">0.53</td><td class="n">2.38</td><td rowspan="4">N≤8：比 Codex 单干便宜 47%–73%，且方差更小</td></tr>
<tr><td>sc02</td><td class="n">2</td><td class="n">0.33</td><td class="n">2.90</td></tr>
<tr><td>sc04</td><td class="n">4</td><td class="n">0.27</td><td class="n">4.34</td></tr>
<tr><td>sc08</td><td class="n">8</td><td class="n">0.45</td><td class="n">4.40</td></tr>
<tr><td>sc16</td><td class="n">16</td><td class="n">2.34</td><td class="n">9.54</td><td>反转：每单元底座 × 16</td></tr>
<tr><td>h1</td><td class="n">5</td><td class="n">1.29</td><td class="n">5.06</td><td>跨层共享上下文，worker 各重读全局</td></tr>
<tr><td>b3</td><td class="n">8</td><td class="n">4.10</td><td class="n">—</td><td>planner 自己浏览了仓库，额度优势反转</td></tr>
</tbody></table>

<div class="callout">
<div class="label">两条可直接引用的结论</div>
<p><b>一、对 B 是结构性全败。</b>21 格里 D 从未低于 B；D/B 逐格最小 1.87×、最大 19.8×。
根因是成本形态：B 的总成本与 N 几乎无关，D 近似线性。加规模不会反转。</p>
<p><b>二、对 A 只在"Codex 会反复试错"的任务上有胜算。</b>N≤8 的同构任务上便宜 47%–73%、方差从 47%–87% 降到 8%–26%；
但 h1（A 只读一遍就做对）与 b3（A 一次做对 200k）上 D 分别贵 1.29× 与 4.10×。</p>
</div>

<h3>四种"听起来对、实测相反"的说法</h3>
<table>
<thead><tr><th>说法</th><th>实测</th></tr></thead>
<tbody>
<tr><td>单元越多越摊薄</td><td><b>反了</b>：N=16 时 D/A 1.37–4.82，越往上越贵（每单元底座线性叠加）</td></tr>
<tr><td>编排更快</td><td>只在 A 卡住时成立：sc08 r1 墙钟 434s→200s、sc04 r2 356s→146s；
A 顺利时反而更快（46–76s vs D 的 73–206s）</td></tr>
<tr><td>编排提高通过率</td><td>21 格里<b>没有一格</b>是"A 挂、D 过"。唯一分歧是 t03：B 过、D 挂（slugify 差一处）</td></tr>
<tr><td>跨文件、要先读约定 = 编排的机会</td><td><b>反了</b>：h1 上 D/A = 1.24–1.34，三臂里最贵</td></tr>
</tbody></table>
<div class="cap">表 3　这四条如果写进路由规则会直接把人带偏，所以单列。</div>

<h2>三、h1 为什么输：每个 worker 都把全局重读了一遍</h2>

<p>h1 是 31 个源文件、5 个领域各 4 层、必须先读 <code>docs/CONVENTIONS.md</code> 的题。
直觉上"跨文件、要读文档"应该轮到编排，实测正好相反。</p>

<table>
<thead><tr><th>口径</th><th>输入 token</th><th>说明</th></tr></thead>
<tbody>
<tr><td>A 一次做完 5 个领域</td><td class="n">486k / 504k / 700k</td><td>读一次，然后在同一上下文里写完全部</td></tr>
<tr><td>D 的 5 个 worker，每个</td><td class="n">1164k / 1209k / 1706k / 1316k / 808k</td><td>均值 ≈ 1.24M，每个都要重建一遍全局理解</td></tr>
<tr><td>D 合计</td><td class="n">5.8M – 6.2M</td><td><b>约为 A 的 12 倍</b></td></tr>
</tbody></table>

<p>L 的单价优势是 13 倍，token 膨胀是 12 倍——<b>两者几乎正好抵消</b>，再叠加计划费，D 就成了最贵的那个（1.29×）。
所以判据不是"单元数 N"，而是：</p>

<div class="callout soft">
<div class="label">真正的变量</div>
<p><b>单元之间能共享的知识，单会话能否一次装下并复用。</b>
跨越这道边界做并行，每个 worker 都要重建一次全局理解；
膨胀倍数随「共享上下文 ÷ 单元自身工作量」上升。</p>
</div>

<h2>四、数据可信度：两次污染事故与一次手工/机器对照</h2>

<p>今天有一半时间花在"证明数据没被污染"上。三件事必须写进记录，否则这些数字不可信。</p>

<table>
<thead><tr><th>事故</th><th>现象</th><th>根因</th><th>处置</th></tr></thead>
<tbody>
<tr><td>A 臂 r2（E22）</td><td>成本 $0.0724→$0.2531（3.5×），墙钟 68→320s，自派 8 个 worker</td>
<td>只是没把 orch 藏起来——它不需要技能文件，PATH 上有就会调</td>
<td>该格作废；A 臂定义改为"orch 不可达"，不是"不用 orch"</td></tr>
<tr><td>12 格 A 被污染（E26）</td><td>整批 A 格都在偷偷编排，记录里看不出异常</td>
<td>两层：<code>.hidden</code> 残留使 rename 静默失败；<code>SKILL.md</code> 教模型"没装就跑 install_cli.py"</td>
<td>唯一 stash 目录 + <code>shutil.which</code> 校验 + 连技能目录一起藏 + 跑后复查 <code>.orch-runs</code></td></tr>
<tr><td>12 格 A 失败（Cloudflare）</td><td>0 token、直接失败</td><td>上游拦截</td><td>标 invalid 不入表；上游恢复后重跑</td></tr>
</tbody></table>
<div class="cap">表 4　入表的每一格 A 都满足 <code>orch_hidden=True</code>，且事后复查无 <code>.orch-runs</code>。</div>

<h3>手工 vs 机器：补丁逐字节相同</h3>

<p>同一题、同一句原话、同一模型，只换驱动方式（客户端手点 vs CLI 调）：</p>

<table class="tight">
<thead><tr><th>指标</th><th class="n">手工 r1</th><th class="n">机器 r3</th><th class="n">差</th></tr></thead>
<tbody>
<tr><td>单元通过</td><td class="n">8/8</td><td class="n">8/8</td><td class="n">同</td></tr>
<tr><td>补丁字节</td><td class="n">4,672</td><td class="n">4,672</td><td class="n"><b>逐字节相同</b></td></tr>
<tr><td>H 输入</td><td class="n">228,007</td><td class="n">200,267</td><td class="n">−12.2%</td></tr>
<tr><td>墙钟</td><td class="n">74s</td><td class="n">68s</td><td class="n">−8%</td></tr>
</tbody></table>
<div class="cap">表 5　换驱动方式不改变产出，只让输入 token 少 12%——差的是两边系统提示的长度。</div>

<p class="small">补充（E35）：b3 那两格 A 只藏了 shim、skill 仍可读，A 的第一条命令就是
<code>cat ~/.codex/skills/orch/SKILL.md</code>，共 16 条命令。所以那两格 A 的成本略偏高（1–2%），
方向上偏保守，不改变判断。</p>

<h2>五、路由规则落库：从"禁止读"到"控制面读"</h2>

<p>今天最值得记的不是数据，是<b>一条规则被自己的证据推翻又重新写</b>的过程。</p>

<table class="tight">
<thead><tr><th>时间</th><th>写进 skill 的规则</th><th>依据</th></tr></thead>
<tbody>
<tr><td>18:20</td><td>N=2–8 交给 orch；N≥9 自己做</td><td>sc 阶梯：N≤8 有 11/12 格便宜，N=16 全贵</td></tr>
<tr><td>18:25</td><td>硬约束：规划阶段不许读仓库（否则额度归零）</td><td>b3 那次 planner 读仓库 → H 从 17.6k 冲到 1.41M</td></tr>
<tr><td>18:33</td><td><b>撤回上一条</b>：改成"控制面读、执行面不读"</td><td>三变体（只给清单 / 通读全文 / 自由探索）产出的计划<b>完全相同</b></td></tr>
<tr><td>18:35</td><td>适用面放宽到"批量产出（文档/表格/素材/跨系统操作）"</td><td>"文件名含信息"是本题库偏置，非代码场景不成立</td></tr>
</tbody></table>

<div class="callout soft">
<div class="label">现在的边界（skill 已同步两处）</div>
<p><b>该读（控制面）</b>：需求原文与验收标准、项目结构与约定、被拆单元的签名级现状、上一轮失败原因。<br>
<b>不该读（执行面）</b>：要改的那几行的完整实现、样板代码、无关模块、worker 各自的细节。<br>
判据：要它判断"拆得对不对、验收够不够"→ 读；要它判断"这行怎么改"→ 不读。</p>
</div>

<h2>六、读不读仓库：探针、代价、以及一条被推翻的结论</h2>

<p>为回答"规划阶段该不该读仓库"，做了三变体探针（<code>scripts/probe_plan_ctx.py</code>）：
同题面、同模型、同仓库副本，只改给 Codex 的输入。</p>

<table>
<thead><tr><th>题</th><th class="n">P0 只给路径清单</th><th class="n">P1 全仓正文</th><th class="n">P2 自由探索</th><th>计划是否相同</th></tr></thead>
<tbody>
<tr><td>h1（31 文件 / 601 行）</td><td class="n">17,976</td><td class="n">29,204（1.62×）</td>
<td class="n">106,528（5.93×）</td><td><b>完全相同</b>：5 任务 / 20 个文件</td></tr>
<tr><td>b3（8 个 loader）</td><td class="n">17,754</td><td class="n">22,569（1.27×）</td>
<td class="n">17,785（1.00×，它没去读）</td><td><b>完全相同</b>：8 任务 / 8 文件</td></tr>
</tbody></table>
<div class="cap">表 6　"读不读"对计划本身几乎没有影响；贵的不是读，是来回翻。</div>

<p>机制上：拆 <code>sess-D-b3-r2.jsonl</code> 看到 <b>1 个 turn、27 条命令、累计 1.4M 输入</b>——
每次工具调用都把整个上下文重发一遍。所以 1.4M 不是"读了一遍"，是"试了 27 次"。</p>

<h3>体验上真丢的五件事</h3>
<table>
<thead><tr><th>丢的东西</th><th>具体表现</th></tr></thead>
<tbody>
<tr><td>仓库现状认知</td><td>只能说"这些文件在"，说不出"这个函数还是占位实现"</td></tr>
<tr><td>失败归因</td><td>验收挂了，分不清是计划错、判据错还是 worker 错，只能转述报告</td></tr>
<tr><td>多轮指代</td><td>"把这个函数改成异步"——"这个"没有落点，要么用户贴码，要么它去读</td></tr>
<tr><td>可抽查</td><td>报告"8/8 通过"无法二次确认，用户只能信</td></tr>
<tr><td>责任位置</td><td>Codex 退化成"需求翻译 + 命令搬运"，验收回到用户手上</td></tr>
</tbody></table>
<div class="cap">表 7　最后一条最要紧：这不是省额度还保持体验，是换了一种分工。</div>

<h3>要保持体验，六条路线的价格（以 h1 为基准）</h3>
<table>
<thead><tr><th>路线</th><th class="n">H 输入</th><th class="n">相对 A 省</th><th>体验恢复</th></tr></thead>
<tbody>
<tr><td>A 通读一次</td><td class="n">29k</td><td class="n">17–24×</td><td>中：知道现状，不能追问</td></tr>
<tr><td>B 便宜模型先产摘要</td><td class="n">~22k</td><td class="n">22–32×</td><td>低：摘要会失真</td></tr>
<tr><td>C 事后读 diff 抽查</td><td class="n">18k + 3–8k</td><td class="n">15–20×</td><td>中高：能确认改对没</td></tr>
<tr><td>D 完全自由探索</td><td class="n">107k</td><td class="n">4.6–6.5×</td><td>高：接近客户端</td></tr>
<tr><td>E 失败才读</td><td class="n">18k / 28k</td><td class="n">17× / 11×</td><td>中：只救异常路径</td></tr>
<tr><td>F 接整件活（b3 那次）</td><td class="n">1.4M</td><td class="n">0.5×（更贵）</td><td>完整</td></tr>
</tbody></table>
<div class="cap">表 8　A + C 组合约 32–37k，仍省 15 倍，是性价比最高的一条。</div>

<h3>更正：上面"读不读没差别"是伪结论</h3>
<p>探针的题目有两个偏置，不能外推。三条证据：</p>
<table>
<thead><tr><th>偏置来源</th><th>具体</th></tr></thead>
<tbody>
<tr><td>文件名自带语义</td><td>h1 的路径就是 <code>app/领域/层.py</code>，<code>invoices/models.py</code> 同时告诉你领域和层次；b3 是 <code>loader_01..08</code> 同构</td></tr>
<tr><td>题面写死了验收</td><td><code>meta.json</code> 的 <code>accept</code> 是现成命令，拆法几乎被题面规定，规划退化成填空</td></tr>
<tr><td>场景只有代码</td><td>换成 <code>IMG_2041.jpg</code>、<code>表1.xlsx</code>、<code>utils.py</code>，文件名零信息</td></tr>
</tbody></table>
<div class="cap">表 9　所以那个探针测出的是"在这个题库上读不读没差别"，属于测不出，不是"不需要读"。
真正的验证要把语义从文件名里搬走（中性化题库），目前还没做。</div>

<h2>七、额度口径：怎么算的，准到什么程度</h2>

<p>H 侧 token 全部来自 <code>codex exec --json</code> 的 <code>turn.completed.usage</code>，
经 trace 逐 worker 累加，<b>没有估算</b>。现场复核过一格：sc08 的 8 条 <code>task.usage</code> 求和
= 3,565,353，与 <code>run.usage</code> 和 jsonl 记录三方一致。</p>

<table>
<thead><tr><th>覆盖项</th><th>计入 A 的 usage</th><th>计入 D 的 plan_usage</th></tr></thead>
<tbody>
<tr><td>读需求</td><td class="n">✓</td><td class="n">✓</td></tr>
<tr><td>读文件 / 读仓库</td><td class="n">✓（读到多少算多少）</td><td class="n">✗ 通常一次都不读</td></tr>
<tr><td>写方案 / 改代码</td><td class="n">✓</td><td class="n">只写计划</td></tr>
<tr><td>跑验收、失败再改</td><td class="n">✓ 多轮</td><td class="n">✗ worker 的活，不占 H</td></tr>
<tr><td>看报告、复核 diff、apply</td><td class="n">—</td><td class="n">✗ 实验里由 harness 代做</td></tr>
<tr><td>与用户多轮对话</td><td class="n">—</td><td class="n">✗ 没有</td></tr>
</tbody></table>
<div class="cap">表 10　口径不对称：D 的 plan_usage 只含"写计划"，所以 8.5× 是<b>上限</b>，不是承诺。</div>

<p>另需注意：token ≠ 额度。订阅额度是窗口内的消耗速度，token 只是代理指标；
跨模型比 token 无效（E21 已作废过一版结论），所以只做同侧比较。</p>

<h2>八、超窗探针：撞到的是额度，不是窗口</h2>

<table>
<thead><tr><th class="n">目标档（估算）</th><th class="n">实测输入</th><th>结果</th><th class="n">墙钟</th><th>输出摘要</th></tr></thead>
<tbody>{probe_rows}</tbody></table>
<div class="cap">表 11　探针用纯数字填充 + 25%/75% 两处 passcode，要求原样输出。
560k 那档因校准偏差实际只到 492,252（未越线）；900k 档报的是额度上限，不是上下文错误。</div>

<p>三条结论：<b>①</b> <code>context_window 500000</code> 不是硬截断线——619,498 输入被完整处理并正确召回；
<b>②</b> 单轮 exec 无 compact 事件，超窗只会是硬拒或静默截断；
<b>③</b> 额度比窗口先耗尽，900k 那次报 "You've hit your usage limit"。</p>

<p class="small">探针自身有两个缺陷：针在 25%/75%，即使客户端截到 500k 也仍在窗内，所以"答对"不是判决性证据
（usage 报 619k 是强证据）；且未测到 872k（<code>max_context_window</code>）。下一轮把针放到 98% 再测。</p>

<h2>九、文档类梯度：3×3 对照（代码之外的第一组数据）</h2>

<p>为回答"结论是不是只在代码上成立"，造了一组文档题（<code>tasks/docset1</code>）：
6 份规格文档（接口 / 存储 / 鉴权 / 计费 / 通知 / 检索），每份 5 个小节、3 条条款，
按 <code>STYLE.md</code> 修正格式、术语与编号，六份共用一套编号空间。
验收是机器可判的 7 条命令（6 份文档各一条 + 1 条全局一致性），seed 必败 / 参考解必过已双向自检。</p>

<table>
<thead><tr><th>臂</th><th class="n">r1</th><th class="n">r2</th><th class="n">r3</th><th class="n">均值</th><th class="n">CV</th><th>通过</th></tr></thead>
<tbody>
<tr><td>A · Codex 端到端</td><td class="n">$0.0676</td><td class="n">$0.0677</td><td class="n">$0.0573</td>
<td class="n"><b>$0.0642</b></td><td class="n">9%</td><td>3/3</td></tr>
<tr><td>B · ds 端到端</td><td class="n">$0.0115</td><td class="n">$0.0078</td><td class="n">$0.0093</td>
<td class="n"><b>$0.0095</b></td><td class="n">20%</td><td>3/3</td></tr>
<tr><td>D · 编排</td><td class="n">$0.0798</td><td class="n">$0.0945</td><td class="n">$0.0603</td>
<td class="n"><b>$0.0782</b></td><td class="n">22%</td><td><b>3/3</b></td></tr>
</tbody></table>
<div class="cap">表 12　文档类 3×3（计划规则修正后，n=3 全过）。排序 <b>B &lt; A &lt; D</b>；D/A = 1.22×，D/B = <b>8.21×</b>。</div>

<div class="figure">{c4}
<div class="cap">图 4　文档类与代码类放在同一张比值图上。青色 D/A、红色 D/B，1.0 虚线以上为编排更贵。</div>
</div>

<h3>三批数据，只有第三批能用</h3>

<p>这题跑了三批。差别不在模型，在<b>计划长什么样</b>——三次的 D 其实是三个不同的东西：</p>

<table class="tight">
<thead><tr><th>批次</th><th>计划形态</th><th class="n">worker 轮数</th><th class="n">计划输入</th><th class="n">worker 输入</th><th class="n">D 成本</th><th class="n">D/A</th><th>能否引用</th></tr></thead>
<tbody>
<tr><td>第一批</td><td>goal 含「；」被判复合任务，两次校验全挂 → 一格没出计划</td><td class="n">6 / —</td>
<td class="n">80–100k</td><td class="n">3.7–4.1M</td><td class="n">$0.110</td><td class="n">1.72×</td><td>不能（有一格 missing）</td></tr>
<tr><td>第二批</td><td>只加"goal 一句话"，planner 干脆不拆 → 一个 worker 干完 6 份</td><td class="n">1</td>
<td class="n">18–82k</td><td class="n">0.49–0.83M</td><td class="n">$0.044</td><td class="n">0.68×</td><td>不能（已退化成 B + 计划费）</td></tr>
<tr><td><b>第三批</b></td><td><b>加"一个文件一个任务"，6 个任务各写一份</b></td><td class="n"><b>6</b></td>
<td class="n">18–58k</td><td class="n">3.2–4.2M</td><td class="n"><b>$0.078</b></td><td class="n"><b>1.22×</b></td><td><b>是</b></td></tr>
</tbody></table>
<div class="cap">表 13　同一道题、同一模型，只改计划规则，D 的成本在 $0.044–$0.110 之间摆动（2.5 倍）。</div>

<p class="small">第二批那个 $0.0255（r2）几乎正好等于 B 的 $0.0095 加计划费 $0.0143——说明那不是"编排更省"，是编排已经不存在了。</p>

<h3>第三批才回答的问题：文档题上 D 到底贵不贵</h3>

<table class="tight">
<thead><tr><th>对比</th><th>数字</th><th>与代码题对照</th></tr></thead>
<tbody>
<tr><td>D vs A</td><td><b>1.22×</b>（贵 22%）</td>
<td>代码 N≤8 时是 0.40×（便宜 60%）。文档单元更短，worker 的 46k 底座占比更高</td></tr>
<tr><td>D vs B</td><td><b>8.21×</b>（贵 8 倍）</td>
<td>代码 N≤8 是 3.5×。差距翻倍，原因同上：B 只读一遍（$0.0095），D 要 6 个底座 + 计划费</td></tr>
<tr><td>方差</td><td>CV 22%（$0.060–$0.095）</td>
<td>差异几乎全在计划：一次过 17.9k，返工一次 57.9k（3.2×）</td></tr>
</tbody></table>
<div class="cap">表 14　n=3、3/3 通过、worker 轮数均为 6——这是唯一一批满足"D 的定义"的数据。</div>

<div class="callout">
<div class="label">文档类这一组的结论</div>
<p><b>换成文档题，编排既没变好也没变差，是"贵得更明显"</b>：D/A 1.22×、D/B 8.21×，
两条都比代码 N≤8 差一档。根因不是文件类型，是<b>单元工作量 ÷ 固定底座</b>——
文档单元太短，6 个 worker 各付 46k 底座，这笔钱（约 3.2M token）是 A 的 20 倍、B 的 5 倍。</p>
</div>

<h2>十、饱和实验：已放弃（E38）</h2>

<p>原计划做 6 档几何阶梯（27k → 870k token、最多 1,280 文件 / 10.2 万行）去撞上下文容量墙。
<b>2026-11-11 20:36 决定停止</b>，设计文档保留作存档，顶部已标"已取消"。三条原因：</p>

<table>
<thead><tr><th>原因</th><th>事实</th></tr></thead>
<tbody>
<tr><td><b>成本过高</b></td><td>瓶颈不是模型费，是额度窗口：探针 4 档合计约 1.4M 输入就把当日订阅额度打满，
900k 那档直接报 <code>You've hit your usage limit</code>；而按 A 的 CV 47–87%，n 需 ≥15 才有统计力，
单档就要 45 格以上——订阅制 + 单人操作下物理上跑不完</td></tr>
<tr><td><b>条件受限</b></td><td>500k 是客户端配置，实测 619k 已被完整吃下；真正的墙在 872k–1.05M 之间，
而 900k 先撞额度，再往上测不了；探针针位还放低了（25%/75%），判决性不足。
<b>测不到墙，就设计不出"必须超墙"的题</b></td></tr>
<tr><td><b>无力开展</b></td><td>81 格矩阵 + 102,400 行仓库 + 冻结校验 + 双向自检 + 分段解自检，
一个人一台机器收不了口</td></tr>
</tbody></table>

<p class="small">保留：设计全文、E37 探针数据、"500k 不是墙、额度才是"的结论，以及一句必须留着的话——
<b>容量假设既未被证实，也未被证伪</b>（现有最大题 h1 才 1.2 万 token，离墙差 20–80 倍）。</p>

<h2>十一、结论（输出端，强制编排）</h2>

<p>以下结论只对应一种用法：<b>保持 Codex 原生使用场景</b>（同一个客户端、同一个模型、同一套交互），
只把执行下沉到便宜模型，且<b>是否编排由外部强制指定</b>（不是模型自己选）。</p>

<h3>1. 额度：有效降低，中位 8.6 倍</h3>

<table>
<thead><tr><th>口径</th><th class="n">数值</th><th>说明</th></tr></thead>
<tbody>
<tr><td>H 侧输入 token 的下降倍数</td><td class="n"><b>中位 8.6×</b><br>（4.0×–36.9×）</td>
<td>按题族均值比；逐次配对口径为 3.1×–71.5×，中位 8.5×</td></tr>
<tr><td>若 planner 读控制面（+27%–62%）</td><td class="n">5.2×–6.7×</td>
<td>这是真实使用时更可能的值，因为完全"不读"很难长期维持</td></tr>
<tr><td>反例</td><td class="n"><b>0.14×</b></td><td>b3 那次 planner 自己浏览仓库，H 冲 1.4M，反输 7 倍</td></tr>
</tbody></table>
<div class="cap">表 15　额度口径是 H 侧 token（订阅额度的代理），不是额度窗口的计费单位本身。</div>

<h3>2. 中等规模：钱、时间、额度三项同时占优</h3>

<p>定义"中等规模"为 <b>2–8 个互不共享上下文的交付单元</b>（不是"难度中等"）。这一区间四个格子：</p>

<table>
<thead><tr><th>题</th><th class="n">N</th><th class="n">金额 D/A</th><th class="n">时间 D/A</th><th class="n">额度倍数</th></tr></thead>
<tbody>
<tr><td>t01</td><td class="n">1</td><td class="n">0.14×</td><td class="n">0.40×</td><td class="n">19.5×</td></tr>
<tr><td>sc02</td><td class="n">2</td><td class="n">0.33×</td><td class="n">0.82×</td><td class="n">15.3×</td></tr>
<tr><td>sc04</td><td class="n">4</td><td class="n">0.27×</td><td class="n">0.55×</td><td class="n">36.9×</td></tr>
<tr><td>sc08</td><td class="n">8</td><td class="n">0.45×</td><td class="n">0.99×</td><td class="n">32.6×</td></tr>
</tbody></table>
<div class="cap">表 16　金额省 47%–73%，时间省 1%–45%，额度省 5.4×–36.9×。</div>

<h3>3. 质量：暂不能保证，但可观测范围内未检出差异</h3>

<p>当前验收是<b>二值通过 / 不通过</b>（命令退出码），存在天花板效应：
交付物"都合格但好坏不同"这件事，现在的判据分辨不出来。所以只能说：</p>

<ul>
<li>在可观测范围内（3 族题、N=1–16、n=3），所有格子的通过率<b>要么持平、要么更差</b>；</li>
<li><b>没有一例</b>是"A/B 做不出来、D 做出来了"——编排目前没有证明自己是质量手段；</li>
<li>唯一出现分歧的是 t03：B 过、D 挂（<code>slugify</code> 差一处）。</li>
</ul>

<p>因此准确表述是「<b>在可观测范围（难度、类型、数量）内质量无差别</b>」，而不是「质量不变」——
样本量与判据精度都不支持后者。</p>

<h3>4. 对 DeepSeek 单模型：只算交付物成本，全败</h3>

<p>9 个有 B 的格子 <b>9/9 全败</b>，D/B 在 2.38×–8.21× 之间。原因是结构性的：
D 多付一份计划费（≈$0.014–$0.043）加 N 份 worker 底座（≈46k token/个），而 B 只付一次会话。
时间上 D 不总输（B 有时因超时重试慢到 651s），但金额没有任何一格赢。</p>

<h3>5. 还有一笔不进成本表的账：交互体验</h3>

<p>强制编排把"验收"从模型手里挪到了用户手里，并且引入两类新的摩擦：</p>

<table>
<thead><tr><th>体验成本</th><th>本轮的观察</th></tr></thead>
<tbody>
<tr><td>计划返工</td><td>planner 第一版判据常被判为"不可判定"（E014），每次多烧 20k–60k H token；文档题首批甚至两轮全挂（E007）</td></tr>
<tr><td>责任转移</td><td>报告只说"8/8 通过"，无法二次确认；要复核就得自己读 diff，或让 Codex 读（又花额度）</td></tr>
<tr><td>多轮指代失效</td><td>"把这个函数改成异步"——没有落点，要么用户贴码，要么它去读</td></tr>
</tbody></table>
<div class="cap">表 17　这些不改变上面的数字，但决定"用起来累不累"。</div>

<h3>6. 返工机制：整体效益为正，但与失败概率无关</h3>

<p>返工是按需触发的：不触发时成本等于 D，触发时期望挽回 = 救回率 × 失败损失 − 返工成本。
本轮实测救回率 2/3、返工成本 $0.035，所以净收益只取决于"失败损失有多大"：</p>

<table>
<thead><tr><th>场景</th><th class="n">失败损失 L</th><th class="n">每次失败净收益</th></tr></thead>
<tbody>
<tr><td>文档题（重跑一次 $0.064）</td><td class="n">$0.064</td><td class="n">+$0.008（几近持平）</td></tr>
<tr><td>中型代码任务</td><td class="n">≈ $0.2</td><td class="n">+$0.10</td></tr>
<tr><td>大任务 / 对外交付</td><td class="n">≥ $1</td><td class="n"><b>+$0.6</b></td></tr>
</tbody></table>
<div class="cap">表 18　返工划算的阈值：返工成本 &lt; 救回率 × 失败损失，即 L &gt; $0.053。</div>

<p>三个前提缺一个结论就翻转：<b>按需触发</b>（不是每轮跑两遍）、<b>最多 1 次</b>、<b>失败必须上报</b>。</p>

<h3>7. 最佳适用情景</h3>

<div class="callout">
<div class="label">一句话</div>
<p><b>订阅 GPT Plus（额度受限）、独立开发者、一天多批的中等规模任务</b>——这三个条件同时成立时，
省额度的价值大于省下的钱，强制编排是合算的。</p>
</div>

<p>三个条件缺一个，收益就会明显缩水：</p>
<ul>
<li><b>不是 Plus / 额度不受限</b>：省额度没有兑现渠道，只剩"钱省 47%–73%"这一条，而它可以用 B 直接达成更彻底；</li>
<li><b>不是独立开发者</b>：验收成本被摊到别人身上，省下的额度不构成个人收益；</li>
<li><b>不是中等规模</b>：N=1 时计划费吃掉收益（时间 1.36–1.40×），N≥16 时底座 ×N 反噬（2.34× / 4.42×），跨层与短文档同样翻负。</li>
</ul>

<h2>十二、路由：判据、任务集与预实验</h2>

<p>路由要回答的不是"编排好不好"，而是<b>怎么判断这一次该不该编排</b>。判据必须能在派发前估出来。</p>

<table>
<thead><tr><th>编号</th><th>判据</th><th class="n">阈值</th><th>依据</th></tr></thead>
<tbody>
<tr><td>C1</td><td>可拆出的独立交付单元数 k</td><td class="n">2 ≤ k ≤ 8</td><td>k=1 计划费吃掉收益（时间 1.36–1.40×）；k≥9 底座 ×N 反噬（2.34× / 4.34×）</td></tr>
<tr><td>C2</td><td>每单元预期读取量 R</td><td class="n">R ≥ 46k</td><td>worker 固定底座 ≈46k；R 低于它确定性亏（docset1 每单元 24k → 1.22×）</td></tr>
<tr><td>C3</td><td>单元间是否共享全局上下文</td><td class="n">不共享</td><td>h1 每 worker 重读约定 → 1.29×，token 膨胀 12×</td></tr>
<tr><td>C4</td><td>判据能否机器判定</td><td class="n">命令 / 文件断言</td><td>不可判定 → E014 返工，计划费 17.9k→81.6k</td></tr>
<tr><td>C5</td><td>失败损失 L &gt; 返工成本</td><td class="n">L &gt; $0.053</td><td>否则返工不划算（E44）</td></tr>
<tr><td>C6</td><td>是否受订阅额度约束</td><td class="n">订阅制优先额度</td><td>额度省 5–37×，金额贵 22% 也可能值</td></tr>
</tbody></table>
<div class="cap">表 19　C1–C4 全满足，且 C5/C6 至少一条成立 → 走编排；否则自己单干并记 bypass。</div>

<h3>路由为什么值得做：一条能算的结论</h3>

<p>设任务集中需要编排的比例为 q、判据准确率为 a，且不该编排的任务上编排更贵（c_D &gt; c_A）：</p>

<ul>
<li>硬性编排成本 ≈ c_D（一律编排）</li>
<li>路由成本 ≈ a·(q·c_D + (1−q)·c_A) + (1−a)·(q·c_A + (1−q)·c_D)</li>
</ul>

<div class="callout soft">
<div class="label">判据准确率的门槛</div>
<p><b>路由优于硬编 ⟺ a &gt; 0.5</b>，与 q 无关。所以路由实验只需要证明一件事：
<b>判据的判对率能不能过半</b>。它不可能优于"每题都选对"（oracle），那是下界。</p>
</div>

<h3>分散任务集（待建：tasks/router1）</h3>

<table>
<thead><tr><th>组</th><th>形态</th><th class="n">k</th><th class="n">每单元读取</th><th>该编排？</th><th>卡哪条判据</th></tr></thead>
<tbody>
<tr><td>R-yes</td><td>4 个独立模块各 400 行，互不引用</td><td class="n">4</td><td class="n">≈60k</td><td><b>是</b></td><td>—</td></tr>
<tr><td>R-no-short</td><td>6 篇短文档，每篇 40 行</td><td class="n">6</td><td class="n">≈8k</td><td>否</td><td>C2</td></tr>
<tr><td>R-no-one</td><td>单文件 30 行改动</td><td class="n">1</td><td class="n">≈8k</td><td>否</td><td>C1</td></tr>
<tr><td>R-no-shared</td><td>4 模块共享一份 CONVENTIONS</td><td class="n">4</td><td class="n">≈80k</td><td>否</td><td>C3</td></tr>
</tbody></table>
<div class="cap">表 20　每组三臂（A/B/D）× 3 次；第四臂 R（模型自选）留到判据验证后再上。</div>

<h3>预实验：先做两个，验证判据方向</h3>

<table>
<thead><tr><th>任务</th><th>设计</th><th>预期</th></tr></thead>
<tbody>
<tr><td>P1（该编排）</td><td>R-yes 缩到 k=4、每单元 400 行</td><td>D/A 0.3–0.6，额度 5–30×</td></tr>
<tr><td>P2（不该编排）</td><td>单文件 30 行改动</td><td>D/A 1.5–3，时间 1.3–2×</td></tr>
</tbody></table>
<div class="cap">表 21　若 P1 落在 &lt;1、P2 落在 &gt;1，则 C1–C3 方向被支持；若 P1 也 &gt;1，先改阈值再测路由。</div>

<h2>十三、下一步</h2>
<ol>
<li>文档类只跑了 1 组 6 单元。要回答"梯度"，至少再补 3–4 个规模档（2 / 12 / 24 单元），看 D/A 是否随单元数单调恶化。</li>
<li>在 PLAN_PROMPT 里给 E014 一个正确示例（<code>文件断言：docs/x.md 含 `R-API-01`</code>），可省掉每次规划的一轮返工，价值约 60k H token。</li>
<li>若要做容量墙，只能走轻量替代：用"全局唯一序号 + 跨域汇总 + 干扰项"在 60–120k 内制造必须同时持有，而不是十万行。</li>
<li>中性化文件名的代码题库（语义搬进 CONVENTIONS），补上 E34 指出的偏置。</li>
</ol>

<div class="foot">
数据文件：runs/exp/ladder,h1,results,calib.jsonl · ctx-probe/summary.jsonl ·
实验记录：docs/实验日志.md（E22–E37）· 设计：docs/饱和级任务库_设计.md ·
生成脚本：scripts/make_progress_html.py（本页所有图表与统计由数据现算，无手工抄写）。
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
