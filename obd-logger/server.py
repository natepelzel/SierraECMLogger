import asyncio
import csv
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import state
from config import LOG_DIR, SESSION_DECIMATED_POINTS
from decimation import decimate
from pids import COLUMN_UNITS

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
        last_len = len(state.live_deque)
        while True:
            await state.new_data_event.wait()
            state.new_data_event.clear()
            current = list(state.live_deque)
            for entry in current[last_len:]:
                yield f'data: {json.dumps(entry)}\n\n'
            last_len = len(current)

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


@app.get('/sessions')
async def list_sessions():
    sessions = []
    for path in sorted(LOG_DIR.glob('*.csv'), reverse=True):
        try:
            stat = path.stat()
            start_time_str = path.stem.replace('_', ' ', 1).replace('-', '/', 2).replace('-', ':')
            # Derive duration from first and last timestamp rows only
            duration_seconds = None
            with open(path, newline='') as f:
                reader = csv.DictReader(f)
                first_ts = None
                last_ts = None
                for row in reader:
                    ts_val = row.get('timestamp', '')
                    if ts_val:
                        try:
                            ts = float(ts_val)
                            if first_ts is None:
                                first_ts = ts
                            last_ts = ts
                        except ValueError:
                            pass
            if first_ts is not None and last_ts is not None:
                duration_seconds = last_ts - first_ts
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
async def load_session(filename: str):
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
