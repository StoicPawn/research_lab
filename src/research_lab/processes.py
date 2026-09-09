from __future__ import annotations

from pathlib import Path


def active_experiment_count(proc_root: Path = Path('/proc')) -> int:
    """Count resource-bounded Research Lab child experiment processes.

    The runner launches experiments as `python -I main.py`; inspecting /proc avoids
    requiring procps utilities in the slim application image.
    """
    active = 0
    if not proc_root.exists():
        return 0
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', errors='replace')
        except OSError:
            continue
        if 'python -I main.py' in cmd:
            active += 1
    return active
