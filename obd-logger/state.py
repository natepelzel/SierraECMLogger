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
