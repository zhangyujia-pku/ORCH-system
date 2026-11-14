"""orch —— 多 agent 编排系统的 M0 层：任务契约与静态校验。"""

from .contract import (
    CAPABILITIES,
    CODES,
    FIELDS,
    Report,
    Task,
    Violation,
    is_decidable,
    repo_snapshot,
    topo_layers,
    validate_document,
    validate_file,
    writes_overlap,
)

__all__ = [
    "CAPABILITIES",
    "CODES",
    "FIELDS",
    "Report",
    "Task",
    "Violation",
    "is_decidable",
    "repo_snapshot",
    "topo_layers",
    "validate_document",
    "validate_file",
    "writes_overlap",
]
__version__ = "1.9.8"
