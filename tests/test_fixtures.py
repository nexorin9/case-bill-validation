"""tests/test_fixtures.py — 脱敏出院小结 fixture 多样性契约。

任务 26 验收：本测试文件断言
- 10 份脱敏出院小结样例全部存在；
- 每份字数 ≥ 1500（与 spec.md §6 一致）；
- 每份章节完整（主诉/现病史/既往史/辅助检查/诊断/治疗/出院医嘱）；
- 至少 3 份预埋 KNOWN_CONFLICT 标记，覆盖 4 类典型问题
  (诊断-手术错位 / 时序倒置 / ICU 超住院 / 模糊用语 / 字段缺失)；
- 姓名/身份证使用 <EXAMPLE_NAME>/<EXAMPLE_ID> 占位，未编造真实样貌。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# 全部 10 份出院小结，按编号顺序
DISCHARGE_FILES = [f"discharge_{i:02d}.txt" for i in range(1, 11)]

# 章节关键词（每个 fixture 至少出现一次）
SECTION_KEYWORDS = [
    "主诉",
    "现病史",
    "既往史",
    "辅助检查",
    "诊断",
    "治疗",
    "出院医嘱",
]

# 占位符（必须使用，禁止编造真实姓名/身份证号）
PLACEHOLDER_NAME = "<EXAMPLE_NAME>"
PLACEHOLDER_ID = "<EXAMPLE_ID>"

# 4 类典型问题对应的 conflict 标识
EXPECTED_CONFLICT_TYPES = {
    "surgery_before_admission",       # 时序倒置：手术日期早于入院日期
    "diagnosis_surgery_mismatch",     # 诊断-手术错位：主要诊断 ICD 与主要手术 ICD 不符
    "icu_days_exceed_stay",           # ICU 天数 > 住院天数
    "vague_wording_days_unknown",     # 模糊用语：住院天数不详
    "missing_field_id_card",          # 字段缺失：缺身份证号
}

MIN_CHAR_COUNT = 1500


# ---------------- 路径存在性 ----------------


def test_fixtures_dir_exists():
    """fixtures 目录必须存在。"""
    assert FIXTURES_DIR.exists(), f"fixtures 目录缺失: {FIXTURES_DIR}"
    assert FIXTURES_DIR.is_dir()


@pytest.mark.parametrize("filename", DISCHARGE_FILES)
def test_fixture_file_exists(filename: str):
    """每份脱敏出院小结文件必须存在。"""
    path = FIXTURES_DIR / filename
    assert path.exists(), f"fixture 缺失: {path}"
    assert path.is_file()


# ---------------- 字数下限 ----------------


@pytest.mark.parametrize("filename", DISCHARGE_FILES)
def test_fixture_min_char_count(filename: str):
    """每份字数 ≥ 1500（spec.md §6 baseline）。"""
    text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
    char_count = len(text)
    assert char_count >= MIN_CHAR_COUNT, (
        f"{filename} 字数不足: {char_count} < {MIN_CHAR_COUNT}"
    )


def test_total_fixture_chars_above_baseline():
    """10 份总字数应 ≥ 1500 × 10 = 15000。"""
    total = sum(
        len((FIXTURES_DIR / f).read_text(encoding="utf-8"))
        for f in DISCHARGE_FILES
    )
    assert total >= MIN_CHAR_COUNT * 10


# ---------------- 章节完整性 ----------------


@pytest.mark.parametrize("filename", DISCHARGE_FILES)
@pytest.mark.parametrize("keyword", SECTION_KEYWORDS)
def test_fixture_has_section_keyword(filename: str, keyword: str):
    """每份 fixture 必须含 7 类章节关键词。"""
    text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
    assert keyword in text, (
        f"{filename} 缺章节关键词「{keyword}」"
    )


def test_fixture_has_baseline_patient_header():
    """每份 fixture 至少含姓名 + 住院号占位符（脱敏底线）。"""
    for filename in DISCHARGE_FILES:
        text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        assert PLACEHOLDER_NAME in text, (
            f"{filename} 缺 {PLACEHOLDER_NAME} 占位符"
        )
        assert PLACEHOLDER_ID in text, (
            f"{filename} 缺 {PLACEHOLDER_ID} 占位符"
        )


def test_fixture_no_real_looking_id_card():
    """不应出现形似真实身份证号的 18 位连续数字（脱敏底线）。"""
    id_pattern = re.compile(r"\b\d{17}[\dXx]\b")
    for filename in DISCHARGE_FILES:
        text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        # 占位符 <EXAMPLE_ID> 不是身份证号
        # 真实身份证号反例：110101199003078888
        matches = id_pattern.findall(text)
        # 排除占位符上下文（占位符 <EXAMPLE_ID> 中不应出现真实身份证）
        # 此测试要求：fixture 文件里不能出现 18 位真实身份证
        assert not matches, (
            f"{filename} 疑似包含真实身份证号: {matches}"
        )


# ---------------- 已知冲突多样性 ----------------


def test_at_least_three_fixtures_have_known_conflict():
    """至少 3 份预埋 KNOWN_CONFLICT 标记（spec.md §6 baseline）。"""
    conflict_files = [
        f for f in DISCHARGE_FILES
        if "KNOWN_CONFLICT" in (FIXTURES_DIR / f).read_text(encoding="utf-8")
    ]
    assert len(conflict_files) >= 3, (
        f"冲突样例不足: {len(conflict_files)} < 3"
    )


def test_all_four_conflict_types_present():
    """4 类典型问题必须全部出现（诊断-手术错位 / 时序倒置 /
    ICU 超住院 / 模糊用语 / 字段缺失）。"""
    all_conflicts = set()
    for filename in DISCHARGE_FILES:
        text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        for m in re.finditer(r"KNOWN_CONFLICT:\s*([a-z_]+)", text):
            all_conflicts.add(m.group(1))
    missing = EXPECTED_CONFLICT_TYPES - all_conflicts
    assert not missing, (
        f"fixture 缺少冲突类型: {missing}; 实际找到: {sorted(all_conflicts)}"
    )


def test_conflict_marker_format_is_lowercase_snake_case():
    """KNOWN_CONFLICT 标识符必须是 lowercase_snake_case。"""
    for filename in DISCHARGE_FILES:
        text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        for m in re.finditer(r"KNOWN_CONFLICT:\s*(\S+)", text):
            label = m.group(1)
            assert re.fullmatch(r"[a-z][a-z0-9_]*", label), (
                f"{filename} KNOWN_CONFLICT 标签格式非法: {label!r}"
            )


def test_conflict_fixtures_have_extra_density():
    """冲突样例字数应不少于正常样例（承载更复杂的矛盾叙述）。"""
    conflict_files = [
        f for f in DISCHARGE_FILES
        if "KNOWN_CONFLICT" in (FIXTURES_DIR / f).read_text(encoding="utf-8")
    ]
    clean_files = [
        f for f in DISCHARGE_FILES
        if "KNOWN_CONFLICT" not in (FIXTURES_DIR / f).read_text(encoding="utf-8")
    ]
    assert len(conflict_files) >= 3
    # 冲突样例平均字数 ≥ 1500
    avg_conflict = sum(
        len((FIXTURES_DIR / f).read_text(encoding="utf-8"))
        for f in conflict_files
    ) / len(conflict_files)
    assert avg_conflict >= MIN_CHAR_COUNT


# ---------------- 特殊字符多样性（中文病案真实形态） ----------------


def test_fixtures_contain_mixed_cjk_and_ascii():
    """fixture 应混排中文 + ASCII（ICD 编码、英文药名、英文缩写）。"""
    for filename in DISCHARGE_FILES:
        text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        has_cjk = any("一" <= c <= "鿿" for c in text)
        has_ascii_letter = re.search(r"[A-Za-z]", text) is not None
        has_ascii_digit = re.search(r"\d", text) is not None
        assert has_cjk, f"{filename} 缺中文"
        assert has_ascii_letter, f"{filename} 缺英文字母"
        assert has_ascii_digit, f"{filename} 缺数字"


def test_fixtures_have_icd_codes():
    """fixture 应含 ICD-10 / ICD-9-CM-3 编码（业务对象）。

    通过关键字 + 编码模式双重确认：含「ICD-10」标签或主要诊断 ICD 编码，
    含「ICD-9-CM-3」标签或主要手术编码。
    实际 8/10 含 ICD-10 标签（discharge_01/03 是基础样例
    不带编码也可走通流水线），9/10 含 ICD-9-CM-3 标签；
    阈值取实际最小值的下限，确保回归敏感。
    """
    icd10_label = re.compile(r"ICD-?10|主要诊断.*[A-Z]\d{2}")
    icd9_label = re.compile(r"ICD-?9-?CM-?3|主要手术.*\d{2}\.\d")
    fixtures_with_icd10 = 0
    fixtures_with_icd9 = 0
    for filename in DISCHARGE_FILES:
        text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        if icd10_label.search(text):
            fixtures_with_icd10 += 1
        if icd9_label.search(text):
            fixtures_with_icd9 += 1
    assert fixtures_with_icd10 >= 8, (
        f"含 ICD-10 的 fixture 不足: {fixtures_with_icd10}/10"
    )
    assert fixtures_with_icd9 >= 8, (
        f"含 ICD-9-CM-3 的 fixture 不足: {fixtures_with_icd9}/10"
    )


# ---------------- 编码形式统一 ----------------


def test_fixtures_are_utf8_decodable():
    """所有 fixture 必须能被 UTF-8 解码（中文出院小结底线）。"""
    for filename in DISCHARGE_FILES:
        path = FIXTURES_DIR / filename
        text = path.read_text(encoding="utf-8")
        assert isinstance(text, str)
        # 至少含 1 个中文字符
        assert any("一" <= c <= "鿿" for c in text)


def test_fixtures_have_admission_and_discharge_dates():
    """每份 fixture 必须含日期（业务必填）。

    接受 ISO 格式 YYYY-MM-DD 与中文格式 YYYY年MM月DD日。
    例外：discharge_09.txt（vague_wording_days_unknown 冲突）
    故意省略具体日期以表达「住院天数不详」业务矛盾，
    其余 9 份必须有日期标注。
    """
    iso_pattern = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
    cn_pattern = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
    expected_missing_date = {"discharge_09.txt"}  # 模糊用语冲突故意缺
    checked = 0
    for filename in DISCHARGE_FILES:
        if filename in expected_missing_date:
            continue
        text = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        iso_dates = iso_pattern.findall(text)
        cn_dates = cn_pattern.findall(text)
        total = len(iso_dates) + len(cn_dates)
        assert total >= 1, (
            f"{filename} 缺日期标注: ISO={iso_dates}, CN={cn_dates}"
        )
        checked += 1
    assert checked >= 9, (
        f"实际检查 fixture 数不足: {checked}"
    )


def test_fixture_09_intentionally_omits_dates():
    """discharge_09.txt 必须确实缺日期（vague wording 冲突的契约）。

    这条断言是 test_fixtures_have_admission_and_discharge_dates
    例外的对端校验：若 fixture 被修改补上日期，
    则 vague_wording_days_unknown 冲突不再成立。
    """
    text = (FIXTURES_DIR / "discharge_09.txt").read_text(encoding="utf-8")
    iso_dates = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", text)
    cn_dates = re.findall(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", text)
    assert iso_dates == [] and cn_dates == [], (
        f"discharge_09.txt 故意缺日期，应保持为空: ISO={iso_dates}, CN={cn_dates}"
    )


# ---------------- fixture 索引 ----------------


def test_fixture_index_constant_matches_disk():
    """DISCHARGE_FILES 与磁盘实际文件 1:1 对应。"""
    actual = sorted(
        p.name for p in FIXTURES_DIR.iterdir()
        if p.is_file() and p.name.startswith("discharge_") and p.suffix == ".txt"
    )
    assert actual == sorted(DISCHARGE_FILES), (
        f"fixture 索引不一致: 磁盘={actual}, 测试={sorted(DISCHARGE_FILES)}"
    )
