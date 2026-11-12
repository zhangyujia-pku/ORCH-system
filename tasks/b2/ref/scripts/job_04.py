"""job_04：统计 shipments 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 shipments 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("shipments"):
            total += 1
    log.info("processed {} shipments".format(total))
    return total
