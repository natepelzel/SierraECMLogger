from pathlib import Path

CAN_INTERFACE             = 'can0'
CAN_BITRATE               = 500_000     # LMM Duramax HS-CAN

# 0x7DF = functional (broadcast to all emissions ECUs); 0x7E0 = physical (ECM only).
# GM mode 22 PIDs are conventionally requested physically, and ECUs may stay silent
# instead of sending a negative response to functional requests.
OBD_REQUEST_ID            = 0x7DF

LOG_DIR                   = Path(__file__).parent / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

PID_CONFIG_FILE           = Path(__file__).parent / 'pid_config.json'

LIVE_WINDOW_SECONDS       = 60          # Rolling window shown in live chart
LIVE_DEQUE_MAXLEN         = 600         # ~10 min of sweeps at ~1Hz
SESSION_DECIMATED_POINTS  = 1_500       # Target points per series for historical view
POLL_TIMEOUT_MS           = 500         # Max wait for ECM response per PID
FSYNC_INTERVAL_S          = 30          # How often to flush CSV to disk
