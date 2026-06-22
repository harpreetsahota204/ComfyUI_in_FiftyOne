"""ComfyUI server lifecycle: config, PID file, spawn, health-check."""

import os
import subprocess
import sys
import time

import requests

from ._constants import (
    DEFAULT_COMFYUI_PATH,
    DEFAULT_COMFYUI_PORT,
    PID_FILE,
    STATE_DIR,
    _persist,
)


def _get_config(ctx) -> dict:
    """Read plugin configuration from the execution store.

    ``comfyui_path`` is run through ``os.path.expanduser`` so that
    user-supplied values like ``~/comfy/ComfyUI`` resolve.  Idempotent
    on absolute paths.
    """
    store = ctx.store("comfyui_plugin_config")
    return {
        "comfyui_path": os.path.expanduser(
            store.get("comfyui_path") or DEFAULT_COMFYUI_PATH
        ),
        "comfyui_port": int(store.get("comfyui_port") or DEFAULT_COMFYUI_PORT),
        "comfyui_args": store.get("comfyui_args") or [],
    }


def _set_config(ctx, key: str, value):
    """Write a single config value to the execution store."""
    store = ctx.store("comfyui_plugin_config")
    store.set(key, value)


def _is_server_running(port: int, timeout: float = 2.0) -> bool:
    """Check if a ComfyUI server is responding on the given port."""
    try:
        resp = requests.get(
            f"http://127.0.0.1:{port}/system_stats",
            timeout=timeout,
        )
        return resp.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


def _read_pid() -> "int | None":
    """Read the PID from the PID file, or None if absent/stale."""
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_pid(pid: int):
    """Write a PID to the PID file."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(pid))


def _clear_pid():
    """Remove the PID file."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def _spawn_comfyui(comfyui_path: str, port: int, extra_args: list) -> subprocess.Popen:
    """Spawn a ComfyUI server subprocess."""
    main_py = os.path.join(comfyui_path, "main.py")
    if not os.path.isfile(main_py):
        raise FileNotFoundError(
            f"ComfyUI main.py not found at {main_py}. "
            f"Check your comfyui_path setting."
        )

    cmd = [
        sys.executable,
        main_py,
        "--listen", "127.0.0.1",
        "--port", str(port),
        "--enable-cors-header",
        *extra_args,
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=comfyui_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    _persist.comfyui_process = proc
    _write_pid(proc.pid)

    return proc


def _wait_for_server(port: int, timeout: float = 120.0) -> bool:
    """Poll until the ComfyUI server is responsive (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_server_running(port):
            return True
        time.sleep(1.0)
    return False
