"""Reject private artifact paths before a public push or source package."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crap.publication import check_paths


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True)


def outgoing_commits(updates):
    commits = set()
    for line in updates:
        _, local, _, remote = line.split()
        if set(local) == {"0"}:
            continue
        revision = local if set(remote) == {"0"} else remote + ".." + local
        commits.update(git("rev-list", revision).splitlines())
    return sorted(commits)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--push", action="store_true")
    mode.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.install:
        git("config", "core.hooksPath", ".githooks")
        git("config", "crap.python", sys.executable.replace("\\", "/"))
        print("Installed the public-source pre-push check for this checkout.")
        return 0
    rejected = set()
    if args.push:
        for commit in outgoing_commits(sys.stdin):
            rejected.update(check_paths(git("ls-tree", "-rz", "--name-only", commit).split("\0")[:-1]))
    else:
        rejected.update(check_paths(git("ls-files", "-z").split("\0")[:-1]))
    if rejected:
        print("Publication blocked: these paths are outside the public pipeline source policy:", file=sys.stderr)
        print("\n".join(sorted(rejected)), file=sys.stderr)
        return 1
    print("Public-source path check passed. Problem-specific content must remain private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
