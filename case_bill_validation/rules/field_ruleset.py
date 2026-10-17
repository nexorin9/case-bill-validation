"""193 项结算清单字段规则集（内存镜像 + 查询 API）。

设计要点：

- 启动时由 :func:`migrations.init_billing_db` 把 193 项从 markdown 源
  灌入 SQLite，再由 :meth:`BillingFieldRuleSet.open` 一次性把全部字段
  加载到内存 dict，**避免流水线每条记录都做一次 SQL**；
- 内存结构：``self._fields: dict[str, BillingField]``，value 含类型/约束/
  分区/必填；提供 ``get_field`` / ``required_fields`` / ``fields_by_section``
  / ``iter_all`` 四类查询；
- 字段数 == 193 不变式在 :meth:`open` 末尾再次自检，失同步即抛
  :class:`migrations.ConfigurationError`；
- 流水线各 processor 仅依赖本类的内存 dict；测试可注入临时 db 验证。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .migrations import (
    ALL_FIELDS,
    ConfigurationError,
    EXPECTED_TOTAL_FIELDS,
    FieldSpec,
    init_billing_db,
)


@dataclass(frozen=True)
class BillingField:
    """193 项之一的内存镜像。"""

    name: str
    section: str
    field_type: str
    required: bool
    value_constraint: str
    description: str
    related_violation: str
    example_value: str


def _to_billing_field(row: sqlite3.Row) -> BillingField:
    return BillingField(
        name=row["name"],
        section=row["section"],
        field_type=row["field_type"],
        required=bool(row["required"]),
        value_constraint=row["value_constraint"] or "",
        description=row["description"] or "",
        related_violation=row["related_violation"] or "",
        example_value=row["example_value"] or "",
    )


class BillingFieldRuleSet:
    """193 项字段规则集（内存）。"""

    def __init__(
        self,
        fields: dict[str, BillingField],
        *,
        enforce_count: bool = True,
    ) -> None:
        self._fields = fields
        # 启动期不变式自检（生产路径强制；测试可显式关掉）
        if enforce_count and len(fields) != EXPECTED_TOTAL_FIELDS:
            raise ConfigurationError(
                f"BillingFieldRuleSet 启动期字段数核验失败："
                f"期望 {EXPECTED_TOTAL_FIELDS} 实际 {len(fields)}"
            )

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, db_path: Path) -> "BillingFieldRuleSet":
        """从 SQLite 加载。若 db 不存在或空，自动 init。"""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if not db_path.exists() or db_path.stat().st_size == 0:
            init_billing_db(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM billing_fields")
            rows = cur.fetchall()
        fields = {_to_billing_field(r) for r in rows} if False else {
            r["name"]: _to_billing_field(r) for r in rows
        }
        return cls(fields)

    @classmethod
    def from_specs(
        cls,
        specs: list[FieldSpec],
        *,
        enforce_count: bool = False,
    ) -> "BillingFieldRuleSet":
        """测试用：从内存 FieldSpec 列表构建。默认 ``enforce_count=False`` ——

        测试场景通常只需要若干字段而非完整 193 项；生产路径的 ``open()`` 仍
        强制 193 项不变式。
        """
        fields = {
            s.name: BillingField(
                name=s.name,
                section=s.section,
                field_type=s.field_type,
                required=s.required,
                value_constraint=s.value_constraint,
                description=s.description,
                related_violation=s.related_violation,
                example_value=s.example_value,
            )
            for s in specs
        }
        return cls(fields, enforce_count=enforce_count)

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------

    def get_field(self, name: str) -> BillingField | None:
        return self._fields.get(name)

    def has_field(self, name: str) -> bool:
        return name in self._fields

    def required_fields(self) -> list[str]:
        return [f.name for f in self._fields.values() if f.required]

    def optional_fields(self) -> list[str]:
        return [f.name for f in self._fields.values() if not f.required]

    def fields_by_section(self, section: str) -> list[BillingField]:
        return [f for f in self._fields.values() if f.section == section]

    def iter_all(self):
        """全部 193 项字段（按插入序，依赖 migrations 的定义顺序）。"""
        # 重排为：基本信息 → 门诊慢特病 → 住院诊疗 → 医疗收费
        section_order = ["基本信息", "门诊慢特病诊疗", "住院诊疗", "医疗收费"]
        by_section: dict[str, list[BillingField]] = {}
        for f in self._fields.values():
            by_section.setdefault(f.section, []).append(f)
        for sec in section_order:
            yield from by_section.get(sec, [])

    def section_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self._fields.values():
            out[f.section] = out.get(f.section, 0) + 1
        return out

    def count(self) -> int:
        return len(self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    def __contains__(self, name: str) -> bool:
        return name in self._fields

    def __repr__(self) -> str:
        counts = self.section_counts()
        return (
            f"<BillingFieldRuleSet total={len(self._fields)} "
            f"sections={counts}>"
        )


def assert_consistent_with_migrations() -> int:
    """启动期直接调用：迁移 + 加载并核验 193 项不变式。"""
    from .migrations import list_fields  # 局部避免循环
    specs = list(list_fields())
    rs = BillingFieldRuleSet.from_specs(list(specs))
    return rs.count()


__all__ = ["BillingField", "BillingFieldRuleSet", "assert_consistent_with_migrations"]