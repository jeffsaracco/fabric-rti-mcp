import json
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="relies on POSIX signal delivery")

_INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}


def _start_stdio_server(stderr_file: Path) -> subprocess.Popen[bytes]:
    env = {key: value for key, value in os.environ.items() if key not in ("PORT", "FUNCTIONS_CUSTOMHANDLER_PORT")}
    env["FABRIC_RTI_TRANSPORT"] = "stdio"
    with stderr_file.open("wb") as stderr:
        return subprocess.Popen(
            [sys.executable, "-c", "from fabric_rti_mcp.server import main; main()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
        )


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT], ids=lambda sig: sig.name)
def test_stop_signals_exit_cleanly_while_stdin_is_open(stop_signal: signal.Signals, tmp_path: Path) -> None:
    stderr_file = tmp_path / "stderr.log"
    proc = _start_stdio_server(stderr_file)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(json.dumps(_INITIALIZE_REQUEST).encode() + b"\n")
        proc.stdin.flush()
        # A completed `initialize` round trip means the stdio reader thread is now blocked on stdin.
        ready, _, _ = select.select([proc.stdout], [], [], 60)
        assert ready, "server did not answer initialize"
        assert b'"result"' in proc.stdout.readline()

        proc.send_signal(stop_signal)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Shutdown is stuck behind the stdin reader thread. A second signal at this point makes CPython abort
            # ("Fatal Python error: _enter_buffered_busy"), which macOS reports as a crash.
            proc.send_signal(stop_signal)
            proc.wait(timeout=10)

        stderr = stderr_file.read_text(errors="replace")
        assert "Fatal Python error" not in stderr, stderr
        assert proc.returncode == 0, stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdin.close()
        proc.stdout.close()
