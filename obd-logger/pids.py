import json
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
    enabled: bool = True
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


# ── Additional Mode 01 Parse Functions ───────────────────────────────────────

def parse_engine_load(data: bytes) -> float:
    # PID 0x04 — Calculated engine load
    # Formula: A * 100 / 255  [%]
    return data[0] * 100.0 / 255.0


def parse_fuel_trim(data: bytes) -> float:
    # PID 0x06/0x07/0x08/0x09 — Short/long term fuel trim
    # Formula: (A - 128) * 100 / 128  [%]
    return (data[0] - 128.0) * 100.0 / 128.0


def parse_fuel_pressure(data: bytes) -> float:
    # PID 0x0A — Fuel pressure (gauge)
    # Formula: A * 3  [kPa]
    return float(data[0]) * 3.0


def parse_rpm(data: bytes) -> float:
    # PID 0x0C — Engine RPM
    # Formula: (A*256 + B) / 4  [RPM]
    return ((data[0] * 256) + data[1]) / 4.0


def parse_speed(data: bytes) -> float:
    # PID 0x0D — Vehicle speed
    # Formula: A  [km/h]
    return float(data[0])


def parse_timing_advance(data: bytes) -> float:
    # PID 0x0E — Timing advance
    # Formula: A/2 - 64  [° before TDC]
    return data[0] / 2.0 - 64.0


def parse_run_time(data: bytes) -> float:
    # PID 0x1F — Run time since engine start
    # Formula: A*256 + B  [s]
    return float((data[0] * 256) + data[1])


def parse_fuel_rail_gauge(data: bytes) -> float:
    # PID 0x23 — Fuel rail gauge pressure (diesel/GDI)
    # Formula: (A*256 + B) * 10 kPa → converted to MPa for consistency with mode 22
    return ((data[0] * 256) + data[1]) * 10.0 / 1000.0


def parse_control_voltage(data: bytes) -> float:
    # PID 0x42 — Control module voltage
    # Formula: (A*256 + B) / 1000  [V]
    return ((data[0] * 256) + data[1]) / 1000.0


def parse_abs_load(data: bytes) -> float:
    # PID 0x43 — Absolute load value (normalised air mass per intake stroke)
    # Formula: (A*256 + B) * 100 / 255  [%]
    return ((data[0] * 256) + data[1]) * 100.0 / 255.0


def parse_fuel_rate(data: bytes) -> float:
    # PID 0x5E — Engine fuel rate
    # Formula: (A*256 + B) / 20  [L/h]
    return ((data[0] * 256) + data[1]) / 20.0


def parse_torque_pct(data: bytes) -> float:
    # PID 0x61/0x62 — Driver demand / actual engine torque (% of reference)
    # Formula: A - 125  [%]
    return float(data[0]) - 125.0


def parse_ref_torque(data: bytes) -> float:
    # PID 0x63 — Engine reference torque
    # Formula: A*256 + B  [Nm]
    return float((data[0] * 256) + data[1])


# ── Mode 22 Stub ──────────────────────────────────────────────────────────────
# Returns raw bytes as a float (first byte) until verified on the truck.
# Replace with real scaling after probe.py session.

def parse_stub(data: bytes) -> float:
    # Stub: return raw first byte. Replace after probe session.
    return float(data[0]) if data else float('nan')


# ── PID Registry ─────────────────────────────────────────────────────────────

PIDS: list[Pid] = [

    # ── Standard OBD-II (Mode 01) — active ───────────────────────────────────
    Pid('maf',          0x01, 0x10, parse_maf,      'g/s',  100),
    Pid('iat',          0x01, 0x0F, parse_iat,      '°C',   500),
    Pid('coolant_temp', 0x01, 0x05, parse_coolant,  '°C',   500),
    Pid('map',          0x01, 0x0B, parse_map,      'kPa',  100),
    Pid('baro_sae',     0x01, 0x33, parse_baro_sae, 'kPa',  500),
    Pid('throttle',     0x01, 0x11, parse_throttle, '%',    100),

    # ── Standard OBD-II (Mode 01) — preloaded, off by default ────────────────
    Pid('rpm',                   0x01, 0x0C, parse_rpm,            'RPM',  100,  enabled=False),
    Pid('vehicle_speed',         0x01, 0x0D, parse_speed,          'km/h', 100,  enabled=False),
    Pid('engine_load',           0x01, 0x04, parse_engine_load,    '%',    100,  enabled=False),
    Pid('timing_advance',        0x01, 0x0E, parse_timing_advance, '°',    100,  enabled=False),
    Pid('accel_pedal_d',         0x01, 0x49, parse_throttle,       '%',    100,  enabled=False),
    Pid('rel_throttle',          0x01, 0x45, parse_throttle,       '%',    100,  enabled=False),
    Pid('abs_load',              0x01, 0x43, parse_abs_load,       '%',    100,  enabled=False),
    Pid('driver_demand_torque',  0x01, 0x61, parse_torque_pct,     '%',    100,  enabled=False),
    Pid('actual_torque',         0x01, 0x62, parse_torque_pct,     '%',    100,  enabled=False),
    Pid('ref_torque',            0x01, 0x63, parse_ref_torque,     'Nm',   2000, enabled=False),
    Pid('stft_b1',               0x01, 0x06, parse_fuel_trim,      '%',    500,  enabled=False),
    Pid('ltft_b1',               0x01, 0x07, parse_fuel_trim,      '%',    500,  enabled=False),
    Pid('fuel_pressure',         0x01, 0x0A, parse_fuel_pressure,  'kPa',  100,  enabled=False),
    Pid('fuel_rail_gauge',       0x01, 0x23, parse_fuel_rail_gauge,'MPa',  100,  enabled=False),
    Pid('fuel_rate',             0x01, 0x5E, parse_fuel_rate,      'L/h',  500,  enabled=False),
    Pid('ambient_temp',          0x01, 0x46, parse_iat,            '°C',   1000, enabled=False),
    Pid('oil_temp',              0x01, 0x5C, parse_iat,            '°C',   1000, enabled=False),
    Pid('control_voltage',       0x01, 0x42, parse_control_voltage,'V',    1000, enabled=False),
    Pid('run_time',              0x01, 0x1F, parse_run_time,       's',    1000, enabled=False),

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


# ── Persistent PID Config ─────────────────────────────────────────────────────

def load_pid_config() -> dict:
    """Load per-PID overrides from JSON. Returns {} if not found or invalid."""
    from config import PID_CONFIG_FILE
    try:
        with open(PID_CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_pid_config() -> None:
    """Persist current PIDS enabled/poll_interval_ms to JSON."""
    from config import PID_CONFIG_FILE
    data = {p.name: {'enabled': p.enabled, 'poll_interval_ms': p.poll_interval_ms} for p in PIDS}
    with open(PID_CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def apply_pid_config(overrides: dict) -> None:
    """Apply overrides dict to the PIDS list in-place."""
    for p in PIDS:
        if p.name in overrides:
            ov = overrides[p.name]
            if 'enabled' in ov:
                p.enabled = bool(ov['enabled'])
            if 'poll_interval_ms' in ov:
                p.poll_interval_ms = max(10, int(ov['poll_interval_ms']))


# Apply any persisted configuration on module load
apply_pid_config(load_pid_config())
