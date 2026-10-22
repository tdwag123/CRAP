"""Path checks for the public pipeline repository, not a proof-content classifier."""
from pathlib import PurePosixPath

ROOT_FILES = {"README.md", "AGENTS.md", "pyproject.toml", ".gitignore", ".gitattributes",
              "LICENSE", "LICENSE.md", "NOTICE", "SECURITY.md", "CONTRIBUTING.md"}
SOURCE_ROOTS = {"crap", "tests", "scripts", "deploy", ".github", ".githooks", "docs"}
PRIVATE_PARTS = {".crap", ".leaneval", ".test-artifacts", ".lake", ".git", "proofs", "solutions",
                 "research", "workspaces", "generated", "submissions", "runs", "artifacts", "handoff"}
PRIVATE_NAMES = {"dag.json", "node-status.json", "proof.md", "progress.md", "sources.json", "search.md",
                 "statement.md", "result.json", "answer.json", "request.json", "pipeline.local.json", "auth.json"}
SOURCE_SUFFIXES = {".py", ".txt", ".md", ".toml", ".yml", ".yaml", ".example"}


def public_path(path: str) -> bool:
    if "\\" in path:
        return False
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return False
    if set(part.lower() for part in p.parts) & PRIVATE_PARTS:
        return False
    if p.name.lower() in PRIVATE_NAMES or p.name.lower().startswith(".env"):
        return False
    if path in ROOT_FILES or path == ".githooks/pre-push":
        return True
    return len(p.parts) > 1 and p.parts[0] in SOURCE_ROOTS and p.suffix.lower() in SOURCE_SUFFIXES


def check_paths(paths) -> list[str]:
    return sorted(path for path in paths if not public_path(path))
