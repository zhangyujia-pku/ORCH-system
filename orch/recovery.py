"""恢复点仅承认已核验并整合的版本，不猜测中断的 Git 操作。"""

import hashlib
import json
from pathlib import Path
import uuid
import threading

from .control import atomic_json


class Recovery:
    def __init__(self, directory: Path, resume: bool):
        self.lock = threading.RLock()
        self.directory, self.path = directory, directory / "checkpoint.json"
        if resume:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            if self.path.exists():
                old = json.loads(self.path.read_text(encoding="utf-8"))
                atomic_json(directory / "recovery-history" / f"{old['execution_id']}.json", old)
            self.data = {"execution_id": uuid.uuid4().hex, "manifest": "", "next_layer": 0,
                         "base": "", "outcomes": [], "merges": [], "attempts": {},
                         "failures": {}, "waiting": {}, "phase": "new"}
            self.save()

    def save(self, **values):
        with self.lock:
            self.data.update(values)
            atomic_json(self.path, self.data)

    def validate(self, manifest: str):
        if self.data["manifest"] and self.data["manifest"] != manifest:
            raise ValueError("恢复点的计划、基线、检查或后端配置已改变，拒绝复用")
        self.save(manifest=manifest)

    def next_attempt(self, task_id: str) -> int:
        with self.lock:
            number = self.data["attempts"].get(task_id, 0) + 1
            self.data["attempts"][task_id] = number
            self.save()
            return number

    def failed(self, task_id: str):
        with self.lock:
            self.data["failures"][task_id] = self.data["failures"].get(task_id, 0) + 1
            self.save()

    def wait(self, task_id: str, question: str):
        with self.lock:
            self.data["waiting"][task_id] = {"id": uuid.uuid4().hex, "question": question}
            self.save()


def manifest_digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def record_answer(directory: Path, task_id: str, text: str):
    if not text.strip():
        raise ValueError("回答不能为空")
    checkpoint = json.loads((directory / "checkpoint.json").read_text(encoding="utf-8"))
    waiting = checkpoint["waiting"].get(task_id)
    if not waiting:
        raise ValueError("该任务没有待回答的问题")
    path = directory / "answers.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"answers": []}
    data["answers"].append({"execution_id": checkpoint["execution_id"], "task_id": task_id,
                             "question_id": waiting["id"], "question": waiting["question"], "answer": text})
    atomic_json(path, data)


def find_answer(directory: Path, execution_id: str, task_id: str, question_id: str):
    path = directory / "answers.json"
    if not path.exists():
        return None
    answers = json.loads(path.read_text(encoding="utf-8"))["answers"]
    return next((a for a in reversed(answers) if a["execution_id"] == execution_id and
                 a["task_id"] == task_id and a["question_id"] == question_id), None)
