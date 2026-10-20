"""显示用时区。服务器（Railway）时钟是 UTC；数据库里的时间一律按 UTC 存（datetime.utcnow），只在显示时换算。

默认北京时间（UTC+8，中国不实行夏令时，用固定偏移即可，不依赖容器里的时区数据库）；
需要别的时区时设环境变量 DISPLAY_TZ_OFFSET（小时，如 -5）。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

OFFSET_HOURS = float(os.environ.get("DISPLAY_TZ_OFFSET", "8"))
DISPLAY_TZ = timezone(timedelta(hours=OFFSET_HOURS))
LABEL = "北京时间" if OFFSET_HOURS == 8 else f"UTC{OFFSET_HOURS:+g}"


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
