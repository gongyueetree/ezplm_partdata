"""后台任务管理器：同一时间只跑一个任务（单工作线程），带实时日志、停止、历史。"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from datetime import datetime


class Job:
    def __init__(self, kind: str, params: dict):
        self.id = f"{kind}-{datetime.now():%Y%m%d-%H%M%S}"
        self.kind, self.params = kind, params
        self.status = "queued"          # queued / running / done / failed / stopped
        self.log: deque[str] = deque(maxlen=400)
        self.started_at = datetime.now()
        self.finished_at: datetime | None = None
        self.result = None
        self._stop = threading.Event()
        self.progress = {"done": 0, "total": 0}

    def say(self, msg: str):
        self.log.append(f"[{datetime.now():%H:%M:%S}] {msg}")

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
            self.current = job

        def run():
            job.status = "running"
            job.say(f"开始 {kind} {job.params}")
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
                job.finished_at = datetime.now()
                self.history.appendleft(job)

        threading.Thread(target=run, daemon=True, name=job.id).start()
        return job

    def stop(self):
        if self.current and self.is_busy():
            self.current.say("收到停止请求，当前批次完成后停止…")
            self.current.stop()
            return True
        return False


manager = JobManager()
