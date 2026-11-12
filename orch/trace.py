"""只追加的 trace。所有状态都由事件重放得出，不允许改写历史。"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


class Tracer:
    def __init__(self, path: str | Path, session: int = 1, operation_id: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 同一个 run 目录会被反复重跑（改规则、试配置）。事件只追加，
        # 所以必须带 session 才能把"第几次跑"分开，否则统计会把历史混在一起。
        self.session = session
        self.operation_id = operation_id or uuid.uuid4().hex

    def emit(self, event: str, **data: Any) -> None:
        rec = {
            "event": event,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "session": self.session,
            "operation_id": self.operation_id,
            "data": data,
        }
        with _LOCK, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # 坏行不丢：它本身可能是某个进程崩溃的证据
                out.append({"event": "_unparsable", "raw": line})
        return out
