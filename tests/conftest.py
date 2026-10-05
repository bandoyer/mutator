import signal
import threading
import time

import pytest


@pytest.fixture
def ctrl_c_when():
    """Press Ctrl-C once a file exists: SIGINT to this process's main thread only."""

    def arm(marker):
        main = threading.main_thread().ident

        def watch():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if marker.exists():
                    signal.pthread_kill(main, signal.SIGINT)
                    return
                time.sleep(0.02)

        threading.Thread(target=watch, daemon=True).start()

    return arm


def wait_until(marker, seconds):
    """Sleep until `seconds` after `marker` was written, so a late write would have landed."""

    time.sleep(max(0.0, marker.stat().st_mtime + seconds - time.time()))
