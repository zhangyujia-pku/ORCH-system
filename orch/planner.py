"""
M1 · Planner：把 intent.md 编译成 tasks.json，并用校验器逼它改到合格。

核心不是"让模型一次性想对"，而是**给它结构化的拒绝理由，让它改**。
模型第一次拆得不对是常态；能不能被机械地纠正回来，才是工程问题。

三种 provider：
  - stub  ：离线，读预置响应。用于测试和演示整条链路。
  - cli   ：调用任意命令行 agent（codex / claude / gemini …），argv 由 JSON 数组模板给出。
  - 自己写：实现 complete() 即可，比如走 HTTP API。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .contract import (
    Report,
    Task,
    Violation,
    decode_bytes,
    parse_document,
    repair_dependencies,
    repo_snapshot,
    validate_tasks,
)
from .prompts import build_prompt
from .trace import Tracer

# prompt 超过这个长度就不再塞进 argv，改走 stdin。取 7000 是留足余量：
# cmd.exe 的整条命令上限是 8191 字符，而 codex 常被 .cmd 包装脚本间接启动。
_PROMPT_ARGV_LIMIT = 7000


class PlanParseError(ValueError):
    """模型输出里找不到合法 JSON。"""


class ProviderError(RuntimeError):
    """provider 进程失败或超时。"""


# --------------------------------------------------------------------------
# JSON 提取：模型输出永远不会是干净的 JSON
# --------------------------------------------------------------------------

def _strip_trailing_commas(s: str) -> str:
    out: list[str] = []
    in_str = False
    esc = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            if j < len(s) and s[j] in "}]":
                i += 1  # 丢掉这个逗号
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _load(s: str):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def extract_json(text: str) -> dict:
    """从模型的自由输出里挖出第一个合法 JSON 对象。"""
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise PlanParseError("模型返回空字符串")

    blocks = re.findall(r"```(?:json)?\s*(.+?)```", text, re.S)
    for raw in blocks + [text]:
        for cand in (raw.strip(), _strip_trailing_commas(raw.strip())):
            obj = _load(cand)
            if isinstance(obj, dict):
                return obj

    start = text.find("{")
    if start >= 0:
        for cand in (text[start:], _strip_trailing_commas(text[start:])):
            try:
                obj, _ = json.JSONDecoder().raw_decode(cand)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

    head = text[:120].replace("\n", " ")
    raise PlanParseError(f"找不到合法 JSON 对象；开头是：{head!r}")


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------

class Provider:
    name = "base"

    def complete(self, prompt: str, cwd: Path, timeout: int = 300) -> str:
        raise NotImplementedError


class StubProvider(Provider):
    """离线：按顺序吐预置响应，最后一个会被重复使用。"""

    name = "stub"

    def __init__(self, responses: list[str], name: str = "stub"):
        if not responses:
            raise ValueError("responses 不能为空")
        self.responses = list(responses)
        self.name = name
        self.calls = 0

    def complete(self, prompt: str, cwd: Path, timeout: int = 300) -> str:
        r = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return r


def _kill_tree(p: subprocess.Popen) -> bool:
    """连孙进程一起杀。

    agent 会自己 spawn 子进程（跑测试、装依赖）。只 kill 父进程的话，
    孙进程会活下来继续占着 stdout 管道，于是 communicate() 永远等不到 EOF——
    表现出来就是"超时了但任务还在跑"。
    """
    import os
    import signal

    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0 and p.poll() is None:
                p.kill()
                return False
            return result.returncode == 0
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            return True
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
        return False


def _free_port() -> int:
    """要一个当前空闲的本地端口。WorkBuddy CLI 的默认端口被桌面端占着。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def parse_json_events(text: str) -> tuple[str, dict]:
    """从 `codex exec --json` 的 JSONL 里取最终消息与 token 用量。

    为什么不用 "tokens used N" 那一行：它只有总数，分不清输入/输出/缓存。
    而缓存命中恰恰是最该看的——同一个任务重复跑，输入里绝大部分是命中的系统提示。

    返回 (消息, usage)。消息取**最后一条** agent_message：中间那些是过程更新，
    最后一条才是它对本次任务的结论。解析不出来就返回空，让调用方退回原始文本，
    不能因为解析失败把 worker 的产出弄丢。
    """
    import json

    message = ""
    usage: dict = {}
    turns = 0
    total = {"input_tokens": 0, "cached_input_tokens": 0,
             "output_tokens": 0, "reasoning_output_tokens": 0}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                message = item["text"]
        elif kind == "turn.completed":
            turns += 1
            u = ev.get("usage") or {}
            for k in total:
                total[k] += int(u.get(k) or 0)
            usage = {k: v for k, v in total.items() if v}
    if turns:
        # 轮次要单独记：每个 turn 都要重发完整上下文，
        # 只报输入合计看不出"是 prompt 大，还是来回次数多"。
        usage["turns"] = turns
    return message, usage


class CLIProvider(Provider):
    """调用任意命令行 agent。

    argv 是一个 JSON 数组模板，例如：
        ["codex","exec","--sandbox","read-only","-C","{cwd}",
         "--output-last-message","{out_file}","{prompt}"]

    用数组而不是 shell 字符串，是为了避免 prompt 里的引号/分号被 shell 解释。
    可用占位符：
        {prompt}       prompt 全文（作为单个 argv 元素传入，不会被拆词）
        {prompt_file}  prompt 的临时文件路径（适合只吃 stdin 的 CLI）
        {cwd}          目标仓库
        {out_file}     "最终回复写到哪"。很多 CLI 支持把最后一条消息单独落盘，
                       这比从 stdout 里刨 JSON 可靠得多——stdout 常混着日志和警告。
    """

    name = "cli"

    def __init__(self, argv: list[str], name: str = "cli", model: str = ""):
        if not argv:
            raise ValueError("argv 不能为空")
        self.argv = argv
        self.name = name
        # 模型名必须能查：只看 name 看不出"codex"壳里跑的是哪个模型，
        # 而"到底谁干的"这个问题，事后必须有据可查。
        self.model = model or ""
        # 成本核算用。两条来源：
        #   - 纯文本输出末尾的 "tokens used 29,742"（只有总数）
        #   - --json 事件里的 turn.completed.usage（分输入/缓存/输出）
        # 后者优先：只记总数回答不了"钱花在输入还是输出上"。
        self.last_tokens: int | None = None
        self.last_usage: dict = {}

    @staticmethod
    def parse_tokens(text: str) -> int | None:
        import re

        m = re.search(r"tokens\s+used\s*[:：]?\s*([\d,]+)", text or "", re.I)
        return int(m.group(1).replace(",", "")) if m else None

    def complete(self, prompt: str, cwd: Path, timeout: int = 300) -> str:
        return self._complete(prompt, cwd, timeout)

    def complete_controlled(self, prompt: str, cwd: Path, timeout: int, control) -> str:
        return self._complete(prompt, cwd, timeout, control)

    def _complete(self, prompt: str, cwd: Path, timeout: int, control=None) -> str:
        self.last_tokens = None
        self.last_usage = {}
        tmpdir = Path(tempfile.mkdtemp(prefix="orch-"))
        prompt_file = tmpdir / "prompt.txt"
        out_file = tmpdir / "last_message.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        # Windows 的命令行有硬上限（cmd.exe 8191、CreateProcess 32767），而 planning 的提示词
        # 动辄两三万字符（规则 + 需求 + 文件清单）。超限时两条路都是坏的：
        #   * 走 .exe：CreateProcess 直接失败（WinError 206）；
        #   * 走 .cmd 包装：被 cmd.exe 截断，codex 收到空 prompt，转而读 stdin（DEVNULL=EOF），退出 1。
        # 所以长的 prompt 一律改走 stdin（`codex exec -`），argv 里只留一个 "-"。
        big_prompt = len(prompt) > _PROMPT_ARGV_LIMIT and "{prompt}" in self.argv
        argv = [
            "-" if (big_prompt and a == "{prompt}") else
            a.replace("{prompt_file}", str(prompt_file))
            .replace("{out_file}", str(out_file))
            .replace("{prompt}", prompt)
            .replace("{cwd}", str(cwd))
            # 每次调用现取端口：并发跑两个 cbc 时固定端口会 EADDRINUSE
            .replace("{port}", str(_free_port()))
            for a in self.argv
        ]
        try:
            import os

            p = subprocess.Popen(
                argv, cwd=str(cwd),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if big_prompt else subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        except OSError as e:
            prompt_file.unlink(missing_ok=True)
            if getattr(e, "winerror", None) == 206:
                raise ProviderError(
                    f"命令行太长（prompt {len(prompt):,} 字符），Windows 拒绝启动进程。"
                    f"这条本应改走 stdin；请把这句话连同 orch 版本一起报告。") from e
            raise ProviderError(
                f"找不到可执行文件：{argv[0]}。确认它在 PATH 上，或用绝对路径 / 设 CODEX_BIN。"
            ) from e
        if big_prompt:
            # 立刻写完并关掉：control.wait_process 用 communicate() 收尾，stdin 留着会互相等
            try:
                p.stdin.write(prompt.encode("utf-8"))
                p.stdin.flush()
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            finally:
                p.stdin = None

        try:
            stdout_b, stderr_b = control.wait_process(p, timeout) if control else p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if not control:
                _kill_tree(p)
            try:
                p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            raise ProviderError(f"{argv[0]} 超时（{timeout}s），已请求终止进程树；未确认停止时禁止恢复")
        finally:
            prompt_file.unlink(missing_ok=True)

        stdout, stderr = decode_bytes(stdout_b), decode_bytes(stderr_b)
        self.last_tokens = self.parse_tokens((stdout or "") + "\n" + (stderr or ""))
        if p.returncode != 0:
            err = (stderr or "").strip()
            out = (stdout or "").strip()
            # 这条提示只在"没拿到 prompt、去读 stdin"时出现。出现即说明 prompt 没送达，
            # 是命令行传参的问题，不是模型的问题——必须说清楚，否则会被当成模型挂了。
            hint = ""
            if "Reading additional input from stdin" in err and not big_prompt:
                hint = ("\n  提示：codex 没有收到 prompt 参数，正在读 stdin。"
                        f"本次 prompt {len(prompt):,} 字符，接近或超过 Windows 命令行上限。")
            raise ProviderError(
                f"{argv[0]} 调用失败（退出码 {p.returncode}）：{err[-400:]}"
                + (f"\n  stdout 尾部：{out[-300:]}" if out else "")
                + hint)

        # --json 时 stdout 是事件流，不是人话。先解析；解析不出再退回原始文本，
        # 宁可多留一坨日志，也不能把 worker 的结论丢掉。
        event_message, usage = ("", {})
        if "--json" in self.argv:
            event_message, usage = parse_json_events(stdout or "")
            self.last_usage = usage
            if usage:
                self.last_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

        # 优先读 --output-last-message 落的文件：干净，无 stderr 混入
        text = ""
        if out_file.exists():
            text = decode_bytes(out_file.read_bytes()).strip()
        if not text:
            text = event_message.strip()
        if not text:
            text = (stdout or "").strip()
        if not text:
            tail = (stderr or "")[-400:]
            raise ProviderError(f"{argv[0]} 没产出内容（退出码 {p.returncode}）：{tail}")
        return text


def resolve_codex(exe: str | None = None) -> str:
    """找 codex 可执行文件。

    不能信任硬编码路径：Codex 桌面端会自动更新，bin/ 下的版本目录名会变
    （a159911... → 14b838c...），写死的路径第二天就失效。
    所以：先看给定的，失效就按时间挑 bin/*/codex.exe 里最新的。
    """
    import os
    from pathlib import Path

    cand = exe or os.environ.get("CODEX_BIN")
    if cand and Path(cand).exists():
        return cand

    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    if base.exists():
        found = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        if found:
            return str(found[0])

    return shutil.which("codex") or "codex"


def codex_argv(exe: str | None = None, sandbox: str = "read-only") -> list[str]:
    """针对 codex exec 的默认模板。路径可用 CODEX_BIN 覆盖。

    Planner 用 read-only（它只该读）；worker 用 workspace-write（它要改文件）。

    额外的 -c 覆盖从环境变量 CODEX_CONFIG 读，分号分隔，例如：
        CODEX_CONFIG='windows.sandbox="mxc"'
    这样换后端不用改代码，也方便做 A/B。
    """
    import os
    import shutil

    binary = resolve_codex(exe)
    if sandbox == "bypass":
        # 只用于排障：绕过沙箱后隔离就只剩 worktree + 事后越界检查。
        box = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        box = ["--sandbox", sandbox]

    extra: list[str] = []
    for item in filter(None, (s.strip() for s in os.environ.get("CODEX_CONFIG", "").split(";"))):
        extra += ["-c", item]

    return [
        binary,
        "exec",
        "--json",                  # 事件流：想拿 token 用量只能走它
        "--skip-git-repo-check",   # 目标仓库可能不是 git repo
        "--ephemeral",             # 不落会话历史：并发实例不共享状态，跑批可复现
        *extra,
        *box,
        "-C", "{cwd}",
        "--output-last-message", "{out_file}",
        "{prompt}",
    ]


# --------------------------------------------------------------------------
# 一轮 run 的规划
# --------------------------------------------------------------------------

@dataclass
class Attempt:
    n: int
    ok: bool
    raw: str
    codes: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    parse_error: str | None = None


@dataclass
class PlanResult:
    status: str  # "ok" | "fallback"
    attempts: list[Attempt] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    layers: list[list[str]] = field(default_factory=list)
    spec_md: str = ""
    run_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)   # 不阻断，但要说出来

    @property
    def n_attempts(self) -> int:
        return len(self.attempts)


def render_violations(vs: list[Violation]) -> str:
    lines = []
    for v in vs:
        who = f"[{v.task_id}] " if v.task_id else ""
        lines.append(f"- {v.code} {who}{v.message}")
        if v.hint:
            lines.append(f"  （改法：{v.hint}）")
    return "\n".join(lines)


def _read_intent(run_dir: Path) -> tuple[str, dict]:
    md = (run_dir / "intent.md").read_text(encoding="utf-8")
    body = md.split("---\n", 2)[-1].strip() if md.startswith("---") else md.strip()
    meta: dict = {}
    jf = run_dir / "intent.json"
    if jf.exists():
        try:
            meta = json.loads(jf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    return body, meta


def plan_run(
    run_dir: str | Path,
    provider: Provider,
    max_attempts: int = 3,
    timeout: int = 300,
    operation_id: str | None = None,
    feedback: str = "",
) -> PlanResult:
    run_dir = Path(run_dir)
    tracer = Tracer(run_dir / "trace.jsonl", operation_id=operation_id)
    tracer.session = 1 + sum(1 for e in tracer.events() if e.get("event") == "plan.start")

    intent_text, meta = _read_intent(run_dir)
    repo = Path(meta.get("repo", "."))
    files = repo_snapshot(repo) if repo.exists() else []
    budget = meta.get("budget_usd")

    result = PlanResult(status="fallback", run_dir=run_dir)
    archive = run_dir / "planning-history" / uuid.uuid4().hex
    archive.mkdir(parents=True)
    tracer.emit("plan.start", provider=provider.name, max_attempts=max_attempts,
                repo=str(repo), n_repo_files=len(files))
    # 新规划失败时，旧成功计划不能继续冒充当前计划；保留历史副本。
    for name in ("tasks.json", "SPEC.md"):
        previous = run_dir / name
        if previous.exists():
            previous.replace(run_dir / f"{previous.stem}.previous-{tracer.operation_id}{previous.suffix}")

    violations_text = feedback
    stop_reason = "尝试次数耗尽"
    for n in range(1, max_attempts + 1):
        prompt = build_prompt(intent_text, violations_text, sorted(files))
        (archive / f"attempt{n}.prompt.txt").write_text(prompt, encoding="utf-8")
        try:
            raw = provider.complete(prompt, repo, timeout=timeout)
        except ProviderError as e:
            # 调用基础设施失败时停止规划，不声称已执行其他流程。
            tracer.emit("plan.provider_error", attempt=n, error=str(e))
            result.attempts.append(Attempt(n=n, ok=False, raw="", parse_error=str(e)))
            stop_reason = f"模型调用失败：{e}"
            (archive / f"attempt{n}.error.txt").write_text(str(e), encoding="utf-8")
            break

        (run_dir / f"tasks.attempt{n}.raw.txt").write_text(raw, encoding="utf-8")
        (archive / f"attempt{n}.raw.txt").write_text(raw, encoding="utf-8")

        try:
            doc = extract_json(raw)
        except PlanParseError as e:
            v = Violation("E000", str(e), hint="只输出一个 JSON 代码块，不要解释文字")
            result.attempts.append(Attempt(n=n, ok=False, raw=raw,
                                           codes=["E000"], violations=[v]))
            tracer.emit("plan.attempt", attempt=n, ok=False, codes=["E000"])
            violations_text = render_violations([v])
            continue

        spec_md = doc.pop("spec_md", "") if isinstance(doc, dict) else ""
        if not isinstance(doc, dict) or "tasks" not in doc:
            doc = {"tasks": doc if isinstance(doc, list) else []}

        parsed, parse_v = parse_document(doc)
        # 读-写依赖缺失是**可以推导出来的**，交给代码补，不用模型重试
        parsed, repairs = repair_dependencies(parsed, files)
        if repairs:
            tracer.emit("plan.repaired", attempt=n, repairs=[str(r) for r in repairs])

        mode = doc.get("mode") if isinstance(doc, dict) else None
        if mode is not None and not isinstance(mode, str):
            parse_v.append(Violation("E021", "mode 必须是字符串", hint="auto / single / parallel / chain"))
            mode = None
        rep: Report = validate_tasks(parsed, parse_v, files, mode=mode)
        # 单任务预算不能超过本次 run 的总预算（intake 时定的）
        if rep.ok and budget:
            for t in rep.tasks:
                if t.budget > budget:
                    rep.violations.append(
                        Violation("E017", f"budget {t.budget} 超过本次 run 上限 {budget}", t.id)
                    )
        if rep.ok:
            import dataclasses

            payload = {"tasks": [dataclasses.asdict(t) for t in rep.tasks]}
            if mode:
                payload["mode"] = mode
            (run_dir / "tasks.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if not spec_md.strip():
                spec_md = (
                    f"# {intent_text[:40]}\n\n## 要做\n{intent_text}\n\n"
                    "## 备注\nSPEC 由 Planner 省略，此处为自动生成的最小版本。\n"
                )
            (run_dir / "SPEC.md").write_text(spec_md, encoding="utf-8")

            result.status = "ok"
            result.tasks = rep.tasks
            result.layers = rep.layers
            result.spec_md = spec_md
            result.warnings = [f"{w.code} {w.message}" for w in rep.warnings]
            result.attempts.append(Attempt(n=n, ok=True, raw=raw))
            tracer.emit("plan.accepted", attempt=n, n_tasks=len(rep.tasks),
                        layers=rep.layers, max_width=rep.max_width)
            return result

        result.attempts.append(
            Attempt(n=n, ok=False, raw=raw, codes=rep.codes(), violations=rep.violations)
        )
        tracer.emit("plan.rejected", attempt=n, codes=rep.codes(),
                    violations=[v.code for v in rep.violations])
        violations_text = render_violations(rep.violations)

    (run_dir / "plan.failed.json").write_text(
        json.dumps(
            {
                "status": "fallback",
                "reason": f"规划未通过；实际尝试 {result.n_attempts} 次；{stop_reason}；尚未执行",
                "fallback_executed": False,
                "execution_started": False,
                "operation_id": tracer.operation_id,
                "attempts": [
                    {"n": a.n, "codes": a.codes, "parse_error": a.parse_error}
                    for a in result.attempts
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tracer.emit("plan.fallback", attempts=len(result.attempts), reason=stop_reason,
                fallback_executed=False, execution_started=False)
    return result


def _write_plan_failed(run_dir: Path, tracer: Tracer, result: PlanResult, reason: str) -> None:
    """失败证据。宁可多写一份，也别让"没有 tasks.json"变成沉默失败。"""
    (run_dir / "plan.failed.json").write_text(
        json.dumps(
            {
                "status": "rejected",
                "reason": f"{reason}；尚未执行",
                "source": "external",
                "fallback_executed": False,
                "execution_started": False,
                "operation_id": tracer.operation_id,
                "attempts": [
                    {"n": a.n, "codes": a.codes, "parse_error": a.parse_error,
                     "violations": [{"code": v.code, "message": v.message, "task": v.task_id}
                                    for v in a.violations]}
                    for a in result.attempts
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def plan_from_file(
    run_dir: str | Path,
    plan_path: str | Path,
    operation_id: str | None = None,
) -> PlanResult:
    """外部计划的入口：只校验、只落盘，不调用任何模型。

    用在「Codex 自己当 planner」的场景。关键点是它和 plan_run 走**同一条校验路径**：
    解析 → 补依赖 → 23 条规则 → 落盘。规则不因计划来源而变，
    「修补不得放宽标准」才有立足点——外部计划不能绕开检查。
    """
    run_dir = Path(run_dir)
    plan_path = Path(plan_path)
    tracer = Tracer(run_dir / "trace.jsonl", operation_id=operation_id)
    tracer.session = 1 + sum(1 for e in tracer.events() if e.get("event") == "plan.start")

    intent_text, meta = _read_intent(run_dir)
    repo = Path(meta.get("repo", "."))
    files = repo_snapshot(repo) if repo.exists() else []
    budget = meta.get("budget_usd")

    result = PlanResult(status="rejected", run_dir=run_dir)
    archive = run_dir / "planning-history" / f"external-{uuid.uuid4().hex}"
    archive.mkdir(parents=True)

    if not plan_path.exists():
        v = Violation("E000", f"外部计划不存在：{plan_path}",
                      hint="确认 --from 指向一个 .json 文件")
        result.attempts.append(Attempt(n=1, ok=False, raw="", codes=["E000"], violations=[v]))
        _write_plan_failed(run_dir, tracer, result, "外部计划不存在")
        return result

    raw = plan_path.read_text(encoding="utf-8")
    (archive / "external.plan.json").write_text(raw, encoding="utf-8")
    tracer.emit("plan.start", provider="external", source=str(plan_path),
                repo=str(repo), n_repo_files=len(files))

    # 旧计划必须归档：否则上一轮的成功计划会冒充这一轮的结果
    for name in ("tasks.json", "SPEC.md"):
        previous = run_dir / name
        if previous.exists():
            previous.replace(
                run_dir / f"{previous.stem}.previous-{tracer.operation_id}{previous.suffix}"
            )

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        v = Violation("E000", f"不是合法 JSON：{e}",
                      hint='顶层形如 {"tasks": [...]}，或直接给数组')
        result.attempts.append(Attempt(n=1, ok=False, raw=raw, codes=["E000"], violations=[v]))
        tracer.emit("plan.rejected", attempt=1, codes=["E000"], violations=["E000"])
        _write_plan_failed(run_dir, tracer, result, "外部计划不是合法 JSON")
        return result

    spec_md = doc.pop("spec_md", "") if isinstance(doc, dict) else ""
    if not isinstance(doc, dict) or "tasks" not in doc:
        doc = {"tasks": doc if isinstance(doc, list) else []}

    parsed, parse_v = parse_document(doc)
    parsed, repairs = repair_dependencies(parsed, files)
    if repairs:
        tracer.emit("plan.repaired", attempt=1, repairs=[str(r) for r in repairs])

    mode = doc.get("mode") if isinstance(doc, dict) else None
    if mode is not None and not isinstance(mode, str):
        parse_v.append(Violation("E021", "mode 必须是字符串", hint="auto / single / parallel / chain"))
        mode = None
    rep: Report = validate_tasks(parsed, parse_v, files, mode=mode)
    if rep.ok and budget:
        for t in rep.tasks:
            if t.budget > budget:
                rep.violations.append(
                    Violation("E017", f"budget {t.budget} 超过本次 run 上限 {budget}", t.id)
                )

    if rep.ok:
        import dataclasses

        payload = {"tasks": [dataclasses.asdict(t) for t in rep.tasks]}
        if mode:
            payload["mode"] = mode
        (run_dir / "tasks.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if not spec_md.strip():
            spec_md = (f"# {intent_text[:40]}\n\n## 要做\n{intent_text}\n\n"
                       "## 备注\nSPEC 由外部计划省略，此处为自动生成的最小版本。\n")
        (run_dir / "SPEC.md").write_text(spec_md, encoding="utf-8")

        result.status = "ok"
        result.tasks = rep.tasks
        result.layers = rep.layers
        result.spec_md = spec_md
        result.warnings = [f"{w.code} {w.message}" for w in rep.warnings]
        result.attempts.append(Attempt(n=1, ok=True, raw=raw))
        tracer.emit("plan.accepted", attempt=1, n_tasks=len(rep.tasks),
                    layers=rep.layers, max_width=rep.max_width, provider="external")
        return result

    result.attempts.append(
        Attempt(n=1, ok=False, raw=raw, codes=rep.codes(), violations=rep.violations)
    )
    tracer.emit("plan.rejected", attempt=1, codes=rep.codes(),
                violations=[v.code for v in rep.violations])
    _write_plan_failed(run_dir, tracer, result, "外部计划未通过校验")
    # 即使被拒也把解析结果带回去：修补时要拿它当"强度基准"，
    # 否则没法判断修补是"改对了"还是"改松了"。
    result.tasks = rep.tasks
    return result
