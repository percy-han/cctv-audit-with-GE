"""Linux cgroup CPU quota & `/proc/self/schedstat` runqueue wait diagnostics.

Adopted from the battle-tested `percy-han/cctv-audit` Phase 0/Phase 2 instrumentation:
- `cpu_quota()` reads cgroup v2 (`/sys/fs/cgroup/cpu.max`) or cgroup v1 (`cpu.cfs_quota_us`)
  to report the container's true CPU allowance rather than the host machine's core count.
- `runqueue_wait_seconds()` reads field 2 of `/proc/self/schedstat` (nanoseconds spent
  runnable in the kernel runqueue waiting for a CPU to be scheduled). On serverless
  containers without an in-flight request or long-polled `/is_busy`, `runqueue_wait`
  jumps to 79-85%; with `KEEPALIVE_HOLD_SECONDS=25` + `cpu_idle=false`, it drops to 0.0%.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _parse_cpu_max(text: str) -> Optional[float]:
  parts = text.split()
  if len(parts) != 2 or parts[0] == "max":
    return None
  try:
    quota, period = float(parts[0]), float(parts[1])
  except ValueError:
    return None
  return quota / period if quota > 0 and period > 0 else None


def cpu_quota() -> float:
  """Returns the number of CPU cores allocated to this container via cgroups."""
  for path, parse in (
      ("/sys/fs/cgroup/cpu.max", _parse_cpu_max),
      ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", None),
  ):
    try:
      text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
      continue
    if parse is not None:
      value = parse(text)
    else:
      try:
        period = float(
            Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text(
                encoding="utf-8"
            )
        )
        quota = float(text)
        value = quota / period if quota > 0 and period > 0 else None
      except (OSError, ValueError):
        value = None
    if value:
      return round(value, 2)
  return float(os.cpu_count() or 1)


def runqueue_wait_seconds() -> Optional[float]:
  """Returns cumulative seconds this process spent runnable but unscheduled (`/proc/self/schedstat`)."""
  try:
    fields = Path("/proc/self/schedstat").read_text(encoding="utf-8").split()
  except OSError:
    return None
  try:
    return round(float(fields[1]) / 1e9, 4)
  except (IndexError, ValueError):
    return None
