from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Pid:
    name: str                        # Human-readable label, used as CSV column header
    mode: int                        # 0x01 (SAE) or 0x22 (GM extended)
    pid: int                         # e.g. 0x10 for mode 01, 0x2049 for mode 22
    parse_fn: Callable[[bytes], 'float | list[float]']
    unit: str
    poll_interval_ms: int
    csv_columns: list[str] = field(default_factory=list)
    # Single-value PIDs: csv_columns defaults to [name]
    # Injector balance rates: csv_columns = ['inj_bal_cyl1', ..., 'inj_bal_cyl8']

    def __post_init__(self):
        if not self.csv_columns:
            self.csv_columns = [self.name]


# ── Request Frame Construction ────────────────────────────────────────────────

OBD_REQUEST_ID = 0x7DF
OBD_RESPONSE_ID = 0x7E8


def build_request(pid: Pid) -> bytes:
    if pid.mode == 0x01:
        return bytes([0x02, 0x01, pid.pid & 0xFF, 0, 0, 0, 0, 0])
    elif pid.mode == 0x22:
        return bytes([0x03, 0x22, (pid.pid >> 8) & 0xFF, pid.pid & 0xFF, 0, 0, 0, 0])
    raise ValueError(f'Unsupported mode: {pid.mode:#x}')


# ── SAE J1979 Mode 01 Parse Functions ────────────────────────────────────────
# Response layout: [len, 0x41, PID_BYTE, data...]
# data bytes start at index 3 in the full frame (index 0 in the slice passed here)

def parse_maf(data: bytes) -> float:
    # PID 0x10 — MAF air flow rate
    # Formula: ((A*256)+B) / 100  [g/s]
    return ((data[0] * 256) + data[1]) / 100.0


def parse_iat(data: bytes) -> float:
    # PID 0x0F — Intake Air Temperature
    # Formula: A - 40  [°C]
    return data[0] - 40.0


def parse_coolant(data: bytes) -> float:
    # PID 0x05 — Engine Coolant Temperature
    # Formula: A - 40  [°C]
    return data[0] - 40.0


def parse_map(data: bytes) -> float:
    # PID 0x0B — Intake Manifold Absolute Pressure
    # Formula: A  [kPa]
    return float(data[0])


def parse_baro_sae(data: bytes) -> float:
    # PID 0x33 — Barometric Pressure
    # Formula: A  [kPa]
    return float(data[0])


def parse_throttle(data: bytes) -> float:
    # PID 0x11 — Throttle Position
    # Formula: (A * 100) / 255  [%]
    return (data[0] * 100.0) / 255.0


# ── Mode 22 Stub ──────────────────────────────────────────────────────────────
# Returns raw bytes as a float (first byte) until verified on the truck.
# Replace with real scaling after probe.py session.

def parse_stub(data: bytes) -> float:
    # Stub: return raw first byte. Replace after probe session.
    return float(data[0]) if data else float('nan')


# ── PID Registry ─────────────────────────────────────────────────────────────

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

# Build a lookup: pid_name -> Pid, and also column_name -> Pid + unit for server
PIDS_BY_NAME: dict[str, Pid] = {p.name: p for p in PIDS}

# Map every CSV column to its unit for the session API response
COLUMN_UNITS: dict[str, str] = {}
for _p in PIDS:
    for _col in _p.csv_columns:
        COLUMN_UNITS[_col] = _p.unit
