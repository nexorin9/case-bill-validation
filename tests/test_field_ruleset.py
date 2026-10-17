"""权威规则层 (M2) 测试。

覆盖：
- 193 项字段规范、必填率、分区计数与官方一致；
- 违规条目编号与条例 38/40 一致（38.1-38.7, 40.1-40.4）；
- 跨字段规则 ≥ 5 条，覆盖手术日期、ICU 天数、诊断-手术相符性等；
- SQLite → 内存规则集往返，幂等迁移；
- 字段类型/必填率分区分项断言；
- 跨字段评估 evaluate() 通过/失败两类断言。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from case_bill_validation.rules import (
    BillingField,
    BillingFieldRuleSet,
    Comparator,
    CrossFieldRules,
    ViolationCatalog,
)
from case_bill_validation.rules.migrations import (
    ConfigurationError,
    EXPECTED_SECTION_COUNTS,
    EXPECTED_TOTAL_FIELDS,
    init_billing_db,
)


# ---------------------------------------------------------------------------
# 1. 193 项字段规范
# ---------------------------------------------------------------------------


def test_billing_field_count_is_193(tmp_billing_db: Path) -> None:
    """193 项不变式：字段总数等于官方规范。"""
    init_billing_db(tmp_billing_db)
    rs = BillingFieldRuleSet.open(tmp_billing_db)
    assert rs.count() == EXPECTED_TOTAL_FIELDS == 193


def test_billing_field_section_counts_match_official(tmp_billing_db: Path) -> None:
    """分区数 31+6+58+98=193 与官方一致。"""
    init_billing_db(tmp_billing_db)
    rs = BillingFieldRuleSet.open(tmp_billing_db)
    counts = rs.section_counts()
    assert counts == EXPECTED_SECTION_COUNTS
    assert counts["基本信息"] == 31
    assert counts["门诊慢特病诊疗"] == 6
    assert counts["住院诊疗"] == 58
    assert counts["医疗收费"] == 98


def test_billing_field_required_distribution(tmp_billing_db: Path) -> None:
    """必填字段分布合理（结算清单 193 项中"有则必填"项与条件项并存）。

    按医保版规则清单硬规则 3：所有项目均为必填数据指标，但"有则必填"
    语义下少数字段对绝大多数病案确为条件可选（新生儿/颅脑损伤/转科等）。
    本断言验证必填率落在 10-60% 区间内，193 项中至少 30 个必填。
    """
    init_billing_db(tmp_billing_db)
    rs = BillingFieldRuleSet.open(tmp_billing_db)
    required = rs.required_fields()
    optional = rs.optional_fields()
    total = rs.count()
    req_rate = len(required) / total
    assert 0.10 <= req_rate <= 0.60, f"必填率 {req_rate:.2%} 异常"
    assert len(required) >= 30
    assert len(optional) >= 30


def test_billing_field_has_key_inpatient_fields(tmp_billing_db: Path) -> None:
    """关键住院诊疗字段（来自官方规范 §2）。"""
    init_billing_db(tmp_billing_db)
    rs = BillingFieldRuleSet.open(tmp_billing_db)
    must_have = [
        "入院日期", "出院日期", "实际住院天数", "入院科别", "出院科别",
        "主要诊断名称", "主要诊断代码", "门急诊诊断西医", "门急诊诊断代码",
        "主要手术操作名称", "主要手术操作代码", "主要手术操作日期",
        "重症监护病房类型", "ICU住院天数", "离院方式",
        "拟接收机构名称", "拟接收机构代码",
    ]
    for name in must_have:
        assert rs.has_field(name), f"关键字段 {name} 缺失"
        f = rs.get_field(name)
        assert f.section == "住院诊疗"


def test_billing_field_has_14_fee_categories(tmp_billing_db: Path) -> None:
    """14 类费用类目（床位/诊察/检查/化验/治疗/手术/护理/卫生材料/
    西药/中药饮片/中成药/一般诊疗/挂号/其他）齐全。"""
    init_billing_db(tmp_billing_db)
    rs = BillingFieldRuleSet.open(tmp_billing_db)
    fee_prefixes = [
        "床位费", "诊察费", "检查费", "化验费", "治疗费", "手术费",
        "护理费", "卫生材料费", "西药费", "中药饮片费", "中成药费",
        "一般诊疗费", "挂号费", "其他费",
    ]
    for prefix in fee_prefixes:
        matches = [f for f in rs.iter_all() if f.name.startswith(prefix)]
        assert len(matches) >= 3, f"{prefix} 字段不足：{len(matches)}"


def test_billing_field_in_memory_construction_matches_db(tmp_billing_db: Path) -> None:
    """内存规则集（from_specs）与 SQLite 加载结果一致。"""
    from case_bill_validation.rules.migrations import list_fields
    init_billing_db(tmp_billing_db)
    rs_db = BillingFieldRuleSet.open(tmp_billing_db)
    rs_mem = BillingFieldRuleSet.from_specs(list(list_fields()))
    assert rs_db.count() == rs_mem.count() == 193
    for f in rs_db.iter_all():
        m = rs_mem.get_field(f.name)
        assert m is not None
        assert f.section == m.section
        assert f.field_type == m.field_type
        assert f.required == m.required


def test_idempotent_init(tmp_billing_db: Path) -> None:
    """重复 init 字段数不变。"""
    counts1 = init_billing_db(tmp_billing_db)
    counts2 = init_billing_db(tmp_billing_db)
    assert counts1 == counts2 == {"billing_fields": 193, "violation_catalog": 24, "cross_field_rules": 10}


# ---------------------------------------------------------------------------
# 2. 违规负面清单
# ---------------------------------------------------------------------------


def test_violation_catalog_has_clause_38(tmp_billing_db: Path) -> None:
    """条例第 38 条 7 条违规（38.1-38.7）齐全。"""
    init_billing_db(tmp_billing_db)
    cat = ViolationCatalog.open(tmp_billing_db)
    for i in range(1, 8):
        assert cat.has(f"38.{i}"), f"38.{i} 缺失"
        e = cat.get(f"38.{i}")
        assert e.clause == "38"
        assert e.severity in ("info", "warn", "error")


def test_violation_catalog_has_clause_40(tmp_billing_db: Path) -> None:
    """条例第 40 条 4 条违规（40.1-40.4）齐全。"""
    init_billing_db(tmp_billing_db)
    cat = ViolationCatalog.open(tmp_billing_db)
    for i in range(1, 5):
        assert cat.has(f"40.{i}"), f"40.{i} 缺失"


def test_violation_catalog_severity_distribution(tmp_billing_db: Path) -> None:
    """违规严重度分布合理（绝大多数为 error）。"""
    init_billing_db(tmp_billing_db)
    cat = ViolationCatalog.open(tmp_billing_db)
    sev = cat.count_by_severity()
    assert sev.get("error", 0) >= 15
    assert "warn" in sev
    assert "info" not in sev or sev.get("info", 0) == 0


def test_violation_catalog_38_3_covers_decomposition(tmp_billing_db: Path) -> None:
    """38.3 重复/超标准/分解收费覆盖三大子类。"""
    init_billing_db(tmp_billing_db)
    cat = ViolationCatalog.open(tmp_billing_db)
    e = cat.get("38.3")
    assert "重复收费" in e.description
    assert "超标准收费" in e.description
    assert "分解项目收费" in e.description


def test_violation_catalog_wuhan_catalog(tmp_billing_db: Path) -> None:
    """武汉医保局盘点条目（WH-3.1 ~ WH-3.6）齐全。"""
    init_billing_db(tmp_billing_db)
    cat = ViolationCatalog.open(tmp_billing_db)
    for i in range(1, 7):
        assert cat.has(f"WH-3.{i}"), f"WH-3.{i} 缺失"


def test_violation_catalog_audit_rules(tmp_billing_db: Path) -> None:
    """智能审核规则（4 节）条目齐全（AUDIT-1 ~ AUDIT-7）。"""
    init_billing_db(tmp_billing_db)
    cat = ViolationCatalog.open(tmp_billing_db)
    for i in range(1, 8):
        assert cat.has(f"AUDIT-{i}"), f"AUDIT-{i} 缺失"


# ---------------------------------------------------------------------------
# 3. 跨字段规则
# ---------------------------------------------------------------------------


def test_cross_field_rules_count(tmp_billing_db: Path) -> None:
    """跨字段规则 ≥ 5 条（任务硬条件 + task 23 扩展到 ≥10）。"""
    init_billing_db(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    assert crs.count() >= 5


def test_cross_field_surgery_after_admission_pass(tmp_billing_db: Path) -> None:
    """手术日期 ≥ 入院日期：通过。"""
    init_billing_db(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    hits = crs.evaluate({
        "主要手术操作日期": "2026-10-15",
        "入院日期": "2026-10-10",
    })
    surgery_hits = [h for h in hits if h.rule_id == "surgery_date_after_admission"]
    assert len(surgery_hits) == 1
    assert surgery_hits[0].severity == "info"


def test_cross_field_surgery_before_admission_fail(tmp_billing_db: Path) -> None:
    """手术日期 < 入院日期：违规命中（条例 38.7）。"""
    init_billing_db(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    hits = crs.evaluate({
        "主要手术操作日期": "2026-10-05",
        "入院日期": "2026-10-10",
    })
    surgery_hits = [h for h in hits if h.rule_id == "surgery_date_after_admission"]
    assert len(surgery_hits) == 1
    assert surgery_hits[0].severity == "error"
    assert "38.7" in surgery_hits[0].message_text


def test_cross_field_icu_days_exceed_stay_fail(tmp_billing_db: Path) -> None:
    """ICU 天数 > 实际住院天数：违规（武汉盘点 3.5）。"""
    init_billing_db(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    hits = crs.evaluate({"ICU住院天数": 10, "实际住院天数": 5})
    icu_hits = [h for h in hits if h.rule_id == "icu_days_le_stay_days"]
    assert len(icu_hits) == 1
    assert icu_hits[0].severity == "error"


def test_cross_field_bed_days_le_stay_days(tmp_billing_db: Path) -> None:
    """床位计费天数 ≤ 实际住院天数（条例 38.3）。"""
    init_billing_db(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    hits = crs.evaluate({"床位费天数": 7, "实际住院天数": 5})
    bed_hits = [h for h in hits if h.rule_id == "bed_days_le_stay_days"]
    assert len(bed_hits) == 1
    assert bed_hits[0].severity == "error"


def test_cross_field_transfer_requires_org_when_discharge_2(tmp_billing_db: Path) -> None:
    """离院方式 2（医嘱转院）时缺拟接收机构名称 = 违规。"""
    init_billing_db(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    hits = crs.evaluate({
        "拟接收机构名称": "",  # 缺
        "离院方式": "2医嘱转院",
    })
    org_hits = [h for h in hits if h.rule_id == "transfer_requires_org"]
    assert len(org_hits) == 1
    assert org_hits[0].severity == "error"


def test_cross_field_transfer_requires_org_when_discharge_1_ok(tmp_billing_db: Path) -> None:
    """离院方式 1（医嘱离院）时不要求填接收机构 = 通过。"""
    init_billing_db(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    hits = crs.evaluate({
        "拟接收机构名称": "",  # 空
        "离院方式": "1医嘱离院",
    })
    org_hits = [h for h in hits if h.rule_id == "transfer_requires_org"]
    assert len(org_hits) == 1
    assert org_hits[0].severity == "info"  # 通过


def test_cross_field_skips_missing_values(tmp_billing_db: Path) -> None:
    """缺值字段不参与跨字段评估（交给必填校验）。"""
    init_billing_db(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    hits = crs.evaluate({"入院日期": "2026-10-10"})  # 缺出院日期
    discharge_hits = [h for h in hits if h.rule_id == "discharge_date_after_admission"]
    assert len(discharge_hits) == 0  # 跳过


def test_cross_field_comparator_constants(tmp_billing_db: Path) -> None:
    """比较器常量齐全。"""
    assert Comparator.GE == ">="
    assert Comparator.LE == "<="
    assert Comparator.EQ == "=="
    assert Comparator.IN_SET == "in_set"
    assert Comparator.NOT_IN_SET == "not_in_set"
    assert Comparator.DATE_DIFF_EQUALS == "date_diff_equals"


# ---------------------------------------------------------------------------
# 4. 字段名 / 跨字段左侧一致性
# ---------------------------------------------------------------------------


def test_cross_field_references_known_fields(tmp_billing_db: Path) -> None:
    """跨字段规则 lhs/rhs 都在 193 项字段中。"""
    init_billing_db(tmp_billing_db)
    rs = BillingFieldRuleSet.open(tmp_billing_db)
    crs = CrossFieldRules.open(tmp_billing_db)
    for r in crs.all():
        assert rs.has_field(r.lhs_field), f"lhs 字段 {r.lhs_field} 不在规范"
        assert rs.has_field(r.rhs_field), f"rhs 字段 {r.rhs_field} 不在规范"


def test_violation_referenced_by_field(tmp_billing_db: Path) -> None:
    """字段关联的违规条目编号在违规清单中（多条用 ; 分隔）。"""
    init_billing_db(tmp_billing_db)
    rs = BillingFieldRuleSet.open(tmp_billing_db)
    cat = ViolationCatalog.open(tmp_billing_db)
    referenced = 0
    for f in rs.iter_all():
        if f.related_violation:
            for rid in f.related_violation.split(";"):
                rid = rid.strip()
                if not rid:
                    continue
                assert cat.has(rid), (
                    f"字段 {f.name} 关联违规 {rid} 不在清单中"
                )
            referenced += 1
    assert referenced >= 5, "至少 5 个字段须挂违规条目编号"


# ---------------------------------------------------------------------------
# 5. 直接 SQLite 检查（与内存规则集等价）
# ---------------------------------------------------------------------------


def test_sqlite_direct_counts(tmp_billing_db: Path) -> None:
    """直接 SQL 计数与内存一致。"""
    init_billing_db(tmp_billing_db)
    with sqlite3.connect(str(tmp_billing_db)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM billing_fields")
        assert cur.fetchone()[0] == 193
        cur = conn.execute(
            "SELECT section, COUNT(*) FROM billing_fields GROUP BY section"
        )
        section_counts = {row[0]: row[1] for row in cur.fetchall()}
        assert section_counts["基本信息"] == 31
        assert section_counts["医疗收费"] == 98


def test_field_required_field_immutable(tmp_billing_db: Path) -> None:
    """BillingField.frozen 与 repr 一致。"""
    init_billing_db(tmp_billing_db)
    rs = BillingFieldRuleSet.open(tmp_billing_db)
    f = rs.get_field("入院日期")
    assert f is not None
    assert f.required is True
    assert f.section == "住院诊疗"
    assert f.field_type == "date"
    with pytest.raises(Exception):
        f.name = "篡改"  # type: ignore[misc]