import asyncio
import csv
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import state
from config import LOG_DIR, SESSION_DECIMATED_POINTS
from decimation import decimate
from pids import COLUMN_UNITS, PIDS, PIDS_BY_NAME, save_pid_config

app = FastAPI()

_start_time = time.time()

_SAFE_FILENAME = re.compile(r'^[\w\-]+\.csv$')


def _validate_path(filename: str) -> Path:
    if not _SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=400, detail='Invalid filename')
    path = (LOG_DIR / filename).resolve()
    if not str(path).startswith(str(LOG_DIR.resolve())):
        raise HTTPException(status_code=400, detail='Path traversal denied')
    if not path.exists():
        raise HTTPException(status_code=404, detail='Session not found')
    return path


@app.get('/')
async def index():
    return FileResponse('static/index.html')


@app.get('/stream')
async def stream():
    async def event_stream():
        last_seq = state.live_seq
        while True:
            await state.new_data_event.wait()
            state.new_data_event.clear()
            new_count = min(state.live_seq - last_seq, len(state.live_deque))
            last_seq = state.live_seq
            if new_count <= 0:
                continue
            for entry in list(state.live_deque)[-new_count:]:
                yield f'data: {json.dumps(entry)}\n\n'

    return StreamingResponse(event_stream(), media_type='text/event-stream')


@app.get('/status')
async def status():
    return {
        'is_logging': state.is_logging,
        'latest_values': state.latest_values,
        'uptime_seconds': time.time() - _start_time,
    }


@app.post('/logging/start')
async def logging_start():
    state.is_logging = True
    return {'is_logging': True}


@app.post('/logging/stop')
async def logging_stop():
    state.is_logging = False
    return {'is_logging': False}


@app.get('/pids')
async def get_pids():
    return [
        {
            'name': p.name,
            'mode': p.mode,
            'pid': p.pid,
            'unit': p.unit,
            'poll_interval_ms': p.poll_interval_ms,
            'enabled': p.enabled,
            'csv_columns': p.csv_columns,
        }
        for p in PIDS
    ]


class _PidUpdate(BaseModel):
    name: str
    enabled: bool
    poll_interval_ms: int


@app.put('/pids')
async def set_pids(updates: list[_PidUpdate]):
    for u in updates:
        p = PIDS_BY_NAME.get(u.name)
        if p is None:
            continue
        p.enabled = u.enabled
        p.poll_interval_ms = max(10, u.poll_interval_ms)
    save_pid_config()
    return {'ok': True}


def _row_ts(line: bytes) -> float | None:
    """Timestamp from a CSV data row — always the first column."""
    try:
        return float(line.split(b',', 1)[0])
    except ValueError:
        return None


def _session_duration(path: Path) -> float | None:
    """Duration from the first and last data rows only — never reads the whole file."""
    with open(path, 'rb') as f:
        f.readline()  # header
        first_ts = _row_ts(f.readline())
        if first_ts is None:
            return None
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 4096))
        tail = [ln for ln in f.read().splitlines() if ln.strip()]
    last_ts = _row_ts(tail[-1]) if tail else None
    return None if last_ts is None else last_ts - first_ts


# Plain `def` (not async) so FastAPI runs file I/O in its threadpool instead of
# blocking the event loop the CAN poller shares.
@app.get('/sessions')
def list_sessions():
    sessions = []
    for path in sorted(LOG_DIR.glob('*.csv'), reverse=True):
        try:
            stat = path.stat()
            start_time_str = path.stem.replace('_', ' ', 1).replace('-', '/', 2).replace('-', ':')
            duration_seconds = _session_duration(path)
            sessions.append({
                'filename': path.name,
                'start_time': start_time_str,
                'size_bytes': stat.st_size,
                'duration_seconds': duration_seconds,
            })
        except Exception:
            continue
    return sessions


@app.get('/sessions/{filename}')
def load_session(filename: str):
    if filename.endswith('/download'):
        # Shouldn't reach here via normal routing, but guard anyway
        raise HTTPException(status_code=400, detail='Use /download endpoint')
    path = _validate_path(filename)

    timestamps: list[float] = []
    columns: dict[str, list[float]] = {}

    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = [fn for fn in (reader.fieldnames or []) if fn != 'timestamp']
        for fn in fieldnames:
            columns[fn] = []
        for row in reader:
            try:
                ts = float(row['timestamp'])
            except (KeyError, ValueError):
                continue
            timestamps.append(ts)
            for fn in fieldnames:
                raw = row.get(fn, '')
                try:
                    columns[fn].append(float(raw))
                except (ValueError, TypeError):
                    columns[fn].append(float('nan'))

    # Decimate each series independently
    series: dict[str, list[float]] = {}
    shared_ts: list[float] = timestamps  # will be overwritten by first series decimate
    first = True
    for col, values in columns.items():
        t_dec, v_dec = decimate(timestamps, values, SESSION_DECIMATED_POINTS)
        if first:
            shared_ts = t_dec
            first = False
        series[col] = v_dec

    units = {col: COLUMN_UNITS.get(col, '') for col in series}

    return {
        'timestamps': shared_ts,
        'series': series,
        'units': units,
    }


@app.get('/sessions/{filename}/download')
async def download_session(filename: str):
    path = _validate_path(filename)
    return FileResponse(
        path,
        media_type='text/csv',
        filename=filename,
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
