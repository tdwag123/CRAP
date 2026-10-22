"""Package source for transfer to the server, excluding credentials and run state."""
import argparse
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/crap.zip"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sources = [root / name for name in ("README.md", "AGENTS.md", "pyproject.toml", ".gitignore", ".gitattributes")]
    for directory in ("crap", "tests", "scripts", "deploy", ".github"):
        sources.extend(p for p in (root / directory).rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(sources):
            if path.is_symlink():
                raise ValueError(f"Refusing to package a symlink: {path}")
            archive.write(path, path.relative_to(root).as_posix())
    with zipfile.ZipFile(args.output) as archive:
        if archive.testzip():
            raise RuntimeError("Archive integrity check failed")
        print(f"Packaged {len(archive.namelist())} source files: {args.output.resolve()}")


if __name__ == "__main__":
    main()
