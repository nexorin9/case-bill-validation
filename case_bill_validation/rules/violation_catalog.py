"""条例第 38/40 条违规负面清单 + 武汉医保局盘点 + 智能审核规则。

启动时由 :meth:`ViolationCatalog.open` 从 SQLite 加载全部违规条目到
内存 list；按 ``rule_id`` 索引便于规则审查 processor 追加 ``RuleHit``
时直接反查条款。

权威源：``domain/rules/医保违规负面清单.md``（条例第 38/40 条 + 武汉
医保局《10 大类负面清单》+ 智能审核规则条目）。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ViolationEntry:
    rule_id: str
    clause: str
    severity: str
    title: str
    description: str
    detection_method: str


class ViolationCatalog:
    """违规负面清单（内存）。"""

    def __init__(self, entries: dict[str, ViolationEntry]) -> None:
        self._entries = entries

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, db_path: Path) -> "ViolationCatalog":
        if not db_path.exists() or db_path.stat().st_size == 0:
            from .migrations import init_billing_db
            init_billing_db(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM violation_catalog")
            rows = cur.fetchall()
        entries = {
            r["rule_id"]: ViolationEntry(
                rule_id=r["rule_id"],
                clause=r["clause"],
                severity=r["severity"],
                title=r["title"],
                description=r["description"],
                detection_method=r["detection_method"] or "",
            )
            for r in rows
        }
        return cls(entries)

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------

    def get(self, rule_id: str) -> ViolationEntry | None:
        return self._entries.get(rule_id)

    def has(self, rule_id: str) -> bool:
        return rule_id in self._entries

    def all(self) -> list[ViolationEntry]:
        return list(self._entries.values())

    def by_severity(self, severity: str) -> list[ViolationEntry]:
        return [e for e in self._entries.values() if e.severity == severity]

    def by_clause(self, clause: str) -> list[ViolationEntry]:
        return [e for e in self._entries.values() if e.clause == clause]

    def count_by_severity(self) -> dict[str, int]:
        c: Counter[str] = Counter(e.severity for e in self._entries.values())
        return dict(c)

    def count_by_clause(self) -> dict[str, int]:
        c: Counter[str] = Counter(e.clause for e in self._entries.values())
        return dict(c)

    def count(self) -> int:
        return len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, idx: str) -> bool:
        return idx in self._entries

    def __repr__(self) -> str:
        return (
            f"<ViolationCatalog total={len(self._entries)} "
            f"clauses={self.count_by_clause()} "
            f"severities={self.count_by_severity()}>"
        )


__all__ = ["ViolationEntry", "ViolationCatalog"]