"""job_08：统计 payouts 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 payouts 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("payouts"):
            total += 1
    log.info("processed {} payouts".format(total))
    return total
