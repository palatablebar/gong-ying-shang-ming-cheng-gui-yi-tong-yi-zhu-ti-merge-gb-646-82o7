"""样例批次的期望值：由 sample_data/ 的六个脏文件固定。

改动样例数据时这份常量必须同步更新，否则测试失败是正确信号。
"""
from decimal import Decimal

RUN_ID = "MR-8FC3B362EC8F"

INPUT_ROWS = 73
ACCEPTED = 70
QUARANTINED = 2
CONFLICTS = 1

PAIRS = 106
PAIRS_AUTO = 46
PAIRS_REVIEW = 3
PAIRS_REJECT = 57

CLUSTERS = 38
MERGED_MEMBERS = 32
SINGLETONS = 19
CONFLICT_CLUSTERS = 1

AMOUNT_TOTAL = Decimal("1648046.17")
DISTINCT_NORMALIZED = 39

# 金鼎电子科技：通过空税号记录发生传递合并，簇内两个税号冲突。
CONFLICT_CLUSTER_ID = "C-0009"
CONFLICT_CLUSTER_REP = "S-0010"
CONFLICT_CLUSTER_TOTAL = Decimal("79200.00")
CONFLICT_CLUSTER_SHARES = (
    ("S-0010", Decimal("68000.00")),
    ("S-0028", Decimal("5200.00")),
    ("S-0071", Decimal("2600.00")),
    ("S-0072", Decimal("3400.00")),
)

# 同税号但名称仅部分相同：税额匹配优先于名称得分。
PAIR_TAX_MATCH = "P-00042"
PAIR_TAX_MATCH_SCORE = Decimal("0.772500")

# 同名不同城、税号不同：硬冲突拒绝。
PAIR_TAX_CONFLICT = "P-00015"

# 名称相同、城市不同、一侧无税号：降级为复核。
PAIR_CITY_MISMATCH = "P-00044"

# 名称近似、无税号：落在复核带。
PAIR_REVIEW = "P-00105"
PAIR_REVIEW_SCORE = Decimal("0.832778")

# 盛世文化传播与盛世文化：同税号但名称得分低，合计 22000 + 1500。
CLUSTER_SEQUENCE_TOTAL = Decimal("23500.00")
TAX_MATCH_CLUSTER_REP = "S-0012"

# ---- M8 付款批次（由 sample_data/pay_g_batch_utf8.csv 固定）----
# 21 个数据行：14 采纳（8 应付 + 6 抵扣），7 隔离。
PAY_INPUT_ROWS = 21
PAY_ACCEPTED = 14
PAY_PAYABLE_LINES = 8
PAY_DEDUCTION_LINES = 6
PAY_QUARANTINED = 7
PAY_PENDING = 5
PAY_BLOCKED = 1

# 全部抵扣均为负数；待核阶段可付净额等于应付合计。
PAY_PAYABLE_TOTAL = Decimal("53300.00")
# C-0001 两笔抵扣 -2000.00 与 -500.50；C-0007 一笔 -1000.00。
PAY_CLUSTER_0001_DEDUCTION = Decimal("-2500.50")
PAY_CLUSTER_0007_DEDUCTION = Decimal("-1000.00")
# 税号冲突簇 C-0009 的抵扣被阻断，永远不进净额。
PAY_CONFLICT_CLUSTER = "C-0009"
# 名称不符的应付行（主数据"广州市南方建材有限公司"）。
PAY_NAME_MISMATCH_SUPPLIER = "S-0005"
# 主数据中不存在的供应商，必须隔离。
PAY_UNKNOWN_SUPPLIER = "S-0999"

PAY_QUARANTINE_CODES = {
    "W_AMOUNT_ZERO", "W_SUPPLIER_UNKNOWN", "W_TYPE_UNKNOWN", "W_AMOUNT_INVALID",
    "W_ROW_SHORT", "W_ROW_LONG", "W_LINE_KEY_DUP",
}
