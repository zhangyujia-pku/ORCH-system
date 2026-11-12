"""job_01：统计 orders 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 orders 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("orders"):
            total += 1
    log.info("processed {} orders".format(total))
    return total
