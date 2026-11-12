"""job_02：统计 invoices 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 invoices 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("invoices"):
            total += 1
    log.info("processed {} invoices".format(total))
    return total
