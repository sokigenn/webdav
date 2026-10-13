import asyncio
from pathlib import Path
from typing import List, Optional

import httpx
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .downloader import DownloadManager
from .hosts import apply as apply_hosts, clear as clear_hosts, current as current_hosts, parse_hosts_text
from .webdav import WebDAVClient

app = FastAPI(title='WebDAV Downloader')

STATIC_DIR = Path(__file__).parent.parent / 'static'
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')

_state: dict = {'client': None, 'manager': None, 'config': {}}


# ── SPA ───────────────────────────────────────────────────────────────────────
@app.get('/')
async def index():
    return FileResponse(str(STATIC_DIR / 'index.html'))


# ── Connection ────────────────────────────────────────────────────────────────
class ConnectRequest(BaseModel):
    url: str
    username: str = ''
    password: str = ''
    max_concurrent: int = 5
    download_url: str = ''   # optional separate host for file transfers
    hosts: str = ''          # /etc/hosts-style overrides, applied process-wide


@app.post('/api/connect')
async def connect(req: ConnectRequest):
    # Close previous manager if any
    old_mgr: Optional[DownloadManager] = _state['manager']
    if old_mgr:
        await old_mgr.close()

    # Apply hosts overrides before the connection test so the test itself
    # also uses the overridden DNS resolution.
    host_map = parse_hosts_text(req.hosts) if req.hosts.strip() else {}
    apply_hosts(host_map)

    client = WebDAVClient(req.url, req.username, req.password, req.download_url)
    ok, msg = await asyncio.to_thread(client.test_connection)
    if not ok:
        raise HTTPException(400, msg)

    _state['client'] = client
    _state['manager'] = DownloadManager(client, req.max_concurrent)
    _state['config'] = {
        'url': req.url,
        'username': req.username,
        'max_concurrent': req.max_concurrent,
        'download_url': req.download_url,
        'hosts': req.hosts,
    }
    return {'status': 'ok', 'message': msg}


@app.post('/api/disconnect')
async def disconnect():
    mgr: Optional[DownloadManager] = _state['manager']
    if mgr:
        await mgr.close()
    clear_hosts()
    _state['client'] = None
    _state['manager'] = None
    _state['config'] = {}
    return {'status': 'ok'}


@app.get('/api/status')
async def status():
    if _state['client']:
        return {'connected': True, 'config': _state['config']}
    return {'connected': False, 'config': {}}


# ── File listing ──────────────────────────────────────────────────────────────
@app.get('/api/files')
async def list_files(path: str = '/'):
    client: Optional[WebDAVClient] = _state['client']
    if not client:
        raise HTTPException(400, 'Not connected to any WebDAV server')
    try:
        items = await asyncio.to_thread(client.list_directory, path)
        return {'items': items, 'path': path}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Proxy (preview + video/audio streaming) ───────────────────────────────────
@app.api_route('/api/proxy', methods=['GET', 'HEAD'])
async def proxy_file(path: str, request: Request):
    client: Optional[WebDAVClient] = _state['client']
    if not client:
        raise HTTPException(400, 'Not connected')

    url = client.get_download_url(path)
    auth = client.get_auth()

    if request.method == 'HEAD':
        head = await asyncio.to_thread(
            requests.head, url,
            **{'auth': auth, 'timeout': 10, 'allow_redirects': True},
        )
        return Response(
            status_code=head.status_code,
            headers={
                'Content-Type': head.headers.get('content-type', 'application/octet-stream'),
                'Content-Length': head.headers.get('content-length', '0'),
                'Accept-Ranges': 'bytes',
            },
        )

    range_header = request.headers.get('range')
    upstream_headers: dict = {}
    if range_header:
        upstream_headers['Range'] = range_header

    try:
        head = await asyncio.to_thread(
            requests.head, url,
            **{'auth': auth, 'timeout': 10, 'allow_redirects': True},
        )
        content_type = head.headers.get('content-type', 'application/octet-stream')
        total_size_str = head.headers.get('content-length', '')
    except Exception:
        content_type = 'application/octet-stream'
        total_size_str = ''

    resp_headers: dict = {'Accept-Ranges': 'bytes', 'Cache-Control': 'no-cache'}
    status_code = 200

    if range_header and total_size_str.isdigit():
        total = int(total_size_str)
        try:
            spec = range_header.replace('bytes=', '').strip()
            start_s, end_s = spec.split('-')
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
            resp_headers['Content-Range'] = f'bytes {start}-{end}/{total}'
            resp_headers['Content-Length'] = str(end - start + 1)
            status_code = 206
        except Exception:
            pass
    elif not range_header and total_size_str.isdigit():
        resp_headers['Content-Length'] = total_size_str

    async def stream():
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None),
            follow_redirects=True,
        ) as hclient:
            async with hclient.stream('GET', url, auth=auth, headers=upstream_headers) as r:
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    yield chunk

    return StreamingResponse(
        stream(),
        status_code=status_code,
        media_type=content_type,
        headers=resp_headers,
    )


# ── Download queue ────────────────────────────────────────────────────────────
class DownloadRequest(BaseModel):
    files: List[dict]
    save_dir: str


@app.post('/api/downloads/start')
async def start_downloads(req: DownloadRequest):
    manager: Optional[DownloadManager] = _state['manager']
    if not manager:
        raise HTTPException(400, 'Not connected')
    if not req.save_dir:
        raise HTTPException(400, 'save_dir is required')
    save_path = Path(req.save_dir)
    if not save_path.exists():
        try:
            save_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise HTTPException(400, f'Cannot create directory: {exc}')
    count = manager.add_tasks(req.files, req.save_dir)
    return {'count': count}


@app.get('/api/downloads')
async def get_downloads():
    """Returns aggregate summary + currently-running tasks. Fast even with 10 000+ tasks."""
    manager: Optional[DownloadManager] = _state['manager']
    if not manager:
        return {
            'total': 0, 'done': 0, 'error': 0, 'cancelled': 0,
            'running': 0, 'pending': 0,
            'total_bytes': 0, 'downloaded_bytes': 0,
            'speed': 0, 'eta_seconds': None,
            'running_tasks': [],
        }
    return manager.get_summary()


@app.get('/api/downloads/list')
async def list_downloads(
    status: Optional[str] = None,
    page: int = 0,
    limit: int = 50,
):
    """Paginated task list. Use status=error|done|cancelled|pending|running."""
    manager: Optional[DownloadManager] = _state['manager']
    if not manager:
        return {'total': 0, 'page': 0, 'limit': limit, 'tasks': []}
    return manager.get_tasks_page(status, page, limit)


@app.delete('/api/downloads/{task_id}')
async def cancel_download(task_id: str):
    manager: Optional[DownloadManager] = _state['manager']
    if not manager:
        raise HTTPException(400, 'Not connected')
    manager.remove_task(task_id)
    return {'status': 'ok'}


@app.post('/api/downloads/cancel-all')
async def cancel_all():
    manager: Optional[DownloadManager] = _state['manager']
    if manager:
        manager.cancel_all_pending()
    return {'status': 'ok'}


@app.post('/api/downloads/clear-finished')
async def clear_finished():
    manager: Optional[DownloadManager] = _state['manager']
    if manager:
        manager.clear_finished()
    return {'status': 'ok'}


# ── Native folder dialog (Windows) ────────────────────────────────────────────
@app.get('/api/select-dir')
async def select_dir():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(title='Select Download Directory')
        root.destroy()
        return {'path': folder or None}
    except Exception:
        return {'path': None, 'error': 'Native dialog unavailable (Docker: type path manually)'}
