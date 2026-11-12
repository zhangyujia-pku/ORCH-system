"""运行心跳。

文件锁只能证明"进程还活着"，证明不了"它在干活"——进程可能卡在某个调用里、
被系统挂起、或者占了锁却不推进。这两种情况处置完全不同：
  - 锁可获取        → 进程死了，自动回收
  - 锁被持有但没心跳 → 疑似挂起，要人工介入才动

所以心跳必须独立于业务线程：不能放在 worker 调用里，否则卡住时心跳也停，
那就永远"新鲜"，等于没有。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HEARTBEAT_NAME = "run.heartbeat.json"
DEFAULT_INTERVAL = 15.0
# 30 分钟，且不低于单任务超时的两倍——否则一次大上下文预填就会被误判成挂起
DEFAULT_STALE_AFTER = 30 * 60


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class Heartbeat:
    """上下文管理器：进入开始写，退出停写。"""

    def __init__(self, run_dir: str | Path, operation_id: str = "",
                 interval: float = DEFAULT_INTERVAL):
        self.path = Path(run_dir) / HEARTBEAT_NAME
        self.operation_id = operation_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.progress: dict = {}

    def _write(self) -> None:
        _atomic_write(self.path, {
            "ts": time.time(),
            "ts_iso": _now_iso(),
            "pid": os.getpid(),
            "operation_id": self.operation_id,
            "progress": self.progress,
        })

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._write()
            except OSError:
                pass          # 心跳写失败不该拖垮执行

    def __enter__(self) -> "Heartbeat":
        try:
            self._write()
        except OSError:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="orch-heartbeat")
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            self.progress["finished"] = True
            self._write()
        except OSError:
            pass

    def tick(self, **kw) -> None:
        """更新进度并立刻落盘（任务完成这类节点值得马上写）。"""
        self.progress.update(kw)
        try:
            self._write()
        except OSError:
            pass


def read(run_dir: str | Path) -> dict:
    p = Path(run_dir) / HEARTBEAT_NAME
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def age(run_dir: str | Path) -> float | None:
    """距离上次心跳多少秒。没有心跳文件返回 None。"""
    data = read(run_dir)
    ts = data.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    return max(0.0, time.time() - ts)
