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
    LOG_INTERVAL_MS,
    OBD_REQUEST_ID,
    POLL_TIMEOUT_MS,
)
from pids import PIDS, OBD_RESPONSE_ID, build_request


def _active_columns() -> list[str]:
    """Return CSV column list for currently enabled PIDs."""
    return ['timestamp'] + [col for p in PIDS if p.enabled for col in p.csv_columns]


def _open_new_csv() -> tuple[object, csv.DictWriter, list[str]]:
    cols = _active_columns()
    filename = datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '.csv'
    path = LOG_DIR / filename
    f = open(path, 'w', newline='', buffering=1)
    writer = csv.DictWriter(f, fieldnames=cols)
    writer.writeheader()
    return f, writer, cols


def _is_response_to(raw: bytes, pid_obj) -> bool:
    """True if a 0x7E8 frame is the positive response to this specific PID request."""
    if len(raw) < 3 or raw[1] != pid_obj.mode + 0x40:
        return False
    if pid_obj.mode == 0x01:
        return raw[2] == (pid_obj.pid & 0xFF)
    if pid_obj.mode == 0x22:
        return len(raw) >= 4 and raw[2] == (pid_obj.pid >> 8) & 0xFF and raw[3] == pid_obj.pid & 0xFF
    return True


NEGATIVE_RESPONSE = 0x7F
NRC_RESPONSE_PENDING = 0x78
NRC_NAMES = {
    0x11: 'serviceNotSupported',
    0x12: 'subFunctionNotSupported',
    0x13: 'incorrectMessageLength',
    0x22: 'conditionsNotCorrect',
    0x31: 'requestOutOfRange',
    0x33: 'securityAccessDenied',
}

# NRCs meaning the ECM will never answer this PID — retrying is pointless
NRC_UNSUPPORTED = {0x11, 0x12, 0x31}

# (pid name, NRC) pairs already logged — avoids repeating the same rejection every poll
_logged_rejections: set[tuple[str, int]] = set()

# PIDs the ECM rejected as unsupported; skipped until the service restarts.
# Deliberately not written to p.enabled, so the persisted web UI config is untouched.
_unsupported: set[str] = set()


def _should_poll(pid_obj) -> bool:
    return pid_obj.enabled and pid_obj.name not in _unsupported


def _is_rejection_of(raw: bytes, pid_obj) -> bool:
    """True if a 0x7E8 frame is a negative response to this PID's mode.

    Layout: [len, 0x7F, requested_mode, NRC]. Negative responses don't echo the PID,
    so this matches on mode only — safe because only one request is in flight.
    """
    return len(raw) >= 4 and raw[1] == NEGATIVE_RESPONSE and raw[2] == pid_obj.mode


def _log_rejection(pid_obj, nrc: int) -> None:
    if (pid_obj.name, nrc) in _logged_rejections:
        return
    _logged_rejections.add((pid_obj.name, nrc))
    action = 'not polling again until restart' if nrc in NRC_UNSUPPORTED else 'will keep retrying'
    print(
        f'[poller] ECM rejected {pid_obj.name} (mode {pid_obj.mode:02X} PID {pid_obj.pid:#06x}): '
        f'NRC {nrc:#04x} {NRC_NAMES.get(nrc, "unknown")} — {action}',
        file=sys.stderr,
    )


def _drain(reader: can.AsyncBufferedReader) -> None:
    """Discard queued frames, e.g. late responses to requests that already timed out."""
    while True:
        try:
            reader.buffer.get_nowait()
        except asyncio.QueueEmpty:
            return


async def _send_and_recv(bus: can.BusABC, pid_obj, reader: can.AsyncBufferedReader) -> bytes | None:
    """Send an OBD request and await the matching response frame."""
    _drain(reader)
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
        if resp.arbitration_id != OBD_RESPONSE_ID:
            continue
        if _is_response_to(resp.data, pid_obj):
            return bytes(resp.data)
        if _is_rejection_of(resp.data, pid_obj):
            nrc = resp.data[3]
            if nrc == NRC_RESPONSE_PENDING:
                continue  # ECM is still working on it; keep waiting until the deadline
            _log_rejection(pid_obj, nrc)
            if nrc in NRC_UNSUPPORTED:
                _unsupported.add(pid_obj.name)
            return None


def _extract_data_bytes(raw: bytes, pid_obj) -> bytes:
    """Strip the ISO 15765-2 / OBD header bytes, returning only the data payload."""
    if pid_obj.mode == 0x01:
        # Response: [len, 0x41, PID_BYTE, data...]
        return raw[3:]
    elif pid_obj.mode == 0x22:
        # Response: [len, 0x62, PID_HIGH, PID_LOW, data...]
        return raw[4:]
    return raw


MAX_IDLE_SLEEP_S = 0.1


def _seconds_until_next_due(last_polled: dict[str, float], next_row: float) -> float:
    now = time.monotonic()
    waits = [
        last_polled.get(p.name, 0.0) + p.poll_interval_ms / 1000.0 - now
        for p in PIDS if _should_poll(p)
    ]
    return max(0.0, min(waits + [next_row - now, MAX_IDLE_SLEEP_S]))


async def start_poller() -> None:
    last_polled: dict[str, float] = {}
    last_fsync: float = time.monotonic()
    row_interval = LOG_INTERVAL_MS / 1000.0
    next_row: float = time.monotonic()

    csv_file = None
    csv_writer = None
    session_cols: list[str] | None = None

    if state.is_logging:
        csv_file, csv_writer, session_cols = _open_new_csv()

    try:
        # Kernel-side filter: only ECM responses reach the reader, not all HS-CAN broadcast traffic
        bus = can.interface.Bus(
            channel=CAN_INTERFACE,
            interface='socketcan',
            can_filters=[{'can_id': OBD_RESPONSE_ID, 'can_mask': 0x7FF, 'extended': False}],
        )
    except Exception as e:
        print(f'[poller] Failed to open CAN bus: {e}', file=sys.stderr)
        return

    print(f'[poller] sending requests to {OBD_REQUEST_ID:#05x}, listening on {OBD_RESPONSE_ID:#05x}',
          file=sys.stderr)
    reader = can.AsyncBufferedReader()
    notifier = can.Notifier(bus, [reader], loop=asyncio.get_event_loop())

    try:
        while True:
            now = time.monotonic()

            for pid_obj in PIDS:
                if not _should_poll(pid_obj):
                    continue
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

            # Rows are written on a fixed cadence, not per poll — PIDs with different
            # intervals drift out of phase, and writing per poll produced ~90 rows/s.
            row_due = time.monotonic() >= next_row
            if row_due:
                # Advance by whole intervals; if a slow poll put us behind, skip ahead
                # rather than writing a burst of catch-up rows.
                next_row += row_interval
                if next_row <= time.monotonic():
                    next_row = time.monotonic() + row_interval

            if state.is_logging and row_due:
                if csv_writer is None:
                    csv_file, csv_writer, session_cols = _open_new_csv()

                # Build sweep using columns that were active when this session started
                ts = time.time()
                sweep: dict = {'timestamp': ts}
                for col in session_cols[1:]:
                    val = state.latest_values.get(col)
                    sweep[col] = '' if val is None or (isinstance(val, float) and math.isnan(val)) else val

                live_entry = {'ts': ts, **{k: v for k, v in sweep.items() if k != 'timestamp'}}

                csv_writer.writerow(sweep)
                state.live_deque.append(live_entry)
                state.live_seq += 1
                state.new_data_event.set()

                now_mono = time.monotonic()
                if now_mono - last_fsync >= FSYNC_INTERVAL_S:
                    try:
                        os.fsync(csv_file.fileno())
                    except OSError:
                        pass
                    last_fsync = now_mono
            elif not state.is_logging:
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
                    session_cols = None

            # Sleep until the next PID or row is due. Capped so PID config changes from the
            # web UI are picked up promptly; also yields to the web server.
            await asyncio.sleep(_seconds_until_next_due(last_polled, next_row))

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
