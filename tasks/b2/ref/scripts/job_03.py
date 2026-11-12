"""job_03：统计 refunds 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 refunds 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("refunds"):
            total += 1
    log.info("processed {} refunds".format(total))
    return total
