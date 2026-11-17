"""M8 付款批次列契约：表头映射、行类型识别、金额 Decimal 化与行键指纹。

付款 CSV 与名录 CSV 列契约不同，单独定义；金额规则复用 M1 的全角/千分位容忍，
但允许负数（退款抵扣即负数行），并在解析层把"行类型 + 符号"统一成规范行类型。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ...config import (
    DEDUCTION_TYPE_TOKENS,
    LINE_TYPE_DEDUCTION,
    LINE_TYPE_PAYABLE,
    PAYABLE_TYPE_TOKENS,
    PAYMENT_COLUMN_ALIASES,
    PAYMENT_COLUMNS,
)
from ...reasons import W_AMOUNT_INVALID, W_TYPE_UNKNOWN

CENT = Decimal("0.01")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．，－", "0123456789.,-")

PAYMENT_COLUMN_NAMES = tuple(name for name, _required in PAYMENT_COLUMNS)
PAYMENT_REQUIRED = tuple(name for name, required in PAYMENT_COLUMNS if required)


def normalize_header(cells) -> list:
    header = []
    for cell in cells:
        text = cell.replace("﻿", "").replace("　", " ").strip()
        header.append(PAYMENT_COLUMN_ALIASES.get(text, text))
    return header


def map_columns(header) -> dict:
    index = {}
    for position, name in enumerate(header):
        if name in PAYMENT_COLUMN_NAMES and name not in index:
            index[name] = position
    missing = [name for name in PAYMENT_REQUIRED if name not in index]
    return index, missing


def cell(cells, index: dict, name: str) -> str:
    position = index.get(name)
    if position is None or position >= len(cells):
        return ""
    return cells[position]


def clean_code(raw: str) -> str:
    return raw.strip().strip("﻿")


def classify_line_type(raw_type: str, amount: Decimal):
    """行类型以显式类型列优先，符号兜底；两者矛盾时以显式类型为准并交由金额符号校验。

    返回 (规范类型, 错误码)。无法识别且金额为 0 时由调用方按金额错误处理。
    """
    token = (raw_type or "").strip().lower()
    if token in PAYABLE_TYPE_TOKENS:
        return LINE_TYPE_PAYABLE, None
    if token in DEDUCTION_TYPE_TOKENS:
        return LINE_TYPE_DEDUCTION, None
    if token:
        return "", W_TYPE_UNKNOWN
    # 无类型列：按金额符号判定，正数应付、负数抵扣。
    if amount < 0:
        return LINE_TYPE_DEDUCTION, None
    return LINE_TYPE_PAYABLE, None


def parse_amount(raw: str):
    """返回 (Decimal|None, 错误码)。容忍全角、千分位、货币符号；允许负数。"""
    value = (raw or "").strip()
    if value == "":
        return None, W_AMOUNT_INVALID
    normalized = value.translate(_FULLWIDTH_DIGITS)
    normalized = normalized.replace(",", "").replace("，", "").replace(" ¥", "").replace("¥", "")
    normalized = normalized.replace("￥", "").replace(" ", "")
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None, W_AMOUNT_INVALID
    return amount.quantize(CENT, rounding=ROUND_HALF_UP), None


def line_key(source_file: str, src_line_no, supplier_id: str, doc_no: str,
             line_type: str, amount) -> str:
    """行级内容指纹：同批重跑同一物理行得到同一键，作为幂等与核销锚点。

    纳入源文件、物理行号、供应商号、单据号、类型与金额；不含批次号（批次由表关联）
    与可变状态，因此重跑时键稳定，已写入的核销决定能精确接回原行。
    """
    payload = "|".join([
        source_file,
        str(src_line_no),
        supplier_id.strip(),
        doc_no.strip(),
        line_type,
        f"{Decimal(str(amount)):.2f}",
    ])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper()
    return f"PL-{digest}"
