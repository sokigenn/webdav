import asyncio
import uuid
import time
from pathlib import Path
from collections import deque
from typing import Dict, List, Optional, Set, Deque, Tuple

import aiofiles
import httpx


class DownloadTask:
    __slots__ = (
        'id', 'path', 'name', 'save_path', 'total_size',
        'downloaded', 'status', 'error', 'speed', 'retry_count', '_cancel',
    )

    def __init__(self, task_id: str, path: str, name: str, save_path: str, total_size: int):
        self.id = task_id
        self.path = path
        self.name = name
        self.save_path = save_path
        self.total_size = total_size
        self.downloaded = 0
        self.status = 'pending'   # pending / running / done / error / cancelled
        self.error = ''
        self.speed = 0.0          # bytes/sec
        self.retry_count = 0
        self._cancel = False

    def to_dict(self) -> dict:
        progress = (
            round(self.downloaded / self.total_size * 100, 1)
            if self.total_size > 0 else 0
        )
        return {
            'id': self.id,
            'name': self.name,
            'total_size': self.total_size,
            'downloaded': self.downloaded,
            'status': self.status,
            'error': self.error,
            'speed': self.speed,
            'progress': progress,
            'retry_count': self.retry_count,
        }


class DownloadManager:
    MAX_RETRIES = 3
    SPEED_WINDOW = 5.0       # seconds for rolling-window global speed

    @staticmethod
    def _chunk_size(file_size: int) -> int:
        """Larger chunks for larger files to reduce I/O overhead."""
        if file_size > 500 * 1024 * 1024:  return 8 * 1024 * 1024   # >500 MB → 8 MB
        if file_size > 100 * 1024 * 1024:  return 4 * 1024 * 1024   # >100 MB → 4 MB
        if file_size >  10 * 1024 * 1024:  return 2 * 1024 * 1024   # > 10 MB → 2 MB
        return 512 * 1024                                              # small   → 512 KB

    def __init__(self, webdav_client, max_concurrent: int = 5):
        self.client = webdav_client
        self.max_concurrent = max_concurrent

        # Task registry (all tasks, all time)
        self._tasks: Dict[str, DownloadTask] = {}
        self._task_order: List[str] = []       # insertion order for pagination
        self._running_ids: Set[str] = set()    # ids with status=='running'

        # Queue & worker coroutines
        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._started = False

        # Shared HTTP client – reused across all downloads (connection-pool)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=None, pool=15.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=max_concurrent * 4,
                max_keepalive_connections=max_concurrent * 2,
                keepalive_expiry=30,
            ),
        )

        # Aggregate counters (O(1) update each event)
        self._total_bytes: int = 0
        self._downloaded_bytes: int = 0
        self._done_count: int = 0
        self._error_count: int = 0
        self._cancelled_count: int = 0

        # Global speed: (monotonic_time, bytes_delta) samples
        self._speed_samples: Deque[Tuple[float, int]] = deque()

    # ── Public API ──────────────────────────────────────────────────────────

    async def close(self):
        """Shut down workers and the HTTP client."""
        self._started = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        await self._http.aclose()

    def add_tasks(self, files: List[dict], save_dir: str) -> int:
        if not self._started:
            self._start_workers()
        count = 0
        for f in files:
            tid = str(uuid.uuid4())
            # save_as carries relative path (e.g. "folder/sub/file.txt") to preserve structure
            save_as = f.get('save_as') or f['name']
            task = DownloadTask(
                task_id=tid,
                path=f['path'],
                name=f['name'],
                save_path=str(Path(save_dir) / save_as),
                total_size=f.get('size', 0),
            )
            self._tasks[tid] = task
            self._task_order.append(tid)
            self._total_bytes += f.get('size', 0)
            self._queue.put_nowait(task)
            count += 1
        return count

    def cancel_task(self, task_id: str):
        task = self._tasks.get(task_id)
        if not task:
            return
        task._cancel = True
        if task.status == 'pending':
            task.status = 'cancelled'
            self._cancelled_count += 1
        # running tasks: the download loop detects _cancel on the next chunk

    def remove_task(self, task_id: str):
        self.cancel_task(task_id)
        self._tasks.pop(task_id, None)
        self._running_ids.discard(task_id)
        try:
            self._task_order.remove(task_id)
        except ValueError:
            pass

    def cancel_all_pending(self):
        for task in self._tasks.values():
            if task.status == 'pending':
                task._cancel = True
                task.status = 'cancelled'
                self._cancelled_count += 1

    def clear_finished(self):
        """Remove done/error/cancelled tasks from memory to free RAM."""
        to_remove = [
            tid for tid, t in self._tasks.items()
            if t.status in ('done', 'error', 'cancelled')
        ]
        for tid in to_remove:
            self._tasks.pop(tid, None)
            self._running_ids.discard(tid)
            try:
                self._task_order.remove(tid)
            except ValueError:
                pass

    def get_summary(self) -> dict:
        """O(running_count) – safe to call every second even with 10 000+ tasks."""
        now = time.monotonic()
        cutoff = now - self.SPEED_WINDOW
        while self._speed_samples and self._speed_samples[0][0] < cutoff:
            self._speed_samples.popleft()

        if len(self._speed_samples) >= 2:
            window = self._speed_samples[-1][0] - self._speed_samples[0][0]
            speed = sum(b for _, b in self._speed_samples) / max(window, 0.5)
        elif self._speed_samples:
            speed = float(self._speed_samples[0][1])
        else:
            speed = 0.0

        remaining = max(self._total_bytes - self._downloaded_bytes, 0)
        eta = int(remaining / speed) if speed > 0 and remaining > 0 else None

        total = len(self._task_order)
        running = len(self._running_ids)
        pending = max(
            total - self._done_count - self._error_count
            - self._cancelled_count - running, 0
        )

        running_tasks = [
            self._tasks[tid].to_dict()
            for tid in list(self._running_ids)
            if tid in self._tasks
        ]

        return {
            'total': total,
            'done': self._done_count,
            'error': self._error_count,
            'cancelled': self._cancelled_count,
            'running': running,
            'pending': pending,
            'total_bytes': self._total_bytes,
            'downloaded_bytes': self._downloaded_bytes,
            'speed': speed,
            'eta_seconds': eta,
            'running_tasks': running_tasks,
        }

    def get_tasks_page(
        self,
        status: Optional[str],
        page: int,
        limit: int,
    ) -> dict:
        """Paginated task list for browsing error/done/all views."""
        if status:
            ids = [
                tid for tid in self._task_order
                if tid in self._tasks and self._tasks[tid].status == status
            ]
        else:
            ids = [tid for tid in self._task_order if tid in self._tasks]
        total = len(ids)
        slice_ids = ids[page * limit: (page + 1) * limit]
        return {
            'total': total,
            'page': page,
            'limit': limit,
            'tasks': [self._tasks[tid].to_dict() for tid in slice_ids],
        }

    # ── Workers ─────────────────────────────────────────────────────────────

    def _start_workers(self):
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker())
            for _ in range(self.max_concurrent)
        ]

    async def _worker(self):
        while self._started:
            try:
                task: DownloadTask = await asyncio.wait_for(
                    self._queue.get(), timeout=2.0
                )
            except asyncio.TimeoutError:
                continue
            try:
                if not task._cancel and task.status != 'cancelled':
                    await self._download_with_retry(task)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    async def _download_with_retry(self, task: DownloadTask):
        for attempt in range(self.MAX_RETRIES + 1):
            if task._cancel:
                if task.status != 'cancelled':
                    task.status = 'cancelled'
                    self._cancelled_count += 1
                return

            if attempt > 0:
                task.retry_count = attempt
                await asyncio.sleep(min(2 ** attempt, 30))  # exponential back-off

            await self._do_download(task)

            if task.status in ('done', 'cancelled'):
                return
            # status == 'error' → retry unless last attempt

        # All retries exhausted
        self._error_count += 1

    async def _do_download(self, task: DownloadTask):
        task.status = 'running'
        self._running_ids.add(task.id)

        save_path = Path(task.save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        existing = save_path.stat().st_size if save_path.exists() else 0

        url = self.client.get_download_url(task.path)
        auth = self.client.get_auth()

        req_headers: dict = {}
        write_mode = 'wb'
        if existing > 0:
            req_headers['Range'] = f'bytes={existing}-'
            write_mode = 'ab'
            task.downloaded = existing

        chunk_size = self._chunk_size(task.total_size)
        task_speed_win: deque = deque()
        last_speed_ts = time.monotonic()

        try:
            async with self._http.stream(
                'GET', url, auth=auth, headers=req_headers
            ) as resp:
                if resp.status_code == 200 and existing > 0:
                    # Server doesn't support Range → restart
                    write_mode = 'wb'
                    task.downloaded = 0
                elif resp.status_code not in (200, 206):
                    raise RuntimeError(f'HTTP {resp.status_code}')

                # Resolve unknown total size from response headers
                if task.total_size == 0:
                    cr = resp.headers.get('content-range', '')
                    if '/' in cr:
                        ts = cr.split('/')[-1]
                        if ts.isdigit():
                            task.total_size = int(ts)
                            self._total_bytes += int(ts)
                    if task.total_size == 0:
                        cl = resp.headers.get('content-length', '')
                        if cl.isdigit():
                            task.total_size = task.downloaded + int(cl)
                            self._total_bytes += task.total_size

                async with aiofiles.open(save_path, write_mode) as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                        if task._cancel:
                            task.status = 'cancelled'
                            self._cancelled_count += 1
                            return

                        await fh.write(chunk)
                        n = len(chunk)
                        task.downloaded += n
                        self._downloaded_bytes += n

                        now = time.monotonic()

                        # Global speed sample
                        self._speed_samples.append((now, n))

                        # Per-task speed (3-second rolling window)
                        task_speed_win.append((now, n))
                        while task_speed_win and task_speed_win[0][0] < now - 3.0:
                            task_speed_win.popleft()
                        if now - last_speed_ts >= 0.5 and task_speed_win:
                            w = now - task_speed_win[0][0]
                            task.speed = sum(b for _, b in task_speed_win) / max(w, 0.1)
                            last_speed_ts = now

            task.status = 'done'
            self._done_count += 1

        except asyncio.CancelledError:
            task.status = 'cancelled'
            self._cancelled_count += 1
        except Exception as exc:
            task.status = 'error'
            task.error = str(exc)
        finally:
            task.speed = 0.0
            self._running_ids.discard(task.id)
