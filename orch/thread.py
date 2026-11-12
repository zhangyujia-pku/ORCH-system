"""定位「当前这个对话窗口」属于哪个 Codex thread。

为什么需要这一层
----------------
产物要送回**发起它的那个窗口**，所以必须知道 thread_id。实测（2026-11-07，
codex-cli 0.167.0-alpha.3.2 / 桌面端）：

  - `$CODEX_THREAD_ID` 在桌面端执行命令时是**空**的。它只在 exec-server 的
    沙箱进程里注入（二进制字符串表里紧邻 `exec-server/src/process_sandbox.rs`），
    桌面端的命令走的是 `pwsh -Command`，不带这个变量。环境变量这条路不通。
  - 但会话记录 `~/.codex/sessions/**/*.jsonl` 里：
      * 每一行事件都带 `thread_id`
      * `CommandExecution` 条目记录了 Codex 自己 spawn 的 shell 的 `process_id`
    于是「我是谁」可以被反推出来：拿自己的祖先 PID 链，去最近的会话文件里
    找哪条命令的 `process_id` 命中。

三层，从硬到软
--------------
  C1  pid     祖先 PID 链 × `CommandExecution.process_id`   确定性最高，零配合
  C2  nonce   命令文本里的随机串 × `custom_tool_call.input`   skill 主动配合时最稳
  C3  cwd     同一仓库 + 最近被追加写                        兜底，可能歧义

C1 的时序问题：`CommandExecution` 是命令**结束后**才写盘的（item_completed），
而 orch 在命令**执行中**就要开始工作。所以这里提供 `wait_for()`，在后台轮询
几秒即可命中——dispatch 本来就是 fire-and-forget，等 10 秒不影响任何事。

这一层只读文件，不改任何会话记录。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 只看最近这么多个会话文件。会话记录是追加写的，thread_id 出现在最后几行，
# 扫尾部 400 行足够；再往前翻只是在为已经过期的窗口做无用功。
_DEFAULT_LIMIT = 40
_TAIL_LINES = 400


@dataclass
class Match:
    """一次成功的定位。method 说明是靠哪一层认出来的。"""

    thread_id: str
    method: str                 # explicit | pid | nonce | cwd
    confidence: str = "high"    # high | medium | low
    session_file: str = ""
    cwd: str = ""
    originator: str = ""

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "method": self.method,
            "confidence": self.confidence,
            "session_file": self.session_file,
            "cwd": self.cwd,
            "originator": self.originator,
        }


def session_root(root: str | Path | None = None) -> Path:
    """Codex 的 home。尊重 CODEX_HOME，否则 ~/.codex。"""
    if root:
        return Path(root)
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def iter_sessions(root: str | Path | None = None, limit: int = _DEFAULT_LIMIT) -> list[Path]:
    """最近的会话文件，按 mtime 倒序。"""
    base = session_root(root) / "sessions"
    if not base.exists():
        return []
    files = [p for p in base.glob("**/*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def read_meta(path: str | Path) -> dict:
    """会话文件首行的 session_meta：thread id、cwd、来源。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            line = f.readline()
        doc = json.loads(line)
    except Exception:
        return {}
    if doc.get("type") != "session_meta":
        return {}
    pl = doc.get("payload") or {}
    return {
        "thread_id": pl.get("session_id") or pl.get("id") or "",
        "cwd": pl.get("cwd") or "",
        "originator": pl.get("originator") or "",
        "source": pl.get("source") or "",
        "timestamp": doc.get("timestamp") or pl.get("timestamp") or "",
    }


def _tail_lines(path: str | Path, n: int = _TAIL_LINES) -> list[str]:
    """读文件尾部 n 行。会话文件不大（百 KB 级），整读再切片足够快。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return text.splitlines()[-n:]


def scan(
    path: str | Path,
    pids: set[int] | None = None,
    nonce: str | None = None,
    fingerprint: str | None = None,
) -> str | None:
    """在单个会话文件里找证据。命中就返回方法名。"""
    for line in _tail_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        pl = doc.get("payload") or {}
        ptype = pl.get("type")

        if ptype == "item_completed":
            item = pl.get("item") or {}
            if item.get("type") == "CommandExecution":
                if pids:
                    raw = item.get("process_id")
                    try:
                        pid = int(raw)
                    except (TypeError, ValueError):
                        pid = None
                    if pid is not None and pid in pids:
                        return "pid"
                if fingerprint:
                    cmd = " ".join(item.get("command") or [])
                    if fingerprint in strip_noise(cmd):
                        return "fingerprint"

        if ptype in ("custom_tool_call", "function_call", "local_shell_call"):
            blob = json.dumps(pl, ensure_ascii=False)
            if nonce and nonce in blob:
                return "nonce"
            if fingerprint and fingerprint in strip_noise(blob):
                return "fingerprint"

    return None


def ancestor_pids(max_depth: int = 12) -> set[int]:
    """自己的祖先 PID 链（含自身）。

    只为了「在会话记录里认出自己」——不做任何进程操作，也不杀进程。
    """
    chain: list[int] = [os.getpid()]
    if os.name == "nt":
        parents = _win_parent_map()
        cur = os.getpid()
        for _ in range(max_depth):
            nxt = parents.get(cur)
            if not nxt or nxt == cur or nxt in chain:
                break
            chain.append(nxt)
            cur = nxt
        return set(chain)

    # POSIX：/proc 足够
    cur = os.getpid()
    for _ in range(max_depth):
        try:
            stat = Path(f"/proc/{cur}/stat").read_text()
            # 形如 "1234 (name with spaces) S 5678 ..."，ppid 是右括号后第二列
            after = stat.rsplit(")", 1)[1].split()
            nxt = int(after[1])
        except Exception:
            break
        if nxt <= 0 or nxt in chain:
            break
        chain.append(nxt)
        cur = nxt
    return set(chain)


def _win_parent_map() -> dict[int, int]:
    """pid → ppid。用 Toolhelp 快照，纯 ctypes，不依赖第三方库。"""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID:
        return {}

    out: dict[int, int] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def resolve(
    pids: set[int] | None = None,
    nonce: str | None = None,
    cwd: str | Path | None = None,
    root: str | Path | None = None,
    limit: int = _DEFAULT_LIMIT,
    fingerprint: str | None = None,
) -> Match | None:
    """按 pid → fingerprint → nonce → cwd 找。找不到返回 None，不抛异常。"""
    files = iter_sessions(root, limit)
    if not files:
        return None

    metas = {p: read_meta(p) for p in files}

    if pids or nonce or fingerprint:
        for p in files:
            method = scan(p, pids=pids, nonce=nonce, fingerprint=fingerprint)
            if method:
                meta = metas[p]
                return Match(
                    thread_id=meta.get("thread_id") or _id_from_name(p),
                    method=method,
                    confidence="high",
                    session_file=str(p),
                    cwd=meta.get("cwd", ""),
                    originator=meta.get("originator", ""),
                )

    if cwd:
        target = _norm(cwd)
        for p in files:  # files 已按 mtime 倒序，第一个命中的就是最近写入的那个
            meta = metas[p]
            if _norm(meta.get("cwd", "")) == target:
                return Match(
                    thread_id=meta.get("thread_id") or _id_from_name(p),
                    method="cwd",
                    confidence="medium",
                    session_file=str(p),
                    cwd=meta.get("cwd", ""),
                    originator=meta.get("originator", ""),
                )

    return None


def wait_for(
    pids: set[int] | None = None,
    nonce: str | None = None,
    cwd: str | Path | None = None,
    root: str | Path | None = None,
    timeout: float = 45.0,
    interval: float = 2.0,
    fingerprint: str | None = None,
) -> Match | None:
    """轮询等待。C1 依赖命令结束才落盘的记录，所以这里必须能等。

    默认 45 秒——dispatch 是后台任务，等这点时间不影响 5–15 分钟的主流程。
    """
    if pids is None and nonce is None and cwd is None and fingerprint is None:
        return None

    deadline = time.time() + max(0.0, timeout)
    last: Match | None = None
    while True:
        m = resolve(pids=pids, nonce=nonce, cwd=cwd, root=root, fingerprint=fingerprint)
        if m:
            # cwd 兜底先按住，给精确证据一点机会；最后一轮才认。
            if m.method != "cwd":
                return m
            last = m
        if time.time() >= deadline:
            return last
        time.sleep(interval)


_NOISE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")


def strip_noise(s: str) -> str:
    """剥掉所有标点、空白、引号、反斜杠。

    命令从"用户原话"到"被 pwsh 记下来的命令行"之间会经历转义：
    单引号变双引号、换行变空格、中文引号变普通引号。这些都会让朴素子串
    匹配失败，但**汉字和字母本身不会变**。所以比较前两边都做同样的剥除。
    """
    return _NOISE.sub("", s)


def fingerprint_of(text: str, n: int = 20) -> str:
    """从原话里取一段稳定的指纹，用于在会话记录里认出"这条命令是我发的"。

    这是 pid 之外的兜底，而且比 pid 可靠——实测（2026-11-07 01:50）Codex
    记录的 `process_id` 是它启动的 pwsh，但真正执行命令的是中间一层
    `codex-command-runner`，祖先链到不了 pwsh，pid 这一层在桌面端会失配。
    命令文本则一定在会话记录里。
    """
    return strip_noise(text)[:n]


def _id_from_name(path: str | Path) -> str:
    """rollout-2026-11-07T00-20-24-<uuid>.jsonl → uuid"""
    stem = Path(path).stem
    parts = stem.split("-")
    if len(parts) >= 7:
        return "-".join(parts[-5:])
    return ""


def _norm(p: str | Path) -> str:
    """cwd 比较：统一分隔符与大小写（Windows 不区分）。"""
    if not p:
        return ""
    s = str(p).replace("file:///", "").replace("/", "\\").rstrip("\\")
    try:
        from urllib.parse import unquote
        s = unquote(s)
    except Exception:
        pass
    return s.lower()


def main(argv: list[str] | None = None) -> int:
    """自证脚本：在目标窗口里跑一次，看能不能认出自己。

        python -m orch.thread
        python -m orch.thread --nonce abc123 --cwd <仓库>
    """
    import argparse

    ap = argparse.ArgumentParser(prog="orch.thread", description="定位当前 Codex 窗口的 thread id")
    ap.add_argument("--nonce", default=None, help="命令文本里的随机串（skill 配合时用）")
    ap.add_argument("--cwd", default=None, help="按仓库路径兜底")
    ap.add_argument("--root", default=None, help="Codex home，默认 ~/.codex")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    pids = ancestor_pids()
    m = wait_for(pids=pids, nonce=args.nonce, cwd=args.cwd,
                 root=args.root, timeout=args.timeout)

    if args.json:
        print(json.dumps(
            {"pid_chain": sorted(pids), "match": m.to_dict() if m else None},
            ensure_ascii=False, indent=2))
        return 0 if m else 1

    print(f"pid        {os.getpid()}")
    print(f"祖先链     {sorted(pids)}")
    print(f"会话目录   {session_root(args.root) / 'sessions'}")
    if not m:
        print("\n  没认出来。可能原因：")
        print("    · 这次命令不是 Codex 发起的（在普通终端里跑）")
        print("    · 桌面端还没把本条命令写进会话记录 —— 等几秒重试")
        print("    · 会话目录不在默认位置，用 --root 指定")
        return 1

    print(f"\nthread     {m.thread_id}")
    print(f"依据       {m.method}  ({m.confidence})")
    print(f"cwd        {m.cwd}")
    print(f"来源       {m.originator}")
    print(f"会话文件   {m.session_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
