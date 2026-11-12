"""基线：让 Codex 独自做完同一件事，量它花了多少。

为什么要有它：
  v1.9.3 之后 orch 侧的 token 能读出来了，但"省了多少"还需要**另一半**——
  同一件事让 Codex 自己做，花多少。两边用同一个解析口径（--json 事件流），
  数字才可比；不然就是拿两种秤称同一批货。

怎么保证可比：
  - 仓库从同一条提交 clone 出来（不是 copytree，避免把脏工作区带进去）；
  - 任务描述原样给，不拆、不补、不给计划；
  - 验收命令与 orch 那次相同；跑完记录退出码与输出；
  - token 用 orch 里同一个 parse_json_events 解析。

用法：
  python scripts/baseline.py --repo <仓库> --task "<原话>" \
      [--accept "<命令>"]... [--model <模型>] [--timeout 900] [--json-out out.json]

  --vs <orch run 目录>   顺便把 orch 那次的用量并排打出来
  --dry-run              只打印将要执行的命令，不调用模型
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orch.planner import parse_json_events, resolve_codex  # noqa: E402
from orch.providers import build as build_worker_provider  # noqa: E402
from orch.workers import load_workers  # noqa: E402


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _run(argv: list[str], cwd: Path, timeout: int, stdin_text: str | None = None):
    t0 = time.time()
    p = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout,
                       input=(stdin_text.encode("utf-8") if stdin_text else None))
    return p, time.time() - t0


def clone_repo(src: Path) -> Path:
    """从同一条提交 clone：比拷工作区干净，也不会把未提交改动带进基线。"""
    tmp = Path(tempfile.mkdtemp(prefix="orch-baseline-"))
    dst = tmp / src.name
    r = subprocess.run(["git", "clone", "--quiet", str(src), str(dst)],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] clone 失败：{_decode(r.stderr).strip()[:300]}")
    return dst


def build_argv(work: Path, worker: str | None, model: str | None,
               sandbox: str, raw: bool) -> tuple[list[str], str]:
    """拼命令行。两条路，别混：

    --raw        用 codex 自己的默认 provider 与模型（那才是"Codex 独自"）
    --worker X   复用 workers.json 里 X 的模板（同一模型、同一套 -c 参数）

    为什么要复用模板：光给个 `-m deepseek-flash` 是不够的——provider 的 base_url、
    wire_api、env_key 都在那几个 -c 里。少一个，codex 会拿默认 provider 去请求，
    报的却是"模型不可用"，看着像模型的问题。
    """
    if raw:
        argv = [resolve_codex(), "exec", "--json", "--skip-git-repo-check"]
        if sandbox == "bypass":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--sandbox", sandbox]
        if model:
            argv += ["-m", model]
        return argv, model or "codex 默认"

    name = worker or "codex-ds-flash"
    ws, _ = load_workers(str(ROOT / "workers.json"))
    if name not in ws:
        raise SystemExit(f"[FAIL] workers.json 里没有 {name}")
    prov, why = build_worker_provider(ws[name], sandbox=sandbox)
    if prov is None:
        raise SystemExit(f"[FAIL] 造不出 provider：{why}")
    argv = [a.replace("{cwd}", str(work)) for a in prov.argv if a != "{prompt}"]
    if "--json" not in argv:
        argv.insert(argv.index("exec") + 1, "--json")
    if model:
        argv = [a for a in argv]
        try:
            argv[argv.index("-m") + 1] = model
        except ValueError:
            argv += ["-m", model]
    return argv, f"{name} / {model or ws[name].model}"


def count_turns(stdout: str) -> int:
    """数 turn.completed。

    为什么值得记：一次 agent 会话里每个 turn 都要重发完整上下文。
    同一个任务，10 轮就是 10 份输入；只报合计看不出"是 prompt 大还是轮次多"。
    """
    n = 0
    for line in (stdout or "").splitlines():
        if '"turn.completed"' in line:
            n += 1
    return n


def git_changes(repo: Path) -> dict:
    def g(*a):
        return _decode(subprocess.run(["git", *a], cwd=str(repo),
                                      capture_output=True).stdout).strip()

    names = [ln[3:] for ln in g("status", "--porcelain").splitlines() if ln.strip()]
    stat = g("diff", "--stat", "HEAD")
    added = sum(1 for ln in g("diff", "HEAD").splitlines()
                if ln.startswith("+") and not ln.startswith("+++"))
    return {"files": names, "diff_stat": stat.splitlines()[-1] if stat else "",
            "added_lines": added}


def read_orch_usage(run_dir: Path) -> dict:
    """从 orch 的 trace 里读那次运行的汇总用量，用来并排比较。"""
    trace = run_dir / "trace.jsonl"
    if not trace.exists():
        return {}
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "run.usage":
            return e.get("data", {})
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Codex 独自完成同一任务的基线测量")
    ap.add_argument("--repo", required=True, help="基线仓库（从它的 HEAD clone）")
    ap.add_argument("--task", required=True, help="给 Codex 的原话，一字不改")
    ap.add_argument("--accept", action="append", default=[],
                    help="验收命令，可重复；与 orch 那次保持一致")
    ap.add_argument("--model", default=None,
                    help="覆盖 worker 档位里的模型；--raw 时是传给 codex 的 -m")
    ap.add_argument("--worker", default="codex-ds-flash",
                    help="复用 workers.json 里哪一档的 argv（默认最便宜那档）")
    ap.add_argument("--raw", action="store_true",
                    help="不复用模板，跑 codex 自己的默认 provider/模型——这才是'Codex 独自'")
    ap.add_argument("--sandbox", default="bypass" if os.name == "nt" else "workspace-write",
                    choices=["bypass", "workspace-write", "read-only"])
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--vs", default=None, help=" orch 的 run 目录，用于并排比较")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep", action="store_true", help="保留临时仓库，便于查看产物")
    args = ap.parse_args(argv)

    src = Path(args.repo).resolve()
    if not src.exists():
        print(f"[FAIL] 仓库不存在：{src}", file=sys.stderr)
        return 2

    if args.dry_run:
        cmd, label = build_argv(Path("<clone>"), args.worker, args.model,
                                args.sandbox, args.raw)
        print(f"将执行（{label}）：")
        print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        print(f"\n临时 clone：{src} → <tmp>")
        print(f"stdin：{args.task}")
        print(f"验收：{args.accept or '（无）'}")
        return 0

    work = clone_repo(src)
    try:
        cmd, model_label = build_argv(work, args.worker, args.model,
                                      args.sandbox, args.raw)
        print(f"基线：{model_label} · 沙箱 {args.sandbox} · 超时 {args.timeout}s")
        try:
            proc, elapsed = _run(cmd, work, args.timeout, stdin_text=args.task)
        except subprocess.TimeoutExpired:
            print(f"[FAIL] 基线超时（{args.timeout}s）", file=sys.stderr)
            return 3

        stdout, stderr = _decode(proc.stdout), _decode(proc.stderr)
        message, usage = parse_json_events(stdout)
        turns = count_turns(stdout)
        changes = git_changes(work)

        results = []
        for c in args.accept:
            p = subprocess.run(c, cwd=str(work), shell=True, capture_output=True, timeout=300)
            results.append({"command": c, "ok": p.returncode == 0,
                            "returncode": p.returncode,
                            "stdout": _decode(p.stdout)[-600:],
                            "stderr": _decode(p.stderr)[-300:]})

        out = {
            "task": args.task,
            "model": args.model or "",
            "model_label": model_label,
            "sandbox": args.sandbox,
            "exit_code": proc.returncode,
            "wall_s": round(elapsed, 1),
            "usage": usage,
            "turns": turns,
            "tokens_uncached_in": max(0, usage.get("input_tokens", 0)
                                      - usage.get("cached_input_tokens", 0)),
            "changes": changes,
            "acceptance": results,
            "acceptance_ok": all(r["ok"] for r in results) if results else None,
            "message": message[-1200:],
        }
        if args.keep:
            out["workdir"] = str(work)

        print()
        if usage:
            print(f"token  输入 {usage.get('input_tokens', 0):,}"
                  f"（缓存 {usage.get('cached_input_tokens', 0):,}）"
                  f"· 输出 {usage.get('output_tokens', 0):,}")
        else:
            print("token  未采集（事件里没有 usage）")
        print(f"墙钟   {elapsed:.1f}s · 轮次 {turns} · 退出码 {proc.returncode}")
        print(f"改动   {len(changes['files'])} 个文件 · 新增 {changes['added_lines']} 行")
        if results:
            print("验收   " + "；".join(
                f"{'OK' if r['ok'] else 'FAIL'}（退出码 {r['returncode']}）" for r in results))
        if proc.returncode != 0:
            print(f"stderr {stderr.strip()[-400:]}", file=sys.stderr)

        if args.vs:
            orch = read_orch_usage(Path(args.vs))
            if not orch:
                print(f"\n并排：{args.vs} 里没有 run.usage（旧版本或未采集）")
            else:
                print("\n并排（同口径）：")
                print(f"  Codex 独自   输入 {usage.get('input_tokens', 0):>9,}"
                      f"（缓存 {usage.get('cached_input_tokens', 0):>9,}）"
                      f"  输出 {usage.get('output_tokens', 0):>7,}")
                print(f"  orch worker  输入 {orch.get('tokens_in', 0):>9,}"
                      f"（缓存 {orch.get('tokens_cached', 0):>9,}）"
                      f"  输出 {orch.get('tokens_out', 0):>7,}")
                ui = usage.get("input_tokens", 0); oi = orch.get("tokens_in", 0)
                if oi:
                    print(f"  输入比        {ui / oi:.2f}×（>1 表示独自做更费）")

        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n明细已写入 {args.json_out}")

        return 0 if (not results or out["acceptance_ok"]) else 4
    finally:
        if not args.keep:
            shutil.rmtree(work.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
