"""Trace recording and local persistence."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock

from loguru import logger

from pleiad.observability.models import TraceRecord
from pleiad.observability.store import TraceStore


class TraceService:
    """Serialize direct Trace writes through one background worker."""

    def __init__(self, store: TraceStore) -> None:
        self.store = store
        self._pending: set[Future[None]] = set()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pleiad-trace")
        self._lock = RLock()
        self._closed = False

    def record(self, record: TraceRecord) -> None:
        """Enqueue one already-processed record for ordered persistence."""
        try:
            future = self._executor.submit(self.store.append, record)
        except RuntimeError:
            logger.exception("TraceService 已关闭, 无法持久化记录: {}", record.record_type.value)
            return
        with self._lock:
            self._pending.add(future)

    def list(self, execution_id: str) -> tuple[TraceRecord, ...]:
        """Flush pending writes before replaying one execution."""
        self.flush()
        return self.store.list(execution_id)

    def flush(self) -> None:
        """Wait for all queued writes and report storage failures without raising them."""
        while True:
            with self._lock:
                pending = tuple(self._pending)
            if not pending:
                return
            for future in pending:
                try:
                    future.result()
                except Exception:
                    logger.exception("Trace 持久化失败")
                finally:
                    with self._lock:
                        self._pending.discard(future)

    def close(self) -> None:
        """Flush records and release the single ordered writer."""
        if self._closed:
            return
        self.flush()
        self._executor.shutdown(wait=True)
        self.store.close()
        self._closed = True
