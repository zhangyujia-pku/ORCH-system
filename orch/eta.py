"""预估一次运行要多久，据此选阻塞还是投递。

为什么需要它：
  外层单次调用只验到 600 秒。批次超过这个量级，阻塞就会被掐断——
  而掐断时任务还在跑，用户看到的是"没结果"，不是"还在跑"。
  反过来，明明 2 分钟能跑完却走投递，就要多一次唤醒的延迟。

预估的来源是**历史实测**，不是拍脑袋的常数：
  从过去的 trace 里取"每层墙钟"（run 总时长 ÷ 层数），层内任务并行，
  所以总时长 ≈ ceil(任务数/并发) × 每层墙钟 + 固定开销。

  没有历史时退回 150 秒/层的粗估，并在输出里标明 basis=default——
  让人知道这个数字有多可信。
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from statistics import median

DEFAULT_LAYER_SECONDS = 150.0     # 无历史时的粗估
FIXED_OVERHEAD = 20.0             # 建 worktree、跑整体验收的固定开销
BLOCK_LIMIT = 480.0               # 外层单次调用只验到 600 秒，留 20% 余量


def _parse_ts(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


def layers_of(trace: Path) -> int:
    n = 0
    try:
        for line in trace.read_text(encoding="utf-8").splitlines():
            if '"event": "layer.start"' in line or '"event":"layer.start"' in line:
                n += 1
    except OSError:
        return 0
    return n


def run_elapsed_and_layers(run_dir: Path) -> tuple[float, int] | None:
    trace = run_dir / "trace.jsonl"
    if not trace.exists():
        return None
    first = last = None
    try:
        for line in trace.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = _parse_ts(e.get("ts", ""))
            if ts is None:
                continue
            first = ts if first is None else first
            last = ts
    except OSError:
        return None
    if first is None or last is None or last <= first:
        return None
    return last - first, layers_of(trace)


def history_per_layer(runs_dir: str | Path, limit: int = 30) -> list[float]:
    """历史里"每层墙钟"的样本。只取有层记录且跑完了的运行。"""
    root = Path(runs_dir)
    if not root.exists():
        return []
    samples: list[float] = []
    dirs = [d for d in root.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.name, reverse=True)     # 名字带时间戳，倒序即最新优先
    for d in dirs[:limit]:
        got = run_elapsed_and_layers(d)
        if not got:
            continue
        elapsed, layers = got
        if layers >= 1 and elapsed > 0:
            samples.append(elapsed / layers)
    return samples


def estimate(runs_dir: str | Path, n_tasks: int, max_agents: int = 3) -> dict:
    n_tasks = max(1, int(n_tasks))
    agents = max(1, min(int(max_agents), n_tasks))
    samples = history_per_layer(runs_dir)
    if samples:
        per_layer = float(median(samples))
        basis = f"history（{len(samples)} 个样本，每层中位数 {per_layer:.0f}s）"
    else:
        per_layer = DEFAULT_LAYER_SECONDS
        basis = f"default（无历史，按 {per_layer:.0f}s/层粗估）"
    waves = math.ceil(n_tasks / agents)
    total = waves * per_layer + FIXED_OVERHEAD
    return {
        "seconds": round(total, 1),
        "minutes": round(total / 60, 1),
        "waves": waves,
        "per_layer": round(per_layer, 1),
        "agents": agents,
        "basis": basis,
    }


def choose(est: dict, host_running: bool, limit: float = BLOCK_LIMIT) -> tuple[str, str]:
    """返回 (mode, 理由)。"""
    secs = float(est.get("seconds", 0))
    if secs <= limit:
        return "foreground", f"预计 {est['minutes']} 分钟，单次调用等得完"
    if host_running:
        return "serve", (f"预计 {est['minutes']} 分钟，超过单次上限 {int(limit)}s；"
                         f"交给常驻宿主，跑完投回")
    return "foreground", (f"预计 {est['minutes']} 分钟，超过单次上限 {int(limit)}s，"
                          f"但宿主没启动：只能当场跑，可能被掐断。"
                          f"建议双击 start-orch-serve.vbs 后改走投递")


def render(est: dict) -> str:
    return (f"预计 {est['minutes']} 分钟（{est['waves']} 层 × {est['per_layer']}s，"
            f"并发 {est['agents']}）· 依据：{est['basis']}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m orch.eta", description="估一下这批要跑多久、该阻塞还是投递")
    ap.add_argument("--tasks", type=int, required=True)
    ap.add_argument("--max-agents", type=int, default=3)
    ap.add_argument("--runs-dir", default="runs")
    args = ap.parse_args(argv)

    est = estimate(args.runs_dir, args.tasks, args.max_agents)
    try:
        from .dispatch import serve_pid

        host = bool(serve_pid())
    except Exception:                 # noqa: BLE001
        host = False
    mode, why = choose(est, host_running=host)
    print(render(est))
    print(f"建议     {mode}（{why}）")
    print(f"宿主     {'在跑' if host else '未启动'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
