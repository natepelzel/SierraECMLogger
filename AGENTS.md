# OBD Logger — Software Build Plan

## Context

Building a long-duration OBD-II data logger for a 2008 GMC Sierra 3500 (LMM Duramax 6.6L diesel,
E38 ECM) to diagnose intermittent limp mode events suspected to be correlated with ambient
barometric pressure. The logger runs on a Raspberry Pi 3B+ connected to the truck's OBD-II port
via direct CAN wiring (MCP2515 + TJA1050 module over SPI) — no ELM327 adapter.

The Pi speaks raw CAN frames directly. OBD-II is implemented manually: request frames constructed
per SAE J1979, responses filtered by CAN ID. This gives access to both standard mode 01 PIDs and
GM proprietary mode 22 PIDs.

---

## Hardware Context (for reference only — already configured)

- **Pi 3B+** — CAN interface `can0` at 500kbps via MCP2515 over SPI
- **WiFi AP** — Pi broadcasts SSID `OBD-Logger`, static IP `192.168.4.1`. Phone connects directly,
  no router needed. Dashboard at `http://192.168.4.1:8000`
- **Storage** — Samsung Pro Endurance SD card. All log files stored locally on the Pi.
- **OS tweaks** — `/tmp` and `/var/log` mounted as `tmpfs` to reduce SD card wear. Already
  configured in `/etc/fstab` — no action needed from the software.

---

## Stack

- **Language:** Python 3
- **Web framework:** FastAPI + Uvicorn
- **CAN:** `python-can` (asyncio interface — `AsyncBufferedReader` / `Notifier`)
- **Decimation:** `lttbc` + `numpy`
- **Frontend:** Single `index.html`, no build step — uPlot from CDN
- **Storage:** CSV files, one per session, timestamped filename, stored locally on SD

---

## Project Structure

```
obd-logger/
├── main.py            # Entrypoint — wires asyncio tasks for poller + web server
├── can_poller.py      # CAN poll loop, CSV writer, shared state updates
├── server.py          # FastAPI — SSE, session API, logging control, static file
├── pids.py            # Pid dataclass, PID registry, frame builder, response parser
├── config.py          # All tuneable constants
├── decimation.py      # LTTB wrapper for historical session downsampling
├── state.py           # Shared mutable state (deque, is_logging flag, latest values)
├── static/
│   └── index.html     # Single-file frontend — uPlot, live tab, sessions tab
├── logs/              # CSV sessions written here, auto-created on startup
├── requirements.txt
└── obd-logger.service # systemd unit
```

---

## `requirements.txt`

```
python-can
fastapi
uvicorn[standard]
lttbc
numpy
```

---

## `config.py`

```python
from pathlib import Path

CAN_INTERFACE             = 'can0'
CAN_BITRATE               = 500_000     # LMM Duramax HS-CAN

LOG_DIR                   = Path(__file__).parent / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

LIVE_WINDOW_SECONDS       = 60          # Rolling window shown in live chart
LIVE_DEQUE_MAXLEN         = 600         # ~10 min of sweeps at ~1Hz
SESSION_DECIMATED_POINTS  = 1_500       # Target points per series for historical view
POLL_TIMEOUT_MS           = 500         # Max wait for ECM response per PID
FSYNC_INTERVAL_S          = 30          # How often to flush CSV to disk
```

---

## `state.py`

Centralises all shared mutable state so `can_poller` and `server` import from one place
with no circular dependencies.

```python
import asyncio
import collections
from typing import Any

# Rolling buffer of poll sweep dicts: [{'ts': float, pid_name: value, ...}, ...]
live_deque: collections.deque = collections.deque(maxlen=600)

# Latest known value per PID name — always reflects most recent successful parse
latest_values: dict[str, Any] = {}

# Logging control — True on boot, togglable via API
is_logging: bool = True

# Asyncio event — set whenever a new sweep is appended to live_deque.
# SSE handler waits on this instead of busy-polling with sleep().
new_data_event: asyncio.Event = asyncio.Event()
```

---

## `pids.py`

### Pid Dataclass

```python
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class Pid:
    name: str                        # Human-readable label, used as CSV column header
    mode: int                        # 0x01 (SAE) or 0x22 (GM extended)
    pid: int                         # e.g. 0x10 for mode 01, 0x2049 for mode 22
    parse_fn: Callable[[bytes], float | list[float]]
    unit: str
    poll_interval_ms: int
    csv_columns: list[str] = field(default_factory=list)
    # Single-value PIDs: csv_columns defaults to [name]
    # Injector balance rates: csv_columns = ['inj_bal_cyl1', ..., 'inj_bal_cyl8']

    def __post_init__(self):
        if not self.csv_columns:
            self.csv_columns = [self.name]
```

### Request Frame Construction

OBD-II broadcast request CAN ID: `0x7DF`

```python
def build_request(pid: Pid) -> bytes:
    if pid.mode == 0x01:
        return bytes([0x02, 0x01, pid.pid & 0xFF, 0, 0, 0, 0, 0])
    elif pid.mode == 0x22:
        return bytes([0x03, 0x22, (pid.pid >> 8) & 0xFF, pid.pid & 0xFF, 0, 0, 0, 0])
    raise ValueError(f'Unsupported mode: {pid.mode:#x}')
```

### Response Parsing

ECM response CAN ID: `0x7E8`

- **Mode 01** response layout: `[len, 0x41, PID_BYTE, data...]` — data starts at index 2
- **Mode 22** response layout: `[len, 0x62, PID_HIGH, PID_LOW, data...]` — data starts at index 4

Implement standard SAE J1979 parse functions for all mode 01 PIDs.

Mode 22 parse functions are **stubs returning raw bytes** until verified on the truck via
`probe.py` (see Build Order step 1). Fill in correct byte scaling after probe session.

### PID Registry

```python
PIDS: list[Pid] = [

    # ── Standard OBD-II (Mode 01) ─────────────────────────────────────────────
    Pid('maf',          0x01, 0x10, parse_maf,      'g/s',  100),
    Pid('iat',          0x01, 0x0F, parse_iat,      '°C',   500),
    Pid('coolant_temp', 0x01, 0x05, parse_coolant,  '°C',   500),
    Pid('map',          0x01, 0x0B, parse_map,      'kPa',  100),
    Pid('baro_sae',     0x01, 0x33, parse_baro_sae, 'kPa',  500),
    Pid('throttle',     0x01, 0x11, parse_throttle, '%',    100),

    # ── GM Extended (Mode 22) — ALL UNVERIFIED until probe.py is run ──────────
    # Verify hex values against LMM-specific Torque Pro PID files / EFI Live
    # forums before treating any of these as correct.
    Pid('boost_actual',      0x22, 0x2049, parse_stub, 'kPa', 100),
    Pid('boost_desired',     0x22, 0x2048, parse_stub, 'kPa', 100),

    # Vane position: directionality (0% vs 100% = open vs closed) must be
    # confirmed empirically during probe session — see Build Order step 1.
    Pid('vane_actual',       0x22, 0x20C8, parse_stub, '%',   100),
    Pid('vane_desired',      0x22, 0x20C7, parse_stub, '%',   100),

    Pid('fuel_rail_actual',  0x22, 0x2A0A, parse_stub, 'MPa', 100),
    Pid('fuel_rail_desired', 0x22, 0x2A09, parse_stub, 'MPa', 100),

    # ECU internal baro — may be a calculated value rather than a raw sensor read.
    # Compare vs baro_sae during probe. If they track identically, this can be dropped.
    Pid('baro_ecu',          0x22, 0x2A0F, parse_stub, 'kPa', 500),

    # Injector balance rates — PID hex TBD, research LMM-specific sources first.
    # Poll at 2000ms (ECM updates on a slow internal cycle).
    # parse_fn returns list[float] of 8 values, one per cylinder.
    Pid('inj_balance', 0x22, 0x0000, parse_stub, 'mg', 2000,
        csv_columns=['inj_bal_cyl1', 'inj_bal_cyl2', 'inj_bal_cyl3', 'inj_bal_cyl4',
                     'inj_bal_cyl5', 'inj_bal_cyl6', 'inj_bal_cyl7', 'inj_bal_cyl8']),
]
```

---

## `can_poller.py`

Async task started from `main.py` via `asyncio.create_task()`.

### Poll Loop

```
for each Pid in PIDS:
    if not due (now - last_polled[pid.name] < pid.poll_interval_ms):
        continue
    send build_request(pid) on CAN ID 0x7DF
    await response with POLL_TIMEOUT_MS timeout, filter on CAN ID 0x7E8
    on timeout: log warning to stderr, continue — never crash the loop
    parse response bytes → value(s)
    update state.latest_values[pid.name]
    update last_polled[pid.name]

after iterating all PIDs (one sweep):
    build sweep dict: {'ts': time.time(), **state.latest_values}
    if state.is_logging:
        append row to open CSV
        append sweep dict to state.live_deque
        state.new_data_event.set()
        if time since last fsync >= FSYNC_INTERVAL_S:
            os.fsync(csv_file.fileno())
```

### CSV Behaviour

- New file created each time logging is started: `logs/YYYY-MM-DD_HH-MM-SS.csv`
- Header row written on file creation — all `csv_columns` values across all PIDs, plus `timestamp`
- One row per sweep — carry forward last known value for PIDs not updated this sweep
- `fsync` every `FSYNC_INTERVAL_S` seconds — protects against sudden power loss
- When `is_logging = False`: stop writing CSV and updating deque, but keep polling CAN
  so `state.latest_values` stays current

### Logging State Transitions

- Starts `True` on boot
- `POST /logging/stop` → `state.is_logging = False`, close current CSV file handle
- `POST /logging/start` → `state.is_logging = True`, open new timestamped CSV file

---

## `server.py`

FastAPI application. Import shared state from `state.py`.

### Routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serve `static/index.html` |
| `GET` | `/stream` | SSE — streams live sweep dicts as JSON events |
| `GET` | `/status` | `{is_logging, latest_values, uptime_seconds}` |
| `POST` | `/logging/start` | Set `is_logging = True`, open new CSV |
| `POST` | `/logging/stop` | Set `is_logging = False`, close CSV |
| `GET` | `/sessions` | List CSV files with metadata |
| `GET` | `/sessions/{filename}` | Load + decimate session, return JSON for charting |
| `GET` | `/sessions/{filename}/download` | Serve raw CSV as file download |

### SSE (`/stream`)

```python
async def event_stream():
    last_len = len(state.live_deque)
    while True:
        await state.new_data_event.wait()
        state.new_data_event.clear()
        current = list(state.live_deque)
        for entry in current[last_len:]:
            yield f'data: {json.dumps(entry)}\n\n'
        last_len = len(current)
```

Use `StreamingResponse` with `media_type='text/event-stream'`.

### Session List (`/sessions`)

Return list of objects:
```json
[
  {
    "filename": "2024-11-15_14-23-05.csv",
    "start_time": "2024-11-15 14:23:05",
    "size_bytes": 487200,
    "duration_seconds": 3840
  }
]
```

Duration: derive from first and last timestamp rows only — do not load the full file.

### Session Load (`/sessions/{filename}`)

1. Validate filename is within `LOG_DIR` (prevent path traversal)
2. Parse full CSV with `csv.DictReader`
3. For each PID column: run LTTB decimation independently via `decimation.py`,
   using the `timestamp` column as the shared X axis for all series
4. Return:
```json
{
  "timestamps": [1700000000.0, "..."],
  "series": {
    "boost_actual": [14.2, "..."],
    "baro_sae": [98.0, "..."]
  },
  "units": {
    "boost_actual": "kPa",
    "baro_sae": "kPa"
  }
}
```

### Session Download (`/sessions/{filename}/download`)

```python
from fastapi.responses import FileResponse

@app.get('/sessions/{filename}/download')
async def download_session(filename: str):
    path = validate_path(LOG_DIR / filename)  # must confirm path stays within LOG_DIR
    return FileResponse(
        path,
        media_type='text/csv',
        filename=filename,
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )
```

The browser handles the actual download — no frontend JS needed beyond a plain
`<a href="/sessions/{filename}/download" download>` link.

---

## `decimation.py`

```python
import numpy as np
import lttbc

def decimate(
    timestamps: list[float],
    values: list[float],
    target_points: int,
) -> tuple[list[float], list[float]]:
    if len(timestamps) <= target_points:
        return timestamps, values
    t = np.array(timestamps, dtype=np.float64)
    v = np.array(values, dtype=np.float64)
    # Fill NaN (missing values from skipped poll cycles) via linear interpolation
    # before decimating so LTTB doesn't choke on gaps
    mask = np.isnan(v)
    if mask.any():
        v[mask] = np.interp(t[mask], t[~mask], v[~mask])
    t_out, v_out = lttbc.downsample(t, v, target_points)
    return t_out.tolist(), v_out.tolist()
```

---

## `static/index.html`

Single file, no build step.

### CDN Imports

```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/uplot@1.6.24/dist/uPlot.min.css">
<script src="https://cdn.jsdelivr.net/npm/uplot@1.6.24/dist/uPlot.iife.min.js"></script>
```

### Layout

```
┌──────────────────────────────────────────────────────┐
│  ● LOGGING  [Stop Logging]                            │  ← header
│  boost: 14.2 kPa  baro(SAE): 98  baro(ECU): 97       │
│  vane: 42%                                            │
├──────────────────────────────────────────────────────┤
│  [ Live ]  [ Sessions ]                               │  ← tabs
├──────────────────────────────────────────────────────┤
│                                                       │
│   uPlot chart (fills remaining viewport height)       │
│                                                       │
│  ☑ boost_actual  ☑ boost_desired  ☑ baro_sae         │  ← series toggles
│  ☑ vane_actual   ☐ coolant_temp   ☐ iat  ...         │
└──────────────────────────────────────────────────────┘
```

### Header Bar

- Status indicator: green dot + `LOGGING` or red dot + `PAUSED`
- Toggle button: calls `POST /logging/stop` or `/logging/start`, then refreshes `/status`
- Summary chips for key diagnostic PIDs: `boost_actual`, `baro_sae`, `baro_ecu`, `vane_actual`
  — updated on every SSE event
- On page load: `GET /status` to set initial button and indicator state

### Live Tab

- uPlot instance with `LIVE_WINDOW_SECONDS` rolling window
- `EventSource('/stream')` opened on tab activation, closed on tab switch
- On each SSE message:
  - Parse JSON entry
  - Append values to per-series arrays
  - Trim arrays to window length
  - Call `uplot.setData(data)` — never recreate the instance on each update
- Series toggles: call `uplot.setSeries(seriesIndex, {show: bool})`
- Chart width: `window.innerWidth`, update on `window.resize`

### Sessions Tab

**List view:**
- Fetch `/sessions` on tab open
- Render as card list: filename, start time, duration, file size
- Each card has two actions:
  - **Download** — plain `<a href="/sessions/{filename}/download" download>` tag, no JS needed
  - **View** — loads and renders the session chart

**Chart view:**
- Fetch `/sessions/{filename}` → decimated JSON
- Destroy existing uPlot instance, create new one with returned data
- uPlot handles zoom, pan, and cursor scrubbing natively
- `← Back` button returns to list view
- Same series toggle controls as live tab

### uPlot Configuration Notes

- One uPlot instance per tab; destroy and recreate on session load or tab switch
- All timestamps as Unix epoch float seconds — uPlot expects this natively
- Group related series on shared Y axes where units match:
  - Boost actual + desired → shared Y axis (kPa)
  - Vane actual + desired → shared Y axis (%)
  - Baro SAE + baro ECU → shared Y axis (kPa)
  - MAP on its own axis or shared with baro depending on scale
- Mobile-first: full-width chart, touch-friendly uPlot defaults

---

## `main.py`

```python
import asyncio
import uvicorn
from can_poller import start_poller
from server import app

async def main():
    asyncio.create_task(start_poller())
    config = uvicorn.Config(app, host='0.0.0.0', port=8000, log_level='info')
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == '__main__':
    asyncio.run(main())
```

---

## `obd-logger.service`

```ini
[Unit]
Description=OBD Logger
After=network.target

[Service]
ExecStartPre=/sbin/ip link set can0 up type can bitrate 500000
ExecStart=/home/pi/obd-env/bin/python /home/pi/obd-logger/main.py
WorkingDirectory=/home/pi/obd-logger
Restart=on-failure
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

Install:
```bash
sudo cp obd-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable obd-logger
sudo systemctl start obd-logger
```

---

## Build Order

### Step 1 — `probe.py` (throwaway script, not part of final project)

**Run this on the actual truck before writing anything else.**

Write a standalone script that:
1. Brings up `can0` at 500kbps
2. Iterates every PID in the planned list (both mode 01 and mode 22)
3. Sends each request frame, awaits response with timeout
4. Prints raw response bytes + attempted parse for anything that responds
5. Clearly marks non-responding PIDs

This session must confirm:
- Which mode 22 PIDs actually respond on this ECU (treat all as unverified until proven)
- Raw byte layout for each responding mode 22 PID → needed to write correct parse functions
- **Vane position directionality:** at idle note `vane_actual` value, snap throttle briefly,
  observe direction of change vs `vane_desired`. Document whether 100% = open or closed.
  This determines dashboard axis labeling.
- **`baro_ecu` vs `baro_sae`:** if they track identically, `baro_ecu` is redundant and
  should be removed from the PID list. If they diverge, log both.
- Injector balance rate PID hex values — research LMM sources (EFI Live forum, Torque Pro
  LMM PID files) beforehand and verify response here.

### Step 2 — `config.py` + `pids.py`

Fill in all constants. Replace mode 22 parse stubs with real implementations based on
probe output. Confirm or update `inj_balance` PID hex value.

### Step 3 — `state.py` + `can_poller.py`

Core poll loop and CSV writer. Test by tailing the active log file while connected to the
truck:
```bash
tail -f logs/$(ls -t logs/ | head -1)
```
Verify: rows appearing at expected rate, no timeout floods, fsync firing every 30s,
no crashes on intermittent CAN errors.

### Step 4 — `server.py`

API only — no frontend yet. Verify all routes with curl:
```bash
curl http://192.168.4.1:8000/status
curl http://192.168.4.1:8000/sessions
curl -N http://192.168.4.1:8000/stream
curl -OJ http://192.168.4.1:8000/sessions/2024-11-15_14-23-05.csv/download
```

### Step 5 — `decimation.py`

Unit test with synthetic data before wiring into server. Confirm LTTB preserves shape at
aggressive ratios (e.g. 36,000 points → 1,500). Confirm NaN interpolation handles gaps
without crashing.

### Step 6 — `static/index.html`

Live tab first — verify SSE rendering on a real phone browser before touching the sessions
tab. Then: sessions list view → sessions chart view → download link.

### Step 7 — `obd-logger.service` + hardening

- Install and enable service
- Power cycle — confirm dashboard reachable within ~20s of boot
- Kill -9 the process mid-session, confirm CSV is intact to within last `FSYNC_INTERVAL_S`
- Confirm `is_logging` starts `True` after service restart (correct default behaviour)
