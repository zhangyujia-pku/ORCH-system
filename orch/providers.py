"""
把 workers.json 的 argv 模板变成可执行的 Provider，并按分派结果取用。

为什么单独一层：
    路由决策（route()）只回答"该派给谁"，它不该知道 codebuddy 要用 node 启动、
    codex 的路径会随自动更新变化。这些是实现细节，混进路由会让两边都难测。

设计约束：
    - 只认 workers.json 里的 argv 模板，不在这里硬编码命令行——改后端只改 JSON。
    - {cwd} {prompt} {out_file} {prompt_file} {port} 由 CLIProvider 在调用时替换，
      {codex_bin} {cbc_bin} {model} 在这里一次性替换（它们不随调用变化）。
    - 找不到可执行文件不抛异常，返回 None 并记原因：让上层决定是降级还是拒跑。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .contract import Task
from .planner import CLIProvider, Provider, resolve_codex
from .workers import Worker


def _node() -> str:
    return os.environ.get("NODE_BIN") or shutil.which("node") or "node"


def resolve_cbc() -> list[str]:
    """WorkBuddy 自带 CLI。注意它是个 node 脚本，没有 .cmd 垫片。"""
    cand = os.environ.get("CBC_BIN")
    if cand and Path(cand).exists():
        return [cand]

    roots = [
        Path("D:/应用数据/WorkBuddy/Application/resources/app.asar.unpacked/cli/bin"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/WorkBuddy/resources/app.asar.unpacked/cli/bin",
    ]
    for root in roots:
        for name in ("codebuddy.cmd", "codebuddy.exe", "codebuddy"):
            p = root / name
            if p.exists():
                if p.suffix.lower() in (".cmd", ".exe", ".bat"):
                    return [str(p)]
                return [_node(), str(p)]
    found = shutil.which("codebuddy") or shutil.which("cbc")
    return [found] if found else []


BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"


def apply_sandbox(argv: list[str], sandbox: str | None) -> list[str]:
    """把 argv 里的沙箱决定改成调用方要的那个。

    为什么需要这一步：`workers.json` 的模板写死了 `--sandbox workspace-write`，
    而调用方（dispatch）在 Windows 上会改成 bypass。模板不改、传参也不改的话，
    日志里写着 bypass、实际发出去还是 workspace-write——**trace 会替你撒谎**，
    这比没配更难查：看日志的人会以为已经配好了。

    `bypass` 用的是 codex 的独立开关（不是 `--sandbox` 的取值），
    所以这里要把两者当成互斥的两套写法处理。
    """
    if not sandbox:
        return list(argv)

    cleaned: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--sandbox" and i + 1 < len(argv):
            i += 2                      # 连同取值一起丢掉
            continue
        if tok == BYPASS_FLAG:
            i += 1
            continue
        cleaned.append(tok)
        i += 1

    want = [BYPASS_FLAG] if sandbox == "bypass" else ["--sandbox", sandbox]
    # 插在子命令（exec）之后。放末尾会排在 prompt 这个位置参数后面，
    # 有的 CLI 接受、有的不接受，不该赌。
    for idx, tok in enumerate(cleaned):
        if tok == "exec":
            return cleaned[: idx + 1] + want + cleaned[idx + 1:]
    return want + cleaned


def expand(argv: list[str], worker: Worker) -> tuple[list[str], str]:
    """替换一次性占位符。返回 (argv, 失败原因)。"""
    bins = {"codex_bin": [resolve_codex()], "cbc_bin": resolve_cbc()}
    out: list[str] = []
    for token in argv:
        key = token.strip("{}")
        if token.startswith("{") and key in bins:
            if not bins[key]:
                return [], f"找不到 {key} 对应的可执行文件"
            out.extend(bins[key])
        elif token == "{model}":
            out.append(worker.model or "")
        else:
            out.append(token)
    return [a for a in out if a != ""], ""


def build(worker: Worker, sandbox: str | None = None) -> tuple[Provider | None, str]:
    """造一个 worker 的 Provider。失败时返回 (None, 原因)。

    sandbox 为 None 时沿用 workers.json 模板里的写法；给了值就覆盖——
    这是 `--worker-sandbox` 与 Windows 默认 bypass 真正生效的地方。
    """
    if not worker.argv:
        return None, "workers.json 里没有 argv 模板"
    argv, why = expand(list(worker.argv), worker)
    if not argv:
        return None, why
    return CLIProvider(apply_sandbox(argv, sandbox), name=worker.name,
                       model=worker.model), ""


def build_named(name: str, workers_path: str | None = None,
                sandbox: str | None = None) -> tuple[Provider | None, str]:
    """按名字造一个 provider，给 `--provider` 用。

    `deepseek` 走 workers.json 里最便宜的档（codex-ds-flash，退化到 codex-ds）。
    刻意和 `--workers` 共用同一份注册表——如果这里另写一套 argv，
    就会出现「--provider 和 --workers 行为不一致」的两套真相。
    """
    from .workers import load_workers

    if name not in ("deepseek", "ds", "flash"):
        return None, f"未知 provider：{name}"

    ws, _ladder = load_workers(workers_path)
    for key in ("codex-ds-flash", "codex-ds"):
        if key in ws:
            prov, why = build(ws[key], sandbox=sandbox)
            if prov is not None:
                return prov, ""
            return None, f"{key} 造不出来：{why}"
    return None, "workers.json 里没有可用的 deepseek 档（需要 codex-ds-flash 或 codex-ds）"


class WorkerRouter:
    """按分派表取 provider。runner 只需要 `for_task()` 这一个方法。"""

    def __init__(self, providers: dict[str, Provider], assign: dict[str, str],
                 default: str | None = None, strict: bool = False, candidates: dict | None = None):
        self.providers = providers
        self.assign = assign
        self.default = default or next(iter(providers), None)
        self.strict = strict
        self.candidates = candidates or {}

    def name_for(self, task: Task) -> str:
        if self.strict and task.id not in self.assign:
            raise KeyError(f"任务 {task.id} 缺少明确分派，不能使用默认 worker")
        return self.assign.get(task.id, self.default or "")

    def for_task(self, task: Task) -> Provider:
        name = self.name_for(task) or ""
        if name not in self.providers:
            raise KeyError(
                f"任务 {task.id} 被派给 '{name}'，但它没有可用的 provider"
                f"（可用：{', '.join(self.providers) or '无'}）"
            )
        return self.providers[name]

    def distribution(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for name in self.assign.values():
            out[name] = out.get(name, 0) + 1
        return out

    def for_attempt(self, task: Task, attempt: int, switch_after: int):
        chain = self.candidates.get(task.id, [self.name_for(task)])
        name = chain[min((attempt - 1) // switch_after, len(chain) - 1)]
        return self.providers[name]


def build_router(assign: dict[str, str], workers: dict[str, Worker],
                 default: str | None = None, candidates: dict | None = None) -> tuple[WorkerRouter | None, list[str]]:
    """按分派表里**实际用到**的 worker 造 provider（不用的就不造，省得报错）。"""
    used = sorted({n for names in candidates.values() for n in names}) if candidates else sorted(set(assign.values()))
    used = used or ([default] if default else [])
    providers: dict[str, Provider] = {}
    problems: list[str] = []
    for name in used:
        w = workers.get(name)
        if w is None:
            problems.append(f"{name}: 不在 workers.json 里")
            continue
        prov, why = build(w)
        if prov is None:
            problems.append(f"{name}: {why}")
            continue
        providers[name] = prov
    if candidates:
        chains = {task: [n for n in names if n in providers] for task, names in candidates.items()}
        if any(not names for names in chains.values()):
            return None, problems or ["某任务没有可用的合格候选"]
        selected = {task: names[0] for task, names in chains.items()}
        return WorkerRouter(providers, selected, strict=True, candidates=chains), problems
    if problems or not providers:
        return None, problems
    return WorkerRouter(providers, assign, default, strict=True), problems
