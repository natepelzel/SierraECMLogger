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
