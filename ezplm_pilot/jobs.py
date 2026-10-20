"""后台任务管理器：同一时间只跑一个任务（单工作线程），带实时日志、停止、历史。

跨实例互斥：Railway 重新部署时新旧容器会短暂并存，各自的内存锁互相看不见，两边同时跑处理会给同一批物料各写一份建议。
所以任务启动前还要在暂存库 sync_state 里抢一把带心跳的锁（job:lock）；持锁实例每 30 秒刷新一次，
超过 LOCK_STALE 秒没刷新视为该实例已退出（容器被替换 / 崩溃），锁可被接管。
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta

from .tz import local_now

LOCK_KEY = "job:lock"
LOCK_STALE = 120          # 秒
LOCK_BEAT = 30            # 秒
INSTANCE = f"{socket.gethostname()}:{os.getpid()}"


def _lock_mine():
    from .staging import SyncState
    return SyncState.value.like(f'%"instance": "{INSTANCE}"%')


def acquire_db_lock(job_id: str) -> str | None:
    """抢跨实例任务锁。成功返回 None；失败返回当前持锁信息（给用户看）。"""
    from sqlalchemy import or_, update
    from sqlalchemy.exc import IntegrityError
    from .staging import SyncState, get_session
    db = get_session()
    now = datetime.utcnow()
    val = json.dumps({"instance": INSTANCE, "job": job_id}, ensure_ascii=False)
    try:
        res = db.execute(update(SyncState).where(SyncState.key == LOCK_KEY, or_(SyncState.value == "", SyncState.updated_at < now - timedelta(seconds=LOCK_STALE), _lock_mine()))
                         .values(value=val, updated_at=now))
        if res.rowcount == 1:
            db.commit()
            return None
        row = db.get(SyncState, LOCK_KEY)
        if row is None:
            db.add(SyncState(key=LOCK_KEY, value=val, updated_at=now))
            db.commit()
            return None
        held = row.value
        db.rollback()
        return held
    except IntegrityError:            # 另一实例在同一瞬间插入了锁行
        db.rollback()
        return "另一实例刚刚启动了任务"
    finally:
        db.close()


def _touch_db_lock(release: bool = False):
    from sqlalchemy import update
    from .staging import SyncState, get_session
    db = get_session()
    try:
        vals = {"value": "", "updated_at": datetime.utcnow()} if release else {"updated_at": datetime.utcnow()}
        db.execute(update(SyncState).where(SyncState.key == LOCK_KEY, _lock_mine()).values(**vals))
        db.commit()
    except Exception as e:  # noqa —— 心跳失败不影响任务本身；连续失败会让锁过期，最坏情况是允许另一实例接管
        print(f"[jobs] 刷新任务锁失败：{e}")
        db.rollback()
    finally:
        db.close()


class Job:
    def __init__(self, kind: str, params: dict):
        self.id = f"{kind}-{local_now():%Y%m%d-%H%M%S}"
        self.kind, self.params = kind, params
        self.status = "queued"          # queued / running / done / failed / stopped
        self.log: deque[str] = deque(maxlen=400)
        self.started_at = local_now()                  # 任务时间按显示时区（默认北京时间）记录，见 tz.py
        self.finished_at: datetime | None = None
        self.result = None
        self._stop = threading.Event()
        self.progress = {"done": 0, "total": 0}

    def say(self, msg: str):
        self.log.append(f"[{local_now():%H:%M:%S}] {msg}")

    def stop(self):
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "params": self.params, "status": self.status, "progress": self.progress,
                "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": self.finished_at.strftime("%Y-%m-%d %H:%M:%S") if self.finished_at else None,
                "result": self.result, "log": list(self.log)}


class JobManager:
    def __init__(self):
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=50)
        self._lock = threading.Lock()

    def is_busy(self) -> bool:
        return self.current is not None and self.current.status in ("queued", "running")

    def start(self, kind: str, fn, params: dict | None = None) -> Job:
        with self._lock:
            if self.is_busy():
                raise RuntimeError(f"已有任务在运行：{self.current.id}")
            job = Job(kind, params or {})
            held = acquire_db_lock(job.id)
            if held is not None:
                try:
                    held = json.loads(held)
                    held = f"实例 {held.get('instance')} 的任务 {held.get('job')}"
                except (ValueError, AttributeError):
                    pass
                raise RuntimeError(f"另一个实例正在运行任务（{held}）。通常是 Railway 重新部署时新旧容器并存；"
                                   f"等那个任务结束，或旧容器退出约 {LOCK_STALE // 60} 分钟后再试")
            self.current = job

        def heartbeat():
            while job.status in ("queued", "running"):
                time.sleep(LOCK_BEAT)
                if job.status in ("queued", "running"):
                    _touch_db_lock()

        def run():
            job.status = "running"
            job.say(f"开始 {kind} {job.params}")
            threading.Thread(target=heartbeat, daemon=True, name=f"{job.id}-beat").start()
            try:
                job.result = fn(job)
                job.status = "stopped" if job.should_stop() else "done"
                job.say(f"结束：{job.result}")
            except Exception as e:  # noqa
                job.status = "failed"
                job.result = {"error": str(e)}
                job.say("失败：" + str(e))
                job.say(traceback.format_exc()[-1500:])
            finally:
                job.finished_at = local_now()
                self.history.appendleft(job)
                _touch_db_lock(release=True)

        threading.Thread(target=run, daemon=True, name=job.id).start()
        return job

    def stop(self):
        if self.current and self.is_busy():
            self.current.say("收到停止请求，当前批次完成后停止…")
            self.current.stop()
            return True
        return False


manager = JobManager()
