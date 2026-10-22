"""Linux child supervisor: kill our process group if the owning supervisor dies.

Invoked as a private script so it works from a source checkout and an installed
package. The real command inherits stdin/stdout/stderr and this process group.
"""
import os
import signal
import subprocess
import sys
import time


def main():
    parent = int(sys.argv[1])
    command = sys.argv[2:]
    if os.getppid() != parent:
        return 125
    try:
        child = subprocess.Popen(command)
    except OSError as exc:
        print(f"Failed to start command: {exc}", file=sys.stderr)
        return 127
    while child.poll() is None:
        if os.getppid() != parent:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        time.sleep(0.1)
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
