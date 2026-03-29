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
