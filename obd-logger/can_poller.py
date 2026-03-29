import asyncio
import csv
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import can

import state
from config import (
    CAN_INTERFACE,
    FSYNC_INTERVAL_S,
    LOG_DIR,
    POLL_TIMEOUT_MS,
)
from pids import PIDS, OBD_REQUEST_ID, OBD_RESPONSE_ID, build_request

# All CSV column names in order (timestamp first, then all PID columns)
_ALL_COLUMNS = ['timestamp'] + [col for p in PIDS for col in p.csv_columns]


def _open_new_csv() -> tuple[object, csv.DictWriter]:
    filename = datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '.csv'
    path = LOG_DIR / filename
    f = open(path, 'w', newline='', buffering=1)
    writer = csv.DictWriter(f, fieldnames=_ALL_COLUMNS)
    writer.writeheader()
    return f, writer


async def _send_and_recv(bus: can.BusABC, pid_obj, reader: can.AsyncBufferedReader) -> bytes | None:
    """Send an OBD request and await the matching response frame."""
    request = build_request(pid_obj)
    msg = can.Message(arbitration_id=OBD_REQUEST_ID, data=request, is_extended_id=False)
    try:
        bus.send(msg)
    except can.CanError as e:
        print(f'[poller] CAN send error for {pid_obj.name}: {e}', file=sys.stderr)
        return None

    deadline = time.monotonic() + POLL_TIMEOUT_MS / 1000.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f'[poller] timeout waiting for response to {pid_obj.name}', file=sys.stderr)
            return None
        try:
            resp = await asyncio.wait_for(reader.get_message(), timeout=remaining)
        except asyncio.TimeoutError:
            print(f'[poller] timeout waiting for response to {pid_obj.name}', file=sys.stderr)
            return None
        if resp.arbitration_id == OBD_RESPONSE_ID:
            return bytes(resp.data)


def _extract_data_bytes(raw: bytes, pid_obj) -> bytes:
    """Strip the ISO 15765-2 / OBD header bytes, returning only the data payload."""
    if pid_obj.mode == 0x01:
        # Response: [len, 0x41, PID_BYTE, data...]
        return raw[3:]
    elif pid_obj.mode == 0x22:
        # Response: [len, 0x62, PID_HIGH, PID_LOW, data...]
        return raw[4:]
    return raw


async def start_poller() -> None:
    last_polled: dict[str, float] = {}
    last_fsync: float = time.monotonic()

    csv_file = None
    csv_writer = None

    if state.is_logging:
        csv_file, csv_writer = _open_new_csv()

    try:
        bus = can.interface.Bus(channel=CAN_INTERFACE, bustype='socketcan')
    except Exception as e:
        print(f'[poller] Failed to open CAN bus: {e}', file=sys.stderr)
        return

    reader = can.AsyncBufferedReader()
    notifier = can.Notifier(bus, [reader], loop=asyncio.get_event_loop())

    try:
        while True:
            now = time.monotonic()

            for pid_obj in PIDS:
                last = last_polled.get(pid_obj.name, 0.0)
                if (now - last) * 1000 < pid_obj.poll_interval_ms:
                    continue

                raw = await _send_and_recv(bus, pid_obj, reader)
                last_polled[pid_obj.name] = time.monotonic()

                if raw is None:
                    continue

                data = _extract_data_bytes(raw, pid_obj)
                try:
                    result = pid_obj.parse_fn(data)
                except Exception as e:
                    print(f'[poller] parse error for {pid_obj.name}: {e}', file=sys.stderr)
                    continue

                # Store results — multi-value PIDs (e.g. inj_balance) return a list
                if isinstance(result, list):
                    for col, val in zip(pid_obj.csv_columns, result):
                        state.latest_values[col] = val
                else:
                    state.latest_values[pid_obj.name] = result

            # Build sweep dict using latest known values for every column
            ts = time.time()
            sweep: dict = {'timestamp': ts}
            for col in _ALL_COLUMNS[1:]:
                val = state.latest_values.get(col)
                sweep[col] = '' if val is None or (isinstance(val, float) and math.isnan(val)) else val

            # Also store under the 'ts' key for the live deque (used by SSE)
            live_entry = {'ts': ts, **{k: v for k, v in sweep.items() if k != 'timestamp'}}

            if state.is_logging:
                if csv_writer is None:
                    csv_file, csv_writer = _open_new_csv()

                csv_writer.writerow(sweep)
                state.live_deque.append(live_entry)
                state.new_data_event.set()

                now_mono = time.monotonic()
                if now_mono - last_fsync >= FSYNC_INTERVAL_S:
                    try:
                        os.fsync(csv_file.fileno())
                    except OSError:
                        pass
                    last_fsync = now_mono
            else:
                # Not logging — close any open CSV, keep polling for latest_values
                if csv_file is not None:
                    try:
                        csv_file.flush()
                        os.fsync(csv_file.fileno())
                    except OSError:
                        pass
                    csv_file.close()
                    csv_file = None
                    csv_writer = None

            # Small yield to allow other coroutines to run between sweeps
            await asyncio.sleep(0)

    finally:
        notifier.stop()
        bus.shutdown()
        if csv_file is not None:
            try:
                csv_file.flush()
                os.fsync(csv_file.fileno())
            except OSError:
                pass
            csv_file.close()


def open_new_log() -> None:
    """Called from server.py when /logging/start is hit — resets CSV state via module globals."""
    # The poller loop itself detects state.is_logging=True and opens a new file.
    # This function exists so server.py has a clear hook if additional logic is needed.
    pass
