"""平台设置：存在暂存库 sync_state 表（key 前缀 cfg:），环境变量优先。

路径约定：DATA_DIR（Railway 挂卷时设为 /data）下放 staging.sqlite、libindex.sqlite、sources/、out/
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
(DATA_DIR / "out").mkdir(parents=True, exist_ok=True)
(DATA_DIR / "sources").mkdir(parents=True, exist_ok=True)
OUT_DIR = DATA_DIR / "out"
SOURCES_DIR = DATA_DIR / "sources"
LIBINDEX_PATH = DATA_DIR / "libindex.sqlite"

DEFAULTS = {
    "api_base": "https://www.ezplm.cn/admin-api",
    "api_token": "",
    "api_page_size": "200",
    "llm_model": "claude-haiku-4-5",
    "anthropic_api_key": "",
    "auto_fill_min": "0.85",
    "flag_min": "0.85",
    "process_batch": "200",
    "ingest_batch": "500",
}
ENV_MAP = {"api_base": "EZPLM_API_BASE", "api_token": "EZPLM_API_TOKEN", "anthropic_api_key": "ANTHROPIC_API_KEY", "llm_model": "EZPLM_LLM_MODEL"}
SECRET_KEYS = {"api_token", "anthropic_api_key"}


def db_url() -> str:
    if os.environ.get("EZPLM_STAGING_DB"):
        return os.environ["EZPLM_STAGING_DB"]
    u = os.environ.get("DATABASE_URL", "")
    if u:
        if u.startswith("postgres://"):
            u = "postgresql+psycopg://" + u[len("postgres://"):]
        elif u.startswith("postgresql://"):
            u = "postgresql+psycopg://" + u[len("postgresql://"):]
        return u
    return f"sqlite:///{DATA_DIR / 'staging.sqlite'}"


def get_all() -> dict:
    from .staging import SyncState, get_session
    db = get_session(db_url())
    out = dict(DEFAULTS)
    for row in db.query(SyncState).filter(SyncState.key.like("cfg:%")).all():
        out[row.key[4:]] = row.value
    for k, env in ENV_MAP.items():
        if os.environ.get(env):
            out[k] = os.environ[env]
    return out


def get(key: str) -> str:
    return get_all().get(key, DEFAULTS.get(key, ""))


def set_many(values: dict):
    from .staging import SyncState, get_session
    db = get_session(db_url())
    for k, v in values.items():
        if k in SECRET_KEYS and v == "":
            continue          # 空密码字段 = 不修改
        row = db.get(SyncState, f"cfg:{k}")
        if row:
            row.value = v
        else:
            db.add(SyncState(key=f"cfg:{k}", value=v))
    db.commit()


def apply_env():
    """把设置里的 key 放进环境变量，供 LLM SDK 等使用。"""
    s = get_all()
    if s.get("anthropic_api_key") and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = s["anthropic_api_key"]
    if s.get("llm_model"):
        os.environ["EZPLM_LLM_MODEL"] = s["llm_model"]
    return s


def api_source():
    from .sources import ApiSource
    s = get_all()
    return ApiSource(base=s["api_base"], token=s["api_token"], page_size=int(s.get("api_page_size") or 200))
