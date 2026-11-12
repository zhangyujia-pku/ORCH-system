"""
verifier：判据 → 谓词。

两类判据分开处理：
  - 命令型（`pytest ... 退出码 0`）→ 直接跑，退出码是唯一标准，不花 token。
  - 断言型（`README.md 包含 ...`）→ 交给一个**只看 diff 和判据**的模型判，
    并且强制它给证据。看不到 worker 的推理，是这一层的核心设计。

顺序很重要：命令型先跑。一条测试挂了就没必要再花一次模型调用。
"""

from __future__ import annotations

import json
import re
import subprocess
import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .contract import Task, _CMD, _OUT_SPEC
from .planner import PlanParseError, Provider, ProviderError, extract_json
from .worktree import Worktree

_SPLIT = re.compile(r"(退出码|，|。|；|,|;)")


@dataclass
class Check:
    criterion: str
    kind: str  # "command" | "judge" | "skipped"
    ok: bool
    evidence: str = ""
    status: str = ""
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    expected_exit: int | None = None

    def __post_init__(self):
        if not self.status:
            self.status = "passed" if self.ok else "failed"


@dataclass
class Verdict:
    ok: bool
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


_QUOTED_EXE = re.compile(r'^\s*"([^"]+\.(?:exe|bat|cmd|ps1|py))"', re.I)


def is_command_criterion(c: str) -> bool:
    """是不是"跑一下就出结果"的判据。

    除了常见命令名，还要认**带引号的绝对路径**——文档任务的判据常常长这样：
        "C:////...////python.exe" "C:/.../docxcheck.py" 报告.docx --min-chars 200 退出码 0
    只认裸命令名的话，这类判据会被误判成断言型，交给模型去"看"
    ——而模型看不到命令输出，只能判 FAIL，看起来像 worker 做错了。
    """
    cmd = command_of(c)
    if _QUOTED_EXE.match(cmd):
        return True
    return bool(_CMD.match(cmd))


def command_of(criterion: str) -> str:
    """从判据里取出**真正要执行的命令行**。

    Planner 会写出三种形态，都得认：
        python -m pytest -q tests 退出码 0
        命令 `python -m pytest -q tests` 退出码 0
        运行：pytest -q tests，退出码 0

    只认第一种的后果是：后两种被当成"断言型"丢给模型判，
    而模型看不到命令输出，只能判 FAIL —— 看起来像 worker 做错了，
    其实是验收器没跑命令。
    """
    s = criterion.strip()
    m = re.search(r"`([^`]+)`", s)
    if m and re.search(r"退出码|返回码", s):
        return m.group(1).strip()
    s = re.sub(r"^\s*(命令|运行|执行)\s*[:：]?\s*", "", s)
    # 只移除命令末尾的说明，不拆开引号中的逗号、分号或 Python 代码。
    suffix = re.search(r"(?:退出码|返回码)\s*-?\d+\s*[。.]?\s*$", s)
    return s[:suffix.start()].rstrip(" ，。；,;") if suffix else s


def split_command(criterion: str) -> tuple[str, int]:
    """'python -m pytest -q tests 退出码 0' → ('python -m pytest -q tests', 0)"""
    m = re.search(r"(?:退出码|返回码)\s*(-?\d+)\s*[。.]?\s*$", criterion)
    expected = int(m.group(1)) if m else 0
    return command_of(criterion), expected


def parse_output_criterion(criterion: str) -> tuple[str, str, str] | None:
    """识别"命令 + 期望输出"型判据。

    这一类以前没支持，会被当成普通命令型跑：命令确实执行了，
    但只比了退出码，没人看输出 —— `git diff` 多改一个文件也照样 PASS。
    如果不识别成命令型，则整条丢给模型判，模型看不到输出只能 FAIL。
    两种错都很难看出来，所以单独成一类。
    """
    m = _OUT_SPEC.search(criterion)
    if not m:
        return None
    cmd = m.group(1).strip()
    only, verb, expected = m.group(2), m.group(3), m.group(4).strip()
    # 判据常写成「输出等于 `README.md` 且退出码 0」。末尾锚点让正则贪婪吃到结尾，
    # 期望值会变成「README.md` 且」，永远比不上。第一个反引号就是期望值的右边界。
    if "`" in expected:
        # 期望值通常被一对反引号包住；取第一个非空片段，就能同时处理
        # 「`X`」和「`X` 且退出码 0」两种写法。
        parts = [x for x in expected.split("`") if x.strip()]
        if parts:
            expected = parts[0]
    expected = re.sub(r"[，；,;]?\s*(?:退出码|返回码)\s*-?\d+\s*[。.]?\s*$", "", expected).rstrip()
    expected = expected.strip("`\"'「」“” ")
    mode = "only" if only else ("exact" if verb in ("为", "等于", "是") else "contains")
    return cmd, expected, mode


def _norm(s: str) -> str:
    return " ".join(s.split())


def _unescape_output(s: str) -> str:
    """planner 习惯用 JSON 风格转义描述命令输出：`git diff --numstat` 会写成
    `1\t1\tfile`。字面比较永远不等（真输出是制表符），而判据看着完全合理——
    这类假失败最难归因，所以在比较前统一还原一次。
    """
    if "\\" not in s:
        return s
    return s.replace("\\t", "\t").replace("\\r", "\r").replace("\\n", "\n")


def run_output_command(cmd: str, expected: str, mode: str, cwd: Path,
                       timeout: int = 300, expected_exit: int = 0, control=None) -> Check:
    expected = _unescape_output(expected)
    chk = run_command(cmd, cwd, timeout=timeout, expected_exit=expected_exit, control=control)
    if not chk.ok:
        chk.kind = "command-output"
        chk.evidence = f"$ {cmd}\n{chk.evidence}"
        return chk
    out = chk.stdout  # 判输出只用完整 stdout，stderr 独立保存。
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if mode == "exact":
        ok = _norm(out) == _norm(expected)
    elif mode == "only":
        ok = _norm(out) == _norm(expected) or (
            bool(lines) and all(_norm(x) == _norm(expected) for x in lines)
        )
    else:
        ok = _norm(expected) in _norm(out)
    label = {"only": "仅含", "exact": "等于", "contains": "含"}[mode]
    return Check(criterion="", kind="command-output", ok=ok,
                 evidence=f"$ {cmd}\n{out[-600:]}\n→ 期望输出{label}「{expected}」："
                          f"{'满足' if ok else '不满足'}",
                 stdout=chk.stdout, stderr=chk.stderr, returncode=chk.returncode, expected_exit=expected_exit)


def normalize_command(cmd: str) -> str:
    """把裸工具名换成 `python -m 工具名`。

    真实环境里 `pytest` 这个可执行文件经常不在 PATH 上（pip 装到了
    Scripts 目录但没加 PATH），而 `python -m pytest` 总是能跑。
    Planner 写的判据是给人看的，这里是给机器执行的，中间需要这一步翻译。
    """
    import shutil
    import sys

    parts = cmd.split()
    if not parts:
        return cmd
    head = parts[0].lower()
    if head in {"pytest", "ruff", "mypy", "black", "flake8"} and shutil.which(parts[0]) is None:
        return " ".join([f'"{sys.executable}"', "-m", *parts])
    return cmd


def run_command(cmd: str, cwd: Path, timeout: int = 300, expected_exit: int = 0, control=None) -> Check:
    from .control import RunStopped
    if control:
        try:
            control.reserve("commands")
            timeout = control.timeout(timeout)
        except RunStopped as e:
            return Check(cmd, "command", False, str(e), status=control.state)
    cmd = normalize_command(cmd).strip()
    try:
        parts = shlex.split(cmd, posix=os.name != "nt")
        head = parts[0].strip('"') if parts else ""
    except ValueError as e:
        return Check(cmd, "command", False, f"命令格式错误：{e}", status="launch_error", expected_exit=expected_exit)
    builtins = {"echo", "exit", "cd", "set", "dir", "type", "ver"} if os.name == "nt" else {"echo", "exit", "cd", "true", "false", "printf"}
    resolved = shutil.which(head) if head else None
    if not resolved and head and (Path(cwd) / head).is_file():
        resolved = str((Path(cwd) / head).resolve())
    if not head or (head.lower() not in builtins and not resolved):
        return Check(cmd, "command", False, f"命令未启动：找不到可执行文件 {head}",
                     status="launch_error", expected_exit=expected_exit)
    if head.lower() not in builtins:
        resolved = str(Path(resolved).resolve())
        token = re.match(r'''^(?:"[^"]*"|'[^']*'|\S+)''', cmd)
        # 固定工具的实际位置，避免整合产物中的 python.bat 等同名文件劫持检查。
        quoted = f'"{resolved}"' if os.name == "nt" else shlex.quote(resolved)
        cmd = quoted + cmd[token.end():]
    try:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p = subprocess.Popen(
            cmd, cwd=str(cwd), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except OSError as e:
        return Check(cmd, "command", False, f"命令未启动：{e}", status="launch_error", expected_exit=expected_exit)
    try:
        stdout, stderr = control.wait_process(p, timeout) if control else p.communicate(timeout=timeout)
    except RunStopped as e:
        return Check(cmd, "command", False, str(e), status=control.state)
    except subprocess.TimeoutExpired as e:
        from .contract import decode_bytes
        from .planner import _kill_tree
        if not control:
            _kill_tree(p)
        try:
            stdout, stderr = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = decode_bytes(e.stdout), decode_bytes(e.stderr)
        return Check(cmd, "command", False, f"超时（{timeout}s），已请求终止检查进程树", status="timeout",
                     stdout=stdout or "", stderr=stderr or "", expected_exit=expected_exit)
    tail = ((stdout or "") + (stderr or "")).strip()[-600:]
    return Check(criterion=cmd, kind="command", ok=p.returncode == expected_exit,
                 evidence=f"实际退出码 {p.returncode}，预期 {expected_exit}\n{tail}",
                 stdout=stdout or "", stderr=stderr or "", returncode=p.returncode,
                 expected_exit=expected_exit)


JUDGE_PROMPT = """你是验收员。你只能看到三样东西：验收判据、代码 diff、以及（如果有）命令输出。
你不知道实现者是谁、也不知道它的思路，**不要推测意图**。

逐条判定。只有能在 diff 或命令输出里指出**具体证据**时，才判 PASS。
判据含糊、无法从 diff 判断、或者需要运行额外命令才能确认的，一律判 FAIL，并说明缺什么。

只输出一个 JSON 代码块：

```json
{"results": [{"i": 1, "verdict": "PASS", "evidence": "README.md:12 新增 '## 数据文件' 一节"}]}
```

## 验收判据
<<CRITERIA>>

## 命令输出
<<COMMANDS>>

## 改动后的文件内容（节选）
<<FILES>>

## diff
<<DIFF>>
"""


def collect_file_context(
    root: Path, files: list[str], limit_files: int = 3, per_file: int = 4000
) -> str:
    """把改动后文件的**完整内容**摘给验收器。

    只给 diff 会误判：像"这段输出受 verbose 条件控制"这种判据，
    diff 里 `if verbose:` 只是上下文行，看不出块结构，验收器就会判 FAIL。
    给整段代码，它才能真的核对。
    """
    chunks: list[str] = []
    for rel in files[:limit_files]:
        p = Path(root) / rel
        if not p.is_file():
            continue
        try:
            if p.stat().st_size > 200_000:
                continue
            raw = p.read_bytes()
            if b"\x00" in raw[:4096]:
                chunks.append(f"### {rel}\n（二进制文件，已跳过内容）")
                continue
            body = raw.decode("utf-8", "replace")[:per_file]
        except OSError:
            continue
        chunks.append(f"### {rel}\n```\n{body}\n```")
    return "\n\n".join(chunks)


def judge(
    criteria: list[str],
    diff: str,
    command_evidence: str,
    provider: Provider,
    cwd: Path,
    timeout: int = 300,
    files_context: str = "",
) -> list[Check]:
    if not criteria:
        return []
    prompt = (
        JUDGE_PROMPT
        .replace("<<CRITERIA>>", "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1)))
        .replace("<<COMMANDS>>", command_evidence or "（无）")
        .replace("<<FILES>>", files_context or "（无）")
        .replace("<<DIFF>>", diff[:9000])
    )
    try:
        raw = provider.complete(prompt, cwd, timeout=timeout)
        doc = extract_json(raw)
    except (ProviderError, PlanParseError) as e:
        return [Check(criterion=c, kind="judge", ok=False, evidence=f"验收器失败：{e}", status="judge_error") for c in criteria]

    by_index = {}
    results = doc.get("results", [])
    if not isinstance(results, list):
        return [Check(c, "judge", False, "验收器 results 必须为列表", status="judge_error") for c in criteria]
    for r in results:
        if isinstance(r, dict) and isinstance(r.get("i"), int):
            by_index[r["i"]] = r

    out: list[Check] = []
    for i, c in enumerate(criteria, 1):
        r = by_index.get(i)
        if not r:
            out.append(Check(criterion=c, kind="judge", ok=False, evidence="验收器没有给出这一条的结论"))
            continue
        ok = str(r.get("verdict", "")).upper() == "PASS"
        ev = str(r.get("evidence", "")).strip()
        # 没有证据的 PASS 不算 PASS
        out.append(Check(criterion=c, kind="judge", ok=ok and bool(ev), evidence=ev or "无证据"))
    return out


def verify_task(
    task: Task,
    wt: Worktree,
    diff: str,
    provider: Provider | None = None,
    timeout: int = 300,
    files_context: str = "",
    control=None,
) -> Verdict:
    command_checks: list[Check] = []
    pending: list[str] = []
    for c in task.acceptance:
        # 顺序要紧：先认"命令+期望输出"，再认纯退出码型。
        # 前者更具体，先判它才不会让"输出仅含 X"退化成"退出码 0 就算过"。
        spec = parse_output_criterion(c)
        if spec:
            cmd, expected, mode = spec
            chk = run_output_command(cmd, expected, mode, wt.path, timeout=timeout,
                                     expected_exit=split_command(c)[1], control=control)
            chk.criterion = c
            command_checks.append(chk)
        elif is_command_criterion(c):
            cmd, expected = split_command(c)
            chk = run_command(cmd, wt.path, timeout=timeout, expected_exit=expected, control=control)
            chk.criterion = c  # 保留原文，便于回报
            command_checks.append(chk)
        else:
            pending.append(c)

    if any(not c.ok for c in command_checks):
        return Verdict(ok=False, checks=command_checks)

    evidence = "\n\n".join(
        f"$ {c.criterion}\n实际退出码 {c.returncode}，预期 {c.expected_exit}"
        f"\nstdout:\n{c.stdout}\nstderr:\n{c.stderr}" for c in command_checks
    )
    judged = judge(pending, diff, evidence, provider, wt.path, timeout, files_context) if (pending and provider) else [
        Check(criterion=c, kind="skipped", ok=False, evidence="没有配置验收器，断言型判据无法判定", status="unsupported")
        for c in pending
    ]

    checks = command_checks + judged
    return Verdict(ok=all(c.ok for c in checks), checks=checks)
