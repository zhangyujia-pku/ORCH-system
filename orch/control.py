"""有限资源、取消、进程监视与执行互斥。仅使用标准库。"""

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from .planner import ProviderError


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    # Windows 上 os.replace 会在目标被另一只手短暂占用时抛 WinError 5（杀软扫描、
    # 上一次写入的句柄还没释放、同步盘在对比）。这份文件是**运行状态**，不是账本：
    # 宁可退化成非原子写，也不要因为一次改名失败把整次运行判死。
    for attempt in range(6):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                break
            time.sleep(0.02 * (attempt + 1))
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class ExecutionPolicy:
    max_attempts: int = 1
    switch_after: int = 1
    switch_workers: bool = False
    max_calls: int = 100
    max_commands: int = 1000
    total_timeout: int = 3600
    max_replans: int = 0


def load_execution(path: Path) -> ExecutionPolicy:
    if not path.exists():
        return ExecutionPolicy()
    doc = json.loads(path.read_text(encoding="utf-8"))
    fields = asdict(ExecutionPolicy())
    if not isinstance(doc, dict) or set(doc) - set(fields):
        raise ValueError("execution 配置含未知字段")
    fields.update(doc)
    for key, value in fields.items():
        if key == "switch_workers":
            if type(value) is not bool:
                raise ValueError("switch_workers 必须为布尔值")
        elif type(value) is not int or value < (0 if key == "max_replans" else 1):
            raise ValueError(f"{key} 必须为有效整数上限")
    return ExecutionPolicy(**fields)


class RunStopped(ProviderError):
    pass


class RunLock:
    """操作系统文件锁：进程退出后自动释放，不依赖猜测 PID 是否复用。"""
    def __init__(self, directory: Path, name: str = "run.lock"):
        self.path = directory / name

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ValueError("同一运行已有执行者，不能重复启动")
        return self

    def __exit__(self, *args):
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


class RunControl:
    def __init__(self, directory: Path, policy: ExecutionPolicy, resume: bool = False):
        self.directory, self.policy = directory, policy
        self.path = directory / "runtime.json"
        self.lock = threading.RLock()
        self.reason, self.state = "", ""
        if resume:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data["policy"] != asdict(policy):
                raise ValueError("恢复时资源配置已改变；请建立新运行")
            if self.data.get("unconfirmed_stop"):
                raise ValueError("上次无法确认子进程树已停止；需人工检查后建立新运行")
            if self.data.get("active"):
                from .dispatch import pid_alive
                if any(pid_alive(int(pid)) for pid in self.data["active"]):
                    raise ValueError("上次调用进程仍存在或 PID 已复用；需先检查，不能直接恢复")
                self.data["active"] = {}
            request = directory / "cancel.request.json"
            if request.exists():
                request.replace(directory / f"cancel.previous-{uuid.uuid4().hex}.json")
        else:
            if self.path.exists():
                previous = json.loads(self.path.read_text(encoding="utf-8"))
                from .dispatch import pid_alive
                if previous.get("unconfirmed_stop") or any(pid_alive(int(pid)) for pid in previous.get("active", {})):
                    raise ValueError("旧调用进程未确认停止；必须先检查，不能用新执行覆盖现场")
            self.data = {"execution_id": uuid.uuid4().hex, "policy": asdict(policy),
                         "deadline": time.time() + policy.total_timeout,
                         "calls": 0, "commands": 0, "active": {}}
        self.save()

    def save(self):
        with self.lock:
            atomic_json(self.path, self.data)

    def stop(self, state: str, reason: str):
        self.state, self.reason = state, reason
        raise RunStopped(reason)

    def check(self) -> None:
        with self.lock:
            if self.state:
                raise RunStopped(self.reason)
            request = self.directory / "cancel.request.json"
            if request.exists():
                data = json.loads(request.read_text(encoding="utf-8"))
                if data.get("execution_id", "") in ("", self.data["execution_id"]):
                    self.stop("cancelled", "用户取消了本次运行")
            if self.data.get("unconfirmed_stop"):
                self.stop("recovery_required", "无法确认上次子进程树已停止，禁止继续发车")
            if time.time() >= self.data["deadline"]:
                self.stop("resource_limit", "运行总时限已耗尽")

    def timeout(self, timeout: int) -> int:
        self.check()
        return max(1, min(timeout, math.ceil(self.data["deadline"] - time.time())))

    def reserve(self, kind: str):
        with self.lock:
            self.check()
            limit = self.policy.max_calls if kind == "calls" else self.policy.max_commands
            if self.data[kind] >= limit:
                self.stop("resource_limit", f"{kind} 次数上限已耗尽")
            self.data[kind] += 1
            self.save()

    def replan(self) -> bool:
        with self.lock:
            self.check()
            used = self.data.get("replans", 0)
            if used >= self.policy.max_replans:
                return False
            self.data["replans"] = used + 1
            self.save()
            return True

    def wait_process(self, process, timeout: int):
        import subprocess
        from .planner import _kill_tree
        try:
            with self.lock:
                self.data["active"][str(process.pid)] = {"started_at": time.time()}
                self.save()
            # 进程已经创建：即使恰在此刻取消，也必须进入终止与清理分支。
            deadline = time.monotonic() + self.timeout(timeout)
            while True:
                self.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("controlled process", timeout)
                try:
                    return process.communicate(timeout=min(0.25, remaining))
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            stopped = _kill_tree(process)
            if stopped is False:
                with self.lock:
                    self.data["unconfirmed_stop"] = True
                    self.save()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            with self.lock:
                # 只有确认进程结束才清除记录，不能把仍在运行的进程当成已停止。
                if process.poll() is not None:
                    self.data["active"].pop(str(process.pid), None)
                self.save()


class ControlledProvider:
    def __init__(self, provider, control: RunControl):
        self.provider, self.control = provider, control
        self.name = provider.name

    def __getattr__(self, item):
        # 包一层之后，model / last_usage 这些"事实"还在内层 provider 上。
        # 不透传的话，worker 记下来的用量永远是空——而它看起来"成功"。
        return getattr(self.provider, item)

    def complete(self, prompt, cwd, timeout=300):
        self.control.reserve("calls")
        timeout = self.control.timeout(timeout)
        method = getattr(self.provider, "complete_controlled", None)
        result = method(prompt, cwd, timeout, self.control) if method else self.provider.complete(prompt, cwd, timeout)
        self.control.check()
        return result
