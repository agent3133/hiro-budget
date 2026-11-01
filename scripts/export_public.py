"""Export the code for the public repository (GitHub) as one commit.

    uv run python scripts/export_public.py --to ../hiro-budget-public \\
        --author "Name <your GitHub no-reply address>" [--ref v0.1.0] [--message ...]

The tree of ``--ref`` (default ``HEAD``) is taken with ``git archive``, so files marked
``export-ignore`` in ``.gitattributes`` (e.g. ``CLAUDE.md``) stay private, and nothing from
the history comes along. Exactly the exported files are checked with
``scripts/privacy_check.py`` (denylist, IBAN checksums, e-mail domains); with findings
nothing is written. Then ``--to`` (a clone of the public repository, or a new folder that
is initialised) gets the tree as one new commit by ``--author``. Pushing stays a separate,
deliberate step: ``git -C <to> push``.
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from privacy_check import check  # noqa: E402

AUTHOR = re.compile(r"^(?P<name>[^<>]+?) <(?P<email>[^<>@\s]+@[^<>\s]+)>$")


def git(*args: str, cwd: Path = ROOT, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def export_tree(ref: str, target: Path) -> list[Path]:
    data = subprocess.run(
        ["git", "archive", "--format=tar", ref], cwd=ROOT, capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(target, filter="data")
    return [p for p in target.rglob("*") if p.is_file()]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--to", type=Path, required=True, help="the public repository's folder")
    p.add_argument("--author", required=True, help='"Name <noreply e-mail>" of the commit')
    p.add_argument("--ref", default="HEAD", help="commit or tag to export (default HEAD)")
    p.add_argument("--message", help='commit message (default "Hiro Budget <version>")')
    args = p.parse_args(argv)
    author = AUTHOR.match(args.author)
    if not author:
        p.error('--author must look like "Name <e-mail>"')

    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        files = export_tree(args.ref, tree)
        findings = check(files, tree)
        if findings:
            print("\n".join(findings))
            print(f"{len(findings)} privacy findings: nothing exported")
            return 1
        version = re.search(
            r'^version = "([^"]+)"', (tree / "pyproject.toml").read_text(encoding="utf-8"), re.M
        ).group(1)
        target = args.to.resolve()
        target.mkdir(parents=True, exist_ok=True)
        if not (target / ".git").exists():
            git("init", "-b", "main", cwd=target)
        for item in target.iterdir():  # the previous release's tree goes
            if item.name == ".git":
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        for item in tree.iterdir():
            dest = target / item.name
            shutil.copytree(item, dest) if item.is_dir() else shutil.copy2(item, dest)

    name, email = author.group("name"), author.group("email")
    identity = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    git(*identity, "add", "-A", cwd=target)
    if not git("status", "--porcelain", cwd=target):
        print(f"{target}: no changes against the last export")
        return 0
    message = args.message or f"Hiro Budget {version}"
    git(*identity, "commit", "-q", "--author", f"{name} <{email}>", "-m", message, cwd=target)
    sha = git("rev-parse", "--short", "HEAD", cwd=target)
    count = git("ls-files", cwd=target).count("\n") + 1
    print(f"{target}: commit {sha} '{message}' by {name}, {count} files, 0 privacy findings")
    print(f"next: check it, then  git -C {target} push -u origin main  and tag v{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
