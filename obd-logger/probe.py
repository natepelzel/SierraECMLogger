#!/usr/bin/env python3
"""
probe.py — Throwaway diagnostic script (Build Order Step 1).

Run this on the truck BEFORE building anything else.

Purpose:
  - Verify which mode 01 and mode 22 PIDs actually respond on this ECU
  - Print raw response bytes + attempted parse for each responding PID
  - Mark non-responding PIDs clearly
  - Confirm vane position directionality (0% = open or closed?)
  - Compare baro_ecu vs baro_sae to decide if baro_ecu is redundant
  - Confirm injector balance rate PID hex values

Usage:
  sudo python3 probe.py

  The script brings up can0 at 500 kbps itself via subprocess — run as root
  or ensure the pi user has CAP_NET_ADMIN (or pre-configure can0 in systemd).

After running, fill in real parse functions in pids.py and update any
mode 22 PID hex values that turned out to be wrong.
"""

import asyncio
import subprocess
import sys
import time

try:
    import can
except ImportError:
    sys.exit('python-can not installed. Run: pip install python-can')

CAN_INTERFACE   = 'can0'
CAN_BITRATE     = 500_000
TIMEOUT_S       = 0.5       # per-PID response timeout
OBD_REQUEST_ID  = 0x7DF
OBD_RESPONSE_ID = 0x7E8

# ── PID list to probe ─────────────────────────────────────────────────────
# (name, mode, pid_int)
PROBE_PIDS = [
    # Mode 01 — standard OBD-II
    ('maf',               0x01, 0x10),
    ('iat',               0x01, 0x0F),
    ('coolant_temp',      0x01, 0x05),
    ('map',               0x01, 0x0B),
    ('baro_sae',          0x01, 0x33),
    ('throttle',          0x01, 0x11),

    # Mode 22 — GM extended (UNVERIFIED — treat all as suspect until confirmed)
    ('boost_actual',      0x22, 0x2049),
    ('boost_desired',     0x22, 0x2048),
    ('vane_actual',       0x22, 0x20C8),
    ('vane_desired',      0x22, 0x20C7),
    ('fuel_rail_actual',  0x22, 0x2A0A),
    ('fuel_rail_desired', 0x22, 0x2A09),
    ('baro_ecu',          0x22, 0x2A0F),
    # Injector balance — PID TBD, placeholder 0x0000 will not respond
    # Research correct PID from EFI Live forum / Torque Pro LMM PID files
    # then update this line before running again.
    ('inj_balance',       0x22, 0x0000),
]

# ── Mode 01 parse functions (same formulas as pids.py) ────────────────────

def parse_mode01(name: str, data: bytes) -> str:
    try:
        if name == 'maf':
            return f'{((data[0] * 256) + data[1]) / 100.0:.2f} g/s'
        if name == 'iat':
            return f'{data[0] - 40} °C'
        if name == 'coolant_temp':
            return f'{data[0] - 40} °C'
        if name == 'map':
            return f'{data[0]} kPa'
        if name == 'baro_sae':
            return f'{data[0]} kPa'
        if name == 'throttle':
            return f'{(data[0] * 100.0) / 255.0:.1f} %'
    except (IndexError, ZeroDivisionError):
        pass
    return '(parse failed)'


# ── Request frame builder ─────────────────────────────────────────────────

def build_request(mode: int, pid: int) -> bytes:
    if mode == 0x01:
        return bytes([0x02, 0x01, pid & 0xFF, 0, 0, 0, 0, 0])
    elif mode == 0x22:
        return bytes([0x03, 0x22, (pid >> 8) & 0xFF, pid & 0xFF, 0, 0, 0, 0])
    raise ValueError(f'Unsupported mode {mode:#x}')


# ── CAN interface setup ───────────────────────────────────────────────────

def setup_can():
    print(f'[probe] Bringing up {CAN_INTERFACE} at {CAN_BITRATE} bps …')
    try:
        subprocess.run(
            ['ip', 'link', 'set', CAN_INTERFACE, 'down'],
            check=False, capture_output=True,
        )
        subprocess.run(
            ['ip', 'link', 'set', CAN_INTERFACE, 'up', 'type', 'can', 'bitrate', str(CAN_BITRATE)],
            check=True, capture_output=True,
        )
        print(f'[probe] {CAN_INTERFACE} up.\n')
    except subprocess.CalledProcessError as e:
        print(f'[probe] WARNING: could not bring up {CAN_INTERFACE}: {e.stderr.decode().strip()}')
        print('[probe] Assuming interface is already configured and continuing …\n')


# ── Main probe loop ───────────────────────────────────────────────────────

async def probe():
    setup_can()

    try:
        bus = can.interface.Bus(channel=CAN_INTERFACE, bustype='socketcan')
    except Exception as e:
        sys.exit(f'[probe] Failed to open CAN bus: {e}')

    reader = can.AsyncBufferedReader()
    notifier = can.Notifier(bus, [reader], loop=asyncio.get_event_loop())

    responding = []
    no_response = []

    print(f'{"PID":<22} {"MODE":>6}  {"HEX":>6}  {"RAW BYTES":<30}  PARSED')
    print('─' * 90)

    for name, mode, pid in PROBE_PIDS:
        request = build_request(mode, pid)
        msg = can.Message(arbitration_id=OBD_REQUEST_ID, data=request, is_extended_id=False)

        try:
            bus.send(msg)
        except can.CanError as e:
            print(f'{name:<22}  SEND ERROR: {e}')
            no_response.append(name)
            continue

        raw = None
        deadline = time.monotonic() + TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                resp = await asyncio.wait_for(reader.get_message(), timeout=remaining)
                if resp.arbitration_id == OBD_RESPONSE_ID:
                    raw = bytes(resp.data)
                    break
            except asyncio.TimeoutError:
                break

        if raw is None:
            no_response.append(name)
            print(f'{name:<22}  mode={mode:#04x}  pid={pid:#06x}  NO RESPONSE')
            continue

        raw_hex = ' '.join(f'{b:02X}' for b in raw)

        if mode == 0x01:
            data = raw[3:]   # strip [len, 0x41, pid_byte]
            parsed = parse_mode01(name, data)
        else:
            data = raw[4:]   # strip [len, 0x62, pid_high, pid_low]
            parsed = f'raw_data={data.hex(" ")}  (STUB — fill in real parse after inspection)'

        print(f'{name:<22}  mode={mode:#04x}  pid={pid:#06x}  [{raw_hex:<29}]  {parsed}')
        responding.append(name)

        # Small gap between requests to avoid flooding the ECM
        await asyncio.sleep(0.05)

    notifier.stop()
    bus.shutdown()

    print()
    print('═' * 90)
    print(f'RESPONDING ({len(responding)}): {", ".join(responding) or "none"}')
    print(f'NO RESPONSE ({len(no_response)}): {", ".join(no_response) or "none"}')
    print()
    print('Next steps:')
    print('  1. For each responding mode 22 PID, note the raw data bytes above.')
    print('  2. Determine byte layout / scaling from EFI Live docs or empirical testing.')
    print('  3. Replace parse_stub() calls in pids.py with real implementations.')
    print('  4. Run probe.py again after an engine warm-up to capture live values.')
    print('  5. Snap-throttle briefly to check vane_actual directionality.')
    print('  6. Compare baro_sae vs baro_ecu — if identical, remove baro_ecu from PIDS.')
    print('  7. Research correct inj_balance PID hex (currently 0x0000 placeholder).')


if __name__ == '__main__':
    asyncio.run(probe())
