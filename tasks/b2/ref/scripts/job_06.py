"""job_06：统计 coupons 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 coupons 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("coupons"):
            total += 1
    log.info("processed {} coupons".format(total))
    return total
