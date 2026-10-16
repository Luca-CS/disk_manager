"""Background tasks with progress, cancellation and exclusivity groups.

The UI polls GET /api/tasks/<id>. A task function receives the Task object,
updates ``task.progress`` (a dict) as it goes, checks ``task.cancel`` and
returns its result (JSON-serializable).
"""

import logging
import secrets
import threading
import time
import traceback

log = logging.getLogger("dm.tasks")


class Busy(Exception):
    def __init__(self, task: "Task"):
        super().__init__(f"Une tâche « {task.label} » est déjà en cours.")
        self.task = task


class Task:
    def __init__(self, kind: str, label: str, group: str):
        self.id = secrets.token_hex(8)
        self.kind = kind
        self.label = label
        self.group = group
        self.status = "running"  # running | done | error | cancelled
        self.progress: dict = {}
        self.result = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.cancel = threading.Event()

    def as_dict(self, include_result: bool = True) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "progress": dict(self.progress), "error": self.error,
            "started": self.started, "finished": self.finished,
        }
        if include_result and self.status in ("done", "cancelled"):
            d["result"] = self.result
        return d


class TaskManager:
    """Only one running task per group: 'scan' (scans, duplicates) and 'delete'
    (every destructive operation) so deletions never interleave."""

    def __init__(self, keep: int = 50):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._keep = keep
        self._lock = threading.Lock()

    def start(self, kind: str, label: str, fn, *, group: str | None = None) -> Task:
        group = group or kind
        with self._lock:
            for t in self._tasks.values():
                if t.group == group and t.status == "running":
                    raise Busy(t)
            task = Task(kind, label, group)
            self._tasks[task.id] = task
            self._order.append(task.id)
            while len(self._order) > self._keep:
                old = self._order.pop(0)
                if self._tasks.get(old) and self._tasks[old].status != "running":
                    self._tasks.pop(old, None)

        def runner():
            try:
                result = fn(task)
                task.result = result
                task.status = "cancelled" if task.cancel.is_set() else "done"
            except Exception as e:  # surfaced to the UI, details in the log
                log.error("task %s failed: %s\n%s", kind, e, traceback.format_exc())
                task.error = str(e) or e.__class__.__name__
                task.status = "error"
            finally:
                task.finished = time.time()

        threading.Thread(target=runner, daemon=True, name=f"task-{kind}").start()
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def active(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.status == "running"]

    def latest(self, kind: str) -> Task | None:
        for tid in reversed(self._order):
            t = self._tasks.get(tid)
            if t and t.kind == kind:
                return t
        return None
