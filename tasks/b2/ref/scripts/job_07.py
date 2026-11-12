"""job_07：统计 payments 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 payments 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("payments"):
            total += 1
    log.info("processed {} payments".format(total))
    return total
