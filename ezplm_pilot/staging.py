"""暂存数据平台的数据库模型。

设计原则：
- ezPLM 只读。这里保存一份物料镜像（orig_* 列 = 拉取时的原值，永不改），处理结果写到 new_* 列
- 每条建议（Proposal）都入 proposals 表，人工复核结果入 reviews 表；new_* 列只由"自动写入"或"复核通过"两种动作改变
- sync_state 记录拉取/处理游标，任何时候中断都能续跑
- 默认 SQLite（单文件，118 万条约 1~2 GB，够用）；设 EZPLM_STAGING_DB=postgresql://... 即可切到 Postgres
"""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Float, Index, Integer, String, Text, create_engine)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class StagedPart(Base):
    __tablename__ = "parts"
    id = Column(String(40), primary_key=True)          # ezPLM 的物料 id
    mpn = Column(String(120), index=True)
    manufacturer = Column(String(200), index=True)
    manufacturer_id = Column(String(40))
    description = Column(Text)
    description_en = Column(Text)
    attributes_json = Column(Text)                     # 物料属性（含封装）原样 JSON
    notes = Column(Text)
    status = Column(String(20))
    # 原值（拉取时快照，永不改）
    orig_category_id = Column(String(40), index=True)
    orig_category = Column(String(200))
    orig_symbol = Column(String(200))
    orig_symbol_lib = Column(String(100))
    orig_footprint = Column(String(200))
    orig_footprint_lib = Column(String(100))
    orig_has_pdf = Column(Boolean, default=False)
    orig_has_image = Column(Boolean, default=False)
    orig_url = Column(String(500))
    # 新值（自动写入或复核通过后才有）
    new_category_id = Column(String(40), index=True)
    new_category = Column(String(200))
    new_symbol = Column(String(200))
    new_footprint = Column(String(200))
    new_url = Column(String(500))
    new_datasheet = Column(String(500))
    new_description_en = Column(Text)
    new_manufacturer = Column(String(200))             # 只作提示：回写只填空，不会覆盖 ezPLM 已有制造商
    # IHS 参考数据匹配结果（ihs.py）
    mpn_norm = Column(String(120), index=True)         # 大写、去符号后的型号，用于和 IHS 等外部数据匹配
    ihs_objid = Column(String(20))                     # 最佳匹配的 IHS Objectid
    ihs_match = Column(String(40))                     # 匹配级别，如 "L2 型号+厂商"
    tags = Column(Text, default="")                    # 逗号分隔
    value_score = Column(Integer, default=0, index=True)
    # 处理状态
    fetched_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, index=True)        # 空 = 待处理
    process_version = Column(String(20))
    exported_at = Column(DateTime, index=True)         # 空 = 未回写

    def effective(self, field: str):
        return getattr(self, f"new_{field}") or getattr(self, f"orig_{field}", None)


class StagedProposal(Base):
    __tablename__ = "proposals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    part_id = Column(String(40), index=True)
    mpn = Column(String(120))
    field = Column(String(30), index=True)             # category_id / symbol / footprint / url / datasheet
    current = Column(String(500))
    proposed = Column(String(500))
    proposed_label = Column(String(500))
    confidence = Column(Float)
    action = Column(String(20), index=True)            # auto_fill / flag_mismatch / review / ok
    method = Column(String(80))
    evidence_json = Column(Text)
    tags = Column(String(200))
    review_status = Column(String(20), default="pending", index=True)  # pending / approved / rejected / auto
    reviewed_by = Column(String(60))
    reviewed_at = Column(DateTime)
    review_note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    job = Column(String(60), index=True)


Index("ix_prop_part_field", StagedProposal.part_id, StagedProposal.field)


class StagedCategory(Base):
    __tablename__ = "categories"
    id = Column(String(40), primary_key=True)
    parent = Column(String(40), index=True)
    depth = Column(Integer)
    name = Column(String(200))


class CategoryParam(Base):
    """分类的关键参数（选型时要问的参数及常见取值范围）。"""
    __tablename__ = "category_params"
    id = Column(Integer, primary_key=True, autoincrement=True)
    category_id = Column(String(40), index=True)
    name = Column(String(120))          # 参数显示名，如 Gain Bandwidth Product
    key = Column(String(80))            # 机器键，如 gbw
    ptype = Column(String(20))          # range / enum / bool / text
    unit = Column(String(30))
    vmin = Column(String(40))
    vmax = Column(String(40))
    values = Column("vals", String(500))  # enum 的常见取值，逗号分隔（列名避开 PostgreSQL 保留字 values）
    priority = Column(String(20))       # decisive / important / reference
    source = Column(String(20))         # model / manual
    sort = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class IhsRef(Base):
    """IHS 参数库（2011-12 ~ 2012-02 快照）里与暂存物料型号相同的记录。只读参考，由 `cli ihs-load` 在本地导入。"""
    __tablename__ = "ihs_ref"
    objid = Column(String(20), primary_key=True)       # IHS Objectid
    mpn = Column(String(120))
    mpn_norm = Column(String(120), index=True)
    mfr_ihs = Column(String(200))                      # IHS 原厂商名
    mfr_current = Column(String(200))                  # 经谱系推导的当前归属（规范名）
    lineage_conf = Column(String(2))                   # 谱系置信度 A/B/C；空 = 未被并购
    family = Column(String(40))                        # IHS 大类（文件名），如 ANALOG_IC_AMP
    part_category = Column(String(120))
    description = Column(Text)
    package = Column(String(200))                      # Mfr Package Description
    status = Column(String(40))                        # 截至 2012-02 的生命周期
    generic = Column(String(120))                      # Generic Number
    datasheet = Column(String(500))
    params_json = Column(Text)                         # 其余非空参数列
    batch = Column(String(10))                         # 数据批次日期


class EzCompany(Base):
    """ezPLM 的厂商 / 代理商（/admin-api/companies）。平台以它为厂商标准。"""
    __tablename__ = "ez_companies"
    id = Column(String(40), primary_key=True)
    name = Column(String(200), index=True)
    aliases_json = Column(Text)                        # ["TI", "德州仪器", ...]
    website = Column(String(300))
    is_manufacturer = Column(Boolean, default=True)
    is_supplier = Column(Boolean, default=False)
    status = Column(String(20))
    fetched_at = Column(DateTime, default=datetime.utcnow)


class EzSymbol(Base):
    """ezPLM 符号库清单（/admin-api/symbols）。不少符号以型号命名、派生自通用根符号（root）。"""
    __tablename__ = "ez_symbols"
    id = Column(String(40), primary_key=True)
    name = Column(String(200), index=True)
    norm = Column(String(200), index=True)             # kicad.norm_name(name)，与型号规范化方式一致
    category = Column(String(120))                     # 符号库/分类，如 Transistor_BJT
    root_id = Column(String(40))
    root_name = Column(String(200))
    fetched_at = Column(DateTime, default=datetime.utcnow)


class EzFootprint(Base):
    """ezPLM 封装库清单（/admin-api/footprints）。"""
    __tablename__ = "ez_footprints"
    id = Column(String(40), primary_key=True)
    name = Column(String(200), index=True)
    category = Column(String(120))                     # 如 Package_TO_SOT_SMD
    has_3d = Column(Boolean, default=False)
    has_pdf = Column(Boolean, default=False)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class OshwBom(Base):
    """开源硬件工作区的一个工程文件（BOM），来自后端导出的 CSV（docs/OSHW_EXPORT.md）。"""
    __tablename__ = "oshw_boms"
    bom_id = Column(String(40), primary_key=True)
    bom_no = Column(String(40))
    project_id = Column(String(40), index=True)
    project_name = Column(String(300))
    project_category = Column(String(120))
    repo = Column(String(300))                         # 从 notes 解析的 GitHub 仓库
    commit_ = Column("commit_sha", String(60))
    subdir = Column(String(500))
    license = Column(String(80))
    eda_tool = Column(String(20))
    parse_status = Column(String(20))
    zip_file_id = Column(String(40))
    zip_url = Column(Text)                             # 签名链接，第二阶段下载 zip 用


class OshwItem(Base):
    """开源工程 BOM 行（ezPLM 已从工程解析出的 value / mpn / 封装 / 位号）。"""
    __tablename__ = "oshw_items"
    item_id = Column(String(40), primary_key=True)
    bom_id = Column(String(40), index=True)
    value = Column(String(300))
    mpn = Column(String(200))
    manufacturer = Column(String(200))
    footprint = Column(String(300))
    designators = Column(Text)
    quantity = Column(Integer)
    part_id = Column(String(40))
    key_norm = Column(String(200), index=True)         # 用来匹配的规范化型号（mpn 优先，否则 value）；空 = 不参与匹配


class OshwMatch(Base):
    """开源 BOM 行 ↔ 暂存物料 的匹配结果（oshw-match 每次重建）。"""
    __tablename__ = "oshw_matches"
    id = Column(Integer, primary_key=True, autoincrement=True)
    part_id = Column(String(40), index=True)
    item_id = Column(String(40), index=True)
    bom_id = Column(String(40))
    how = Column(String(20))                           # mpn / value / generic
    footprint = Column(String(300))
    package_check = Column(String(10))                 # same / unknown（generic 匹配必须 same）


class IhsParamMap(Base):
    """某个 ezPLM 分类的 "IHS 参数列 → 关键参数" 映射（模型生成草稿，人工确认后才用于填参数）。"""
    __tablename__ = "ihs_param_maps"
    category_id = Column(String(40), primary_key=True)
    mapping_json = Column(Text)                        # [{target, kind, source|min|max, scale, value_map, note}]
    status = Column(String(10), default="draft")       # draft / approved
    n_parts = Column(Integer)                          # 生成时该分类有可靠 IHS 匹配的物料数
    model = Column(String(60))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    approved_by = Column(String(60))
    applied_at = Column(DateTime)
    applied_parts = Column(Integer)


class PartParam(Base):
    """物料参数值（目前来源：IHS 映射）。回写 ezPLM 需后端支持物料属性；先在平台展示与导出。"""
    __tablename__ = "part_params"
    id = Column(Integer, primary_key=True, autoincrement=True)
    part_id = Column(String(40), index=True)
    category_id = Column(String(40), index=True)
    key = Column(String(80))
    value = Column(String(200))                        # 展示值（含单位换算后的数值 / 范围 / 枚举）
    num = Column(Float)                                # 单值数值（目标单位），范围取上限；便于筛选
    unit = Column(String(30))
    source = Column(String(20))                        # ihs
    detail = Column(String(200))                       # 来源细节：匹配级别、IHS Objectid、原始列
    created_at = Column(DateTime, default=datetime.utcnow)


Index("ix_part_params_cat_key", PartParam.category_id, PartParam.key)


class SyncState(Base):
    __tablename__ = "sync_state"
    key = Column(String(60), primary_key=True)         # ingest:cursor / ingest:page / process:last_id ...
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class JobLog(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job = Column(String(60), index=True)
    stage = Column(String(20))                         # ingest / process / export
    batch_no = Column(Integer)
    n_items = Column(Integer)
    detail = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)


_ENGINES: dict = {}


def get_engine(url: str | None = None):
    if not url:
        from .settings import db_url
        url = db_url()
    if url in _ENGINES:
        return _ENGINES[url]
    kw = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10, "pool_timeout": 30, "pool_recycle": 1800}
    eng = create_engine(url, **kw)
    Base.metadata.create_all(eng)
    _migrate(eng)
    if url.startswith("sqlite"):
        with eng.connect() as c:
            c.exec_driver_sql("PRAGMA journal_mode=WAL")
    _ENGINES[url] = eng
    return eng


_NEW_PART_COLUMNS = {           # 后加的 parts 列：create_all 不会给已有表加列，这里补上
    "new_description_en": "TEXT", "new_manufacturer": "VARCHAR(200)",
    "mpn_norm": "VARCHAR(120)", "ihs_objid": "VARCHAR(20)", "ihs_match": "VARCHAR(40)",
}


def _migrate(eng):
    """小型就地迁移：旧库里 category_params.values 列改名为 vals；parts 补新列。"""
    from sqlalchemy import inspect
    try:
        cols = {c["name"] for c in inspect(eng).get_columns("category_params")}
        if "values" in cols and "vals" not in cols:
            with eng.begin() as c:
                c.exec_driver_sql('ALTER TABLE category_params RENAME COLUMN "values" TO vals')
    except Exception:  # noqa
        pass
    cols = {c["name"] for c in inspect(eng).get_columns("parts")}
    missing = {k: v for k, v in _NEW_PART_COLUMNS.items() if k not in cols}
    if missing:
        with eng.begin() as c:
            for name, typ in missing.items():
                c.exec_driver_sql(f"ALTER TABLE parts ADD COLUMN {name} {typ}")
            c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_parts_mpn_norm ON parts (mpn_norm)")


def get_session(url: str | None = None):
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)()
