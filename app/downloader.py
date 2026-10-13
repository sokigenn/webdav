import asyncio
import json
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
        'group_id', 'group_name', 'created_at', 'server_url',
    )

    def __init__(self, task_id: str, path: str, name: str, save_path: str,
                 total_size: int, group_id: str, group_name: str,
                 created_at: float, server_url: str):
        self.id = task_id
        self.path = path
        self.name = name
        self.save_path = save_path
        self.total_size = total_size
        self.downloaded = 0
        self.status = 'pending'
        self.error = ''
        self.speed = 0.0
        self.retry_count = 0
        self._cancel = False
        self.group_id = group_id
        self.group_name = group_name
        self.created_at = created_at
        self.server_url = server_url

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
            'group_id': self.group_id,
            'group_name': self.group_name,
            'created_at': self.created_at,
        }


class DownloadGroup:
    __slots__ = ('id', 'name', 'server_url', 'save_dir', 'created_at', 'task_ids')

    def __init__(self, group_id: str, name: str, server_url: str,
                 save_dir: str, created_at: float):
        self.id = group_id
        self.name = name
        self.server_url = server_url
        self.save_dir = save_dir
        self.created_at = created_at
        self.task_ids: List[str] = []

    def to_dict(self, tasks: Dict[str, 'DownloadTask']) -> dict:
        total_files = len(self.task_ids)
        done = error = running = pending = cancelled = 0
        total_size = downloaded = speed = 0
        for tid in self.task_ids:
            t = tasks.get(tid)
            if t is None:
                continue
            total_size += t.total_size
            downloaded += t.downloaded
            speed += t.speed
            if t.status == 'done':          done += 1
            elif t.status == 'error':       error += 1
            elif t.status == 'running':     running += 1
            elif t.status == 'pending':     pending += 1
            elif t.status == 'cancelled':   cancelled += 1

        if running:
            status = 'running'
        elif pending:
            status = 'pending'
        elif error and not done and not running and not pending:
            status = 'error'
        elif done == total_files and total_files > 0:
            status = 'done'
        elif error:
            status = 'error'
        else:
            status = 'done'

        progress = round(downloaded / total_size * 100, 1) if total_size > 0 else 0
        return {
            'id': self.id,
            'name': self.name,
            'server_url': self.server_url,
            'save_dir': self.save_dir,
            'created_at': self.created_at,
            'total_files': total_files,
            'done_files': done,
            'error_files': error,
            'running_files': running,
            'pending_files': pending,
            'cancelled_files': cancelled,
            'total_size': total_size,
            'downloaded': downloaded,
            'progress': progress,
            'speed': speed,
            'status': status,
        }


class DownloadManager:
    MAX_RETRIES = 3
    SPEED_WINDOW = 5.0

    @staticmethod
    def _chunk_size(file_size: int) -> int:
        if file_size > 500 * 1024 * 1024:  return 8 * 1024 * 1024
        if file_size > 100 * 1024 * 1024:  return 4 * 1024 * 1024
        if file_size >  10 * 1024 * 1024:  return 2 * 1024 * 1024
        return 512 * 1024

    def __init__(self, webdav_client, max_concurrent: int = 5,
                 state_file: Optional[Path] = None, server_url: str = ''):
        self.client = webdav_client
        self.max_concurrent = max_concurrent
        self._state_file = state_file
        self._server_url = server_url

        self._tasks: Dict[str, DownloadTask] = {}
        self._task_order: List[str] = []
        self._running_ids: Set[str] = set()

        self._groups: Dict[str, DownloadGroup] = {}
        self._group_order: List[str] = []   # newest-first

        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._started = False
        self._save_task: Optional[asyncio.Task] = None
        self._dirty = False

        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=None, pool=15.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=max_concurrent * 4,
                max_keepalive_connections=max_concurrent * 2,
                keepalive_expiry=30,
            ),
        )

        self._total_bytes: int = 0
        self._downloaded_bytes: int = 0
        self._done_count: int = 0
        self._error_count: int = 0
        self._cancelled_count: int = 0

        self._speed_samples: Deque[Tuple[float, int]] = deque()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        """Start workers, save loop, and restore persisted state."""
        self._start_workers()
        self._save_task = asyncio.create_task(self._save_loop())
        await self.load_state()

    async def close(self):
        self._started = False
        if self._save_task:
            self._save_task.cancel()
            await asyncio.gather(self._save_task, return_exceptions=True)
        if self._dirty:
            await self._do_save()
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        await self._http.aclose()

    # ── Public API ────────────────────────────────────────────────────────────

    def add_tasks_grouped(self, files: List[dict], save_dir: str,
                          group_id: str, group_name: str, server_url: str) -> int:
        grp = DownloadGroup(group_id, group_name, server_url, save_dir, time.time())
        self._groups[group_id] = grp
        self._group_order.insert(0, group_id)   # newest first

        now = time.time()
        count = 0
        for f in files:
            tid = str(uuid.uuid4())
            save_as = f.get('save_as') or f['name']
            task = DownloadTask(
                task_id=tid,
                path=f['path'],
                name=f['name'],
                save_path=str(Path(save_dir) / save_as),
                total_size=f.get('size', 0),
                group_id=group_id,
                group_name=group_name,
                created_at=now,
                server_url=server_url,
            )
            self._tasks[tid] = task
            self._task_order.append(tid)
            grp.task_ids.append(tid)
            self._total_bytes += f.get('size', 0)
            self._queue.put_nowait(task)
            count += 1

        self.mark_dirty()
        return count

    def add_tasks(self, files: List[dict], save_dir: str) -> int:
        gid = str(uuid.uuid4())
        name = files[0]['name'] if len(files) == 1 else f'{len(files)}个文件'
        return self.add_tasks_grouped(files, save_dir, gid, name, self._server_url)

    def cancel_task(self, task_id: str):
        task = self._tasks.get(task_id)
        if not task:
            return
        task._cancel = True
        if task.status == 'pending':
            task.status = 'cancelled'
            self._cancelled_count += 1
            self.mark_dirty()

    def remove_task(self, task_id: str):
        task = self._tasks.get(task_id)
        if task:
            grp = self._groups.get(task.group_id)
            if grp:
                try:
                    grp.task_ids.remove(task_id)
                except ValueError:
                    pass
        self.cancel_task(task_id)
        self._tasks.pop(task_id, None)
        self._running_ids.discard(task_id)
        try:
            self._task_order.remove(task_id)
        except ValueError:
            pass
        self.mark_dirty()

    def cancel_group(self, group_id: str):
        grp = self._groups.get(group_id)
        if not grp:
            return
        for tid in list(grp.task_ids):
            self.cancel_task(tid)

    def remove_group(self, group_id: str):
        grp = self._groups.get(group_id)
        if not grp:
            return
        for tid in list(grp.task_ids):
            task = self._tasks.get(tid)
            if task:
                task._cancel = True
                if task.status == 'pending':
                    task.status = 'cancelled'
                    self._cancelled_count += 1
            self._tasks.pop(tid, None)
            self._running_ids.discard(tid)
            try:
                self._task_order.remove(tid)
            except ValueError:
                pass
        self._groups.pop(group_id, None)
        try:
            self._group_order.remove(group_id)
        except ValueError:
            pass
        self.mark_dirty()

    def cancel_all_pending(self):
        for task in self._tasks.values():
            if task.status == 'pending':
                task._cancel = True
                task.status = 'cancelled'
                self._cancelled_count += 1
        self.mark_dirty()

    def clear_finished(self):
        to_remove = [
            tid for tid, t in self._tasks.items()
            if t.status in ('done', 'error', 'cancelled')
        ]
        for tid in to_remove:
            task = self._tasks.get(tid)
            if task:
                grp = self._groups.get(task.group_id)
                if grp:
                    try:
                        grp.task_ids.remove(tid)
                    except ValueError:
                        pass
            self._tasks.pop(tid, None)
            self._running_ids.discard(tid)
            try:
                self._task_order.remove(tid)
            except ValueError:
                pass

        # Remove groups whose tasks are all gone
        empty = [
            gid for gid, g in self._groups.items()
            if not g.task_ids
        ]
        for gid in empty:
            self._groups.pop(gid, None)
            try:
                self._group_order.remove(gid)
            except ValueError:
                pass
        self.mark_dirty()

    def get_summary(self) -> dict:
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
        }

    def get_tasks_page(self, status: Optional[str], page: int, limit: int) -> dict:
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

    def get_groups_page(self, status: Optional[str], page: int, limit: int) -> dict:
        if status:
            gids = [
                gid for gid in self._group_order
                if gid in self._groups
                and self._groups[gid].to_dict(self._tasks)['status'] == status
            ]
        else:
            gids = [gid for gid in self._group_order if gid in self._groups]
        total = len(gids)
        slice_ids = gids[page * limit: (page + 1) * limit]
        return {
            'total': total,
            'page': page,
            'limit': limit,
            'groups': [self._groups[gid].to_dict(self._tasks) for gid in slice_ids],
        }

    def get_group_tasks(self, group_id: str, page: int, limit: int) -> dict:
        grp = self._groups.get(group_id)
        if not grp:
            return {'total': 0, 'page': page, 'limit': limit, 'tasks': []}
        ids = grp.task_ids
        total = len(ids)
        slice_ids = ids[page * limit: (page + 1) * limit]
        return {
            'total': total,
            'page': page,
            'limit': limit,
            'tasks': [
                self._tasks[tid].to_dict()
                for tid in slice_ids
                if tid in self._tasks
            ],
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def mark_dirty(self):
        self._dirty = True

    async def _save_loop(self):
        while self._started:
            await asyncio.sleep(2.0)
            if self._dirty:
                self._dirty = False
                try:
                    await self._do_save()
                except Exception:
                    pass

    async def _do_save(self):
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix('.tmp')
            data = self._serialize_state()
            async with aiofiles.open(tmp, 'w', encoding='utf-8') as fh:
                await fh.write(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(self._state_file)
        except Exception:
            pass

    def _serialize_state(self) -> dict:
        groups = []
        for gid in self._group_order:
            g = self._groups.get(gid)
            if g:
                groups.append({
                    'id': g.id,
                    'name': g.name,
                    'server_url': g.server_url,
                    'save_dir': g.save_dir,
                    'created_at': g.created_at,
                    'task_ids': list(g.task_ids),
                })

        tasks = []
        for tid in self._task_order:
            t = self._tasks.get(tid)
            if t:
                tasks.append({
                    'id': t.id,
                    'group_id': t.group_id,
                    'group_name': t.group_name,
                    'path': t.path,
                    'name': t.name,
                    'save_path': t.save_path,
                    'total_size': t.total_size,
                    'downloaded': t.downloaded,
                    'status': t.status,
                    'error': t.error,
                    'retry_count': t.retry_count,
                    'created_at': t.created_at,
                    'server_url': t.server_url,
                })

        return {
            'version': 1,
            'saved_at': time.time(),
            'groups': groups,
            'tasks': tasks,
        }

    async def load_state(self):
        if not self._state_file or not self._state_file.exists():
            return
        try:
            async with aiofiles.open(self._state_file, encoding='utf-8') as fh:
                raw = await fh.read()
            data = json.loads(raw)
        except Exception:
            return

        if data.get('version') != 1:
            return

        for gd in data.get('groups', []):
            grp = DownloadGroup(
                gd['id'], gd['name'], gd.get('server_url', ''),
                gd.get('save_dir', ''), gd.get('created_at', 0),
            )
            grp.task_ids = []
            self._groups[grp.id] = grp
            self._group_order.append(grp.id)

        for td in data.get('tasks', []):
            tid = td['id']
            t = DownloadTask(
                task_id=tid,
                path=td['path'],
                name=td['name'],
                save_path=td['save_path'],
                total_size=td.get('total_size', 0),
                group_id=td.get('group_id', ''),
                group_name=td.get('group_name', ''),
                created_at=td.get('created_at', 0),
                server_url=td.get('server_url', ''),
            )
            t.downloaded = td.get('downloaded', 0)
            t.error = td.get('error', '')
            t.retry_count = td.get('retry_count', 0)

            was_active = td['status'] in ('pending', 'running')
            server_matches = (t.server_url == self._server_url)

            if was_active and server_matches:
                t.status = 'pending'
                t._cancel = False
                self._queue.put_nowait(t)
            elif was_active and not server_matches:
                t.status = 'error'
                t.error = t.error or '[中断：服务器不匹配]'
            else:
                t.status = td['status']

            self._tasks[tid] = t
            self._task_order.append(tid)

            grp = self._groups.get(t.group_id)
            if grp and tid not in grp.task_ids:
                grp.task_ids.append(tid)

        self._recalculate_counters()

    def _recalculate_counters(self):
        self._total_bytes = sum(t.total_size for t in self._tasks.values())
        self._downloaded_bytes = sum(t.downloaded for t in self._tasks.values())
        self._done_count = sum(1 for t in self._tasks.values() if t.status == 'done')
        self._error_count = sum(1 for t in self._tasks.values() if t.status == 'error')
        self._cancelled_count = sum(1 for t in self._tasks.values() if t.status == 'cancelled')

    # ── Workers ───────────────────────────────────────────────────────────────

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
                    self.mark_dirty()
                return

            if attempt > 0:
                task.retry_count = attempt
                await asyncio.sleep(min(2 ** attempt, 30))

            await self._do_download(task)

            if task.status in ('done', 'cancelled'):
                return

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
                    write_mode = 'wb'
                    task.downloaded = 0
                elif resp.status_code not in (200, 206):
                    raise RuntimeError(f'HTTP {resp.status_code}')

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
                            self.mark_dirty()
                            return

                        await fh.write(chunk)
                        n = len(chunk)
                        task.downloaded += n
                        self._downloaded_bytes += n

                        now = time.monotonic()
                        self._speed_samples.append((now, n))

                        task_speed_win.append((now, n))
                        while task_speed_win and task_speed_win[0][0] < now - 3.0:
                            task_speed_win.popleft()
                        if now - last_speed_ts >= 0.5 and task_speed_win:
                            w = now - task_speed_win[0][0]
                            task.speed = sum(b for _, b in task_speed_win) / max(w, 0.1)
                            last_speed_ts = now

            task.status = 'done'
            self._done_count += 1
            self.mark_dirty()

        except asyncio.CancelledError:
            task.status = 'cancelled'
            self._cancelled_count += 1
            self.mark_dirty()
        except Exception as exc:
            task.status = 'error'
            task.error = str(exc)
            self.mark_dirty()
        finally:
            task.speed = 0.0
            self._running_ids.discard(task.id)
