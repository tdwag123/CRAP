from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path


def ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}", value) or ".." in value:
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def digest(value) -> str:
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def safe_file(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in ("..", ".git", ".lake") for p in rel.parts):
        raise ValueError(f"Unsafe relative path: {relative}")
    p = root / rel
    for ancestor in [p, *p.parents]:
        if ancestor == root.parent:
            break
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError(f"Links are not allowed in source paths: {relative}")
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes workspace: {relative}")
    return p


def source_files(root: Path) -> dict[str, bytes]:
    paths = [root / "Submission.lean"]
    sub = safe_file(root, "Submission")
    if sub.exists():
        paths.extend(sub.rglob("*.lean"))
    result = {}
    for p in sorted(paths):
        rel = p.relative_to(root).as_posix()
        p = safe_file(root, rel)
        if not p.is_file():
            raise ValueError(f"Missing submission file: {rel}")
        result[rel] = p.read_bytes()
    return result


def source_digest(root: Path) -> str:
    return digest({p: digest(data) for p, data in source_files(root).items()})


def clean_env() -> dict[str, str]:
    allowed = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
               "ELAN_HOME", "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {k: v for k, v in os.environ.items() if k.upper() in allowed}


def terminate_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        if proc.poll() is None:
            proc.kill()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def process_options() -> dict:
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def run_command(argv: list[str], cwd: Path, log: Path, timeout: float, *, stdin: str | None = None,
                env: dict | None = None, tick=None) -> int:
    """Stream logs to disk; bound the entire process group, including on Ctrl-C."""
    log.parent.mkdir(parents=True, exist_ok=True)
    if timeout <= 0:
        log.write_text("Deadline reached before command launch.\n", encoding="utf-8")
        return 124
    with log.open("wb") as out:
        actual = argv
        if sys.platform == "linux":
            actual = [sys.executable, str(Path(__file__).with_name("_process_guard.py")), str(os.getpid()), *argv]
        proc = subprocess.Popen(actual, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, env=env, **process_options())
        try:
            if stdin is not None:
                proc.stdin.write(stdin.encode("utf-8"))
                proc.stdin.close()
            end = time.monotonic() + max(0, timeout)
            while proc.poll() is None:
                if time.monotonic() >= end:
                    terminate_tree(proc)
                    return 124
                if tick:
                    tick()
                time.sleep(min(0.2, max(0, end - time.monotonic())))
            return proc.returncode
        except BaseException:
            terminate_tree(proc)
            raise
        finally:
            # A completed CLI may have started background compilers. They must
            # not keep editing after the turn boundary or race verification.
            if os.name != "nt":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)


@contextlib.contextmanager
def supervisor_lock(root: Path):
    """OS-owned lock releases after a crash; no expiring lease can admit two writers."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "supervisor.lock").open("a+b") as f:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            f.write(b"0")
            f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another supervisor is running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another supervisor is running") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
