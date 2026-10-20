"""显示用时区。服务器（Railway）时钟是 UTC；数据库里的时间一律按 UTC 存（datetime.utcnow），只在显示时换算。

默认加州时间（America/Los_Angeles，夏令时自动切换：夏天 PDT = UTC−7，冬天 PST = UTC−8）。
需要别的时区时设环境变量 DISPLAY_TZ（IANA 时区名，如 Asia/Shanghai）。
时区数据来自 tzdata 包（slim 镜像里没有系统时区库，见 requirements.txt）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ_NAME = os.environ.get("DISPLAY_TZ", "America/Los_Angeles")
DISPLAY_TZ = ZoneInfo(TZ_NAME)
LABEL = {"America/Los_Angeles": "加州时间（PDT/PST）", "Asia/Shanghai": "北京时间"}.get(TZ_NAME, TZ_NAME)


def local_now() -> datetime:
    """当前显示时区的本地时间（不带时区信息，便于直接格式化）。"""
    return datetime.now(DISPLAY_TZ).replace(tzinfo=None)


def to_local(dt, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """数据库里的 UTC 时间 → 显示时区的字符串；空值原样返回空串。"""
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).strftime(fmt)
