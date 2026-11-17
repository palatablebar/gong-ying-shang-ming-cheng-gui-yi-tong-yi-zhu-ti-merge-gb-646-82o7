"""SQLite 存储：批次运行清单、采信结果、候选对、合并簇、复核决定。

金额一律以字符串（Decimal 原值）存取，不在数据库层做浮点运算。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .config import DB_PATH as DEFAULT_DB_PATH

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        input_sha256 TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        params_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        elapsed_ms INTEGER NOT NULL,
        counts_json TEXT NOT NULL,
        files_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS records (
        run_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        supplier_no TEXT NOT NULL,
        raw_name TEXT NOT NULL,
        norm_name TEXT NOT NULL DEFAULT '',
        norm_codes TEXT NOT NULL DEFAULT '[]',
        tax_id TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        bank_account TEXT NOT NULL DEFAULT '',
        erp TEXT NOT NULL DEFAULT '',
        ref_master_id TEXT NOT NULL DEFAULT '',
        ytd_amount TEXT NOT NULL DEFAULT '0.00',
        flags TEXT NOT NULL DEFAULT '[]',
        source_file TEXT NOT NULL DEFAULT '',
        line_no INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, supplier_id)
    )""",
    """CREATE TABLE IF NOT EXISTS quarantine (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        source_file TEXT NOT NULL,
        line_no INTEGER NOT NULL,
        raw_line TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS conflicts (
        run_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        field TEXT NOT NULL,
        kept TEXT NOT NULL,
        rejected TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        source_file TEXT NOT NULL,
        line_no INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS blocks (
        run_id TEXT NOT NULL,
        block_key TEXT NOT NULL,
        member_count INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS block_pairs (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        left_id TEXT NOT NULL,
        right_id TEXT NOT NULL,
        shared_keys TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (run_id, pair_id)
    )""",
    """CREATE TABLE IF NOT EXISTS pair_scores (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        left_id TEXT NOT NULL,
        right_id TEXT NOT NULL,
        dice TEXT NOT NULL,
        ratio TEXT NOT NULL,
        score TEXT NOT NULL,
        verdict TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, pair_id)
    )""",
    """CREATE TABLE IF NOT EXISTS clusters (
        run_id TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        rep_id TEXT NOT NULL,
        tax_id TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        member_count INTEGER NOT NULL,
        total_amount TEXT NOT NULL,
        conflict INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, cluster_id)
    )""",
    """CREATE TABLE IF NOT EXISTS cluster_members (
        run_id TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        share_amount TEXT NOT NULL,
        PRIMARY KEY (run_id, cluster_id, supplier_id)
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        decision TEXT NOT NULL,
        expected_version INTEGER NOT NULL,
        version INTEGER NOT NULL,
        operator TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (run_id, pair_id)
    )""",
    # ---- M8 付款批次 ----
    """CREATE TABLE IF NOT EXISTS payment_batches (
        batch_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        source_file TEXT NOT NULL,
        params_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        counts_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS payment_lines (
        run_id TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        line_key TEXT NOT NULL,
        src_line_no INTEGER NOT NULL,
        supplier_id TEXT NOT NULL,
        supplier_name TEXT NOT NULL DEFAULT '',
        doc_no TEXT NOT NULL DEFAULT '',
        line_type TEXT NOT NULL,
        amount TEXT NOT NULL,
        currency TEXT NOT NULL DEFAULT 'CNY',
        remark TEXT NOT NULL DEFAULT '',
        cluster_id TEXT NOT NULL DEFAULT '',
        matched_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        block_code TEXT NOT NULL DEFAULT '',
        block_detail TEXT NOT NULL DEFAULT '',
        warning_code TEXT NOT NULL DEFAULT '',
        writeoff_version INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, line_key)
    )""",
    """CREATE TABLE IF NOT EXISTS payment_writeoffs (
        run_id TEXT NOT NULL,
        line_key TEXT NOT NULL,
        action TEXT NOT NULL,
        expected_version INTEGER NOT NULL,
        version INTEGER NOT NULL,
        operator TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (run_id, line_key)
    )""",
    """CREATE TABLE IF NOT EXISTS payment_quarantine (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        source_file TEXT NOT NULL,
        line_no INTEGER NOT NULL,
        raw_line TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_pairs_run ON pair_scores(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_pay_lines_run ON payment_lines(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_pay_lines_cluster ON payment_lines(run_id, cluster_id)",
)

TABLES = ("runs", "records", "quarantine", "conflicts", "blocks", "block_pairs",
          "pair_scores", "clusters", "cluster_members", "decisions",
          "payment_batches", "payment_lines", "payment_writeoffs", "payment_quarantine")


def db_path() -> Path:
    """按环境变量解析，便于测试指向临时库。"""
    return Path(os.environ.get("MERGE_DB", str(DEFAULT_DB_PATH)))


def connect(path=None) -> sqlite3.Connection:
    target = Path(path or db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path=None) -> None:
    with connect(path) as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        conn.commit()


def reset_db(path=None) -> None:
    with connect(path) as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ---- runs ----

def save_run(run: dict) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run["run_id"],))
        conn.execute(
            "INSERT INTO runs (run_id, input_sha256, rule_version, params_json, created_at,"
            " elapsed_ms, counts_json, files_json) VALUES (?,?,?,?,?,?,?,?)",
            (run["run_id"], run["input_sha256"], run["rule_version"], run["params_json"],
             run["created_at"], run["elapsed_ms"], run["counts_json"], run["files_json"]),
        )
        conn.commit()


def get_run(run_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM runs WHERE run_id = ?", (run_id,))


def latest_run():
    with connect() as conn:
        return _one(conn, "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT 1")


def list_runs() -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC")


def update_run_counts(run_id: str, counts: dict) -> None:
    with connect() as conn:
        conn.execute("UPDATE runs SET counts_json = ? WHERE run_id = ?",
                     (json.dumps(counts, ensure_ascii=False, sort_keys=True), run_id))
        conn.commit()


# ---- 采信结果 ----

def clear_stage(run_id: str, stage: str) -> None:
    tables = {
        "intake": ("records", "quarantine", "conflicts"),
        "normalize": (),
        "blocks": ("blocks", "block_pairs"),
        "pairs": ("pair_scores",),
        "clusters": ("clusters", "cluster_members"),
    }[stage]
    with connect() as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        conn.commit()


def save_records(run_id: str, records) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO records (run_id, supplier_id, supplier_no, raw_name,"
            " norm_name, norm_codes, tax_id, city, bank_account, erp, ref_master_id, ytd_amount,"
            " flags, source_file, line_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, r["supplier_id"], r["supplier_no"], r["raw_name"], r.get("norm_name", ""),
              r.get("norm_codes", "[]"), r["tax_id"], r["city"], r["bank_account"], r["erp"],
              r["ref_master_id"], str(r["ytd_amount"]), r.get("flags", "[]"),
              r["source_file"], r["line_no"]) for r in records],
        )
        conn.commit()


def list_records(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM records WHERE run_id = ? ORDER BY supplier_id", (run_id,))


def get_record(run_id: str, supplier_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM records WHERE run_id = ? AND supplier_id = ?",
                    (run_id, supplier_id))


def update_norm(run_id: str, supplier_id: str, norm_name: str, norm_codes: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE records SET norm_name = ?, norm_codes = ?"
                     " WHERE run_id = ? AND supplier_id = ?",
                     (norm_name, norm_codes, run_id, supplier_id))
        conn.commit()


def save_quarantine(run_id: str, rows) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO quarantine (run_id, seq, source_file, line_no, raw_line, reason_code,"
            " detail) VALUES (?,?,?,?,?,?,?)",
            [(run_id, i, r["source_file"], r["line_no"], r["raw_line"], r["reason_code"],
              r.get("detail", "")) for i, r in enumerate(rows, 1)],
        )
        conn.commit()


def list_quarantine(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM quarantine WHERE run_id = ? ORDER BY seq", (run_id,))


def save_conflicts(run_id: str, rows) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO conflicts (run_id, supplier_id, field, kept, rejected, reason_code,"
            " source_file, line_no) VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, r["supplier_id"], r["field"], r["kept"], r["rejected"], r["reason_code"],
              r["source_file"], r["line_no"]) for r in rows],
        )
        conn.commit()


def list_conflicts(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM conflicts WHERE run_id = ? ORDER BY supplier_id", (run_id,))


# ---- 分块 ----

def save_blocks(run_id: str, blocks) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM blocks WHERE run_id = ?", (run_id,))
        conn.executemany("INSERT INTO blocks (run_id, block_key, member_count) VALUES (?,?,?)",
                         [(run_id, key, count) for key, count in blocks])
        conn.commit()


def list_blocks(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM blocks WHERE run_id = ? ORDER BY member_count DESC,"
                           " block_key", (run_id,))


def save_block_pairs(run_id: str, pairs) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM block_pairs WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO block_pairs (run_id, pair_id, left_id, right_id, shared_keys)"
            " VALUES (?,?,?,?,?)",
            [(run_id, p["pair_id"], p["left_id"], p["right_id"], p["shared_keys"]) for p in pairs],
        )
        conn.commit()


def list_block_pairs(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM block_pairs WHERE run_id = ? ORDER BY pair_id", (run_id,))


# ---- 候选对 ----

def save_pairs(run_id: str, pairs) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pair_scores WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO pair_scores (run_id, pair_id, left_id, right_id, dice, ratio, score,"
            " verdict, reason_code, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(run_id, p["pair_id"], p["left_id"], p["right_id"], str(p["dice"]), str(p["ratio"]),
              str(p["score"]), p["verdict"], p["reason_code"], p.get("detail", "")) for p in pairs],
        )
        conn.commit()


def list_pairs(run_id: str, verdict: str = "") -> list:
    with connect() as conn:
        if verdict:
            return _rows(conn, "SELECT * FROM pair_scores WHERE run_id = ? AND verdict = ?"
                               " ORDER BY score DESC, pair_id", (run_id, verdict))
        return _rows(conn, "SELECT * FROM pair_scores WHERE run_id = ?"
                           " ORDER BY score DESC, pair_id", (run_id,))


def get_pair(run_id: str, pair_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM pair_scores WHERE run_id = ? AND pair_id = ?",
                    (run_id, pair_id))


# ---- 合并簇 ----

def save_clusters(run_id: str, clusters, members) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM clusters WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM cluster_members WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO clusters (run_id, cluster_id, rep_id, tax_id, city, member_count,"
            " total_amount, conflict) VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, c["cluster_id"], c["rep_id"], c["tax_id"], c["city"], c["member_count"],
              str(c["total_amount"]), 1 if c["conflict"] else 0) for c in clusters],
        )
        conn.executemany(
            "INSERT INTO cluster_members (run_id, cluster_id, supplier_id, share_amount)"
            " VALUES (?,?,?,?)",
            [(run_id, m["cluster_id"], m["supplier_id"], str(m["share_amount"])) for m in members],
        )
        conn.commit()


def list_clusters(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM clusters WHERE run_id = ?"
                           " ORDER BY member_count DESC, cluster_id", (run_id,))


def list_cluster_members(run_id: str, cluster_id: str = "") -> list:
    with connect() as conn:
        if cluster_id:
            return _rows(conn, "SELECT * FROM cluster_members WHERE run_id = ? AND cluster_id = ?"
                               " ORDER BY supplier_id", (run_id, cluster_id))
        return _rows(conn, "SELECT * FROM cluster_members WHERE run_id = ?"
                           " ORDER BY cluster_id, supplier_id", (run_id,))


# ---- 复核 ----

def get_decision(run_id: str, pair_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM decisions WHERE run_id = ? AND pair_id = ?",
                    (run_id, pair_id))


def list_decisions(run_id: str, since: str = "", until: str = "") -> list:
    with connect() as conn:
        sql = "SELECT * FROM decisions WHERE run_id = ?"
        params = [run_id]
        if since:
            sql += " AND decided_at >= ?"
            params.append(since)
        if until:
            sql += " AND decided_at < ?"
            params.append(until)
        sql += " ORDER BY decided_at DESC, pair_id"
        return _rows(conn, sql, tuple(params))


def save_decision(decision: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO decisions (run_id, pair_id, decision, expected_version,"
            " version, operator, decided_at) VALUES (?,?,?,?,?,?,?)",
            (decision["run_id"], decision["pair_id"], decision["decision"],
             decision["expected_version"], decision["version"], decision["operator"],
             decision["decided_at"]),
        )
        conn.commit()


def count(run_id: str, table: str) -> int:
    if table not in TABLES:
        raise ValueError(table)
    with connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE run_id = ?", (run_id,)).fetchone()
        return int(row["n"])


# ---- M8 付款批次 ----

def save_payment_batch(batch: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO payment_batches (batch_id, run_id, input_sha256,"
            " rule_version, source_file, params_json, created_at, counts_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (batch["batch_id"], batch["run_id"], batch["input_sha256"],
             batch["rule_version"], batch["source_file"], batch["params_json"],
             batch["created_at"], batch["counts_json"]),
        )
        conn.commit()


def get_payment_batch(batch_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM payment_batches WHERE batch_id = ?", (batch_id,))


def latest_payment_batch(run_id: str = ""):
    with connect() as conn:
        if run_id:
            return _one(conn, "SELECT * FROM payment_batches WHERE run_id = ?"
                              " ORDER BY created_at DESC, batch_id DESC LIMIT 1", (run_id,))
        return _one(conn, "SELECT * FROM payment_batches"
                          " ORDER BY created_at DESC, batch_id DESC LIMIT 1")


def list_payment_batches(run_id: str = "") -> list:
    with connect() as conn:
        if run_id:
            return _rows(conn, "SELECT * FROM payment_batches WHERE run_id = ?"
                               " ORDER BY created_at DESC, batch_id DESC", (run_id,))
        return _rows(conn, "SELECT * FROM payment_batches"
                           " ORDER BY created_at DESC, batch_id DESC")


def replace_payment_lines(run_id: str, batch_id: str, lines) -> None:
    """整批替换付款行；核销决定表不动，保证重跑不改变已确认归属。"""
    with connect() as conn:
        conn.execute("DELETE FROM payment_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO payment_lines (run_id, batch_id, line_key, src_line_no, supplier_id,"
            " supplier_name, doc_no, line_type, amount, currency, remark, cluster_id,"
            " matched_name, status, block_code, block_detail, warning_code, writeoff_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, batch_id, line["line_key"], line["src_line_no"], line["supplier_id"],
              line.get("supplier_name", ""), line.get("doc_no", ""), line["line_type"],
              str(line["amount"]), line.get("currency", "CNY"), line.get("remark", ""),
              line.get("cluster_id", ""), line.get("matched_name", ""), line["status"],
              line.get("block_code", ""), line.get("block_detail", ""),
              line.get("warning_code", ""), line.get("writeoff_version", 0))
             for line in lines],
        )
        conn.commit()


def list_payment_lines(run_id: str, status: str = "", line_type: str = "") -> list:
    sql = "SELECT * FROM payment_lines WHERE run_id = ?"
    params = [run_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if line_type:
        sql += " AND line_type = ?"
        params.append(line_type)
    sql += " ORDER BY src_line_no, line_key"
    with connect() as conn:
        return _rows(conn, sql, tuple(params))


def get_payment_line(run_id: str, line_key: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM payment_lines WHERE run_id = ? AND line_key = ?",
                    (run_id, line_key))


def update_payment_line_state(run_id: str, line_key: str, status: str,
                              block_code: str, block_detail: str, version: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE payment_lines SET status = ?, block_code = ?, block_detail = ?,"
            " writeoff_version = ? WHERE run_id = ? AND line_key = ?",
            (status, block_code, block_detail, version, run_id, line_key),
        )
        conn.commit()


def get_writeoff(run_id: str, line_key: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM payment_writeoffs WHERE run_id = ? AND line_key = ?",
                    (run_id, line_key))


def save_writeoff(decision: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO payment_writeoffs (run_id, line_key, action,"
            " expected_version, version, operator, decided_at) VALUES (?,?,?,?,?,?,?)",
            (decision["run_id"], decision["line_key"], decision["action"],
             decision["expected_version"], decision["version"], decision["operator"],
             decision["decided_at"]),
        )
        conn.commit()


def list_writeoffs(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM payment_writeoffs WHERE run_id = ?"
                           " ORDER BY decided_at DESC, line_key", (run_id,))


def replace_payment_quarantine(run_id: str, rows) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM payment_quarantine WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO payment_quarantine (run_id, seq, source_file, line_no, raw_line,"
            " reason_code, detail) VALUES (?,?,?,?,?,?,?)",
            [(run_id, i, r["source_file"], r["line_no"], r["raw_line"], r["reason_code"],
              r.get("detail", "")) for i, r in enumerate(rows, 1)],
        )
        conn.commit()


def list_payment_quarantine(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM payment_quarantine WHERE run_id = ? ORDER BY seq",
                     (run_id,))
