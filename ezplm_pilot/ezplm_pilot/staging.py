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
    kw = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}
    eng = create_engine(url, **kw)
    Base.metadata.create_all(eng)
    if url.startswith("sqlite"):
        with eng.connect() as c:
            c.exec_driver_sql("PRAGMA journal_mode=WAL")
    _ENGINES[url] = eng
    return eng


def get_session(url: str | None = None):
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)()
