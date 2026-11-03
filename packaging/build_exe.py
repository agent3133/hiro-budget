"""Build ``dist/launcher.dist/hiro-budget.exe`` with Nuitka.

A folder ("standalone"), not a single file: a onefile exe unpacks itself on every start,
which would cost the time the build is meant to save. The exe finds ``config`` next to it
or in a folder above (``config.default_config_dir``), so it runs from ``dist`` as it is.

Licences: the app is GPL-3.0; the folder gets ``LICENSE`` and ``THIRD_PARTY_LICENSES.txt``.
LGPL libraries stay replaceable: PySide6/Qt ships as its own DLLs and extension modules,
python-fints as source files next to the exe (not compiled; what it imports is). enum-tools
(LGPL, optional for python-fints) is left out. The generated demo goes into ``demo/`` with a
``Start demo.cmd`` (``hiro-budget demo``).

Run: ``uv run --group build python packaging/build_exe.py`` (needs a C compiler: Visual
Studio Build Tools; the first build takes a while).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from hiro_budget import __version__

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "hiro_budget"
MIGRATIONS = PACKAGE / "db" / "migrations"
IN_EXE = "hiro_budget/db/migrations"  # the same place in the exe's folder
NAME = "Hiro Budget"  # product name
EXE = "hiro-budget"  # file name, as the command
# what python-fints imports from outside the standard library (enum_tools is optional)
FINTS_IMPORTS = ("bleach", "lxml.etree", "mt940", "mt940.models", "requests", "sepaxml")


def nuitka_args(output_dir: Path = ROOT / "dist", lto: bool = False) -> list[str]:
    version = ".".join((__version__.split("+")[0].split(".") + ["0"] * 4)[:4])
    return [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        f"--output-dir={output_dir}",
        f"--report={output_dir / 'compilation-report.xml'}",  # what is in it, and why
        f"--lto={'yes' if lto else 'no'}",
        # not --python-flag=no_docstrings: SQLAlchemy reads docstrings when it starts
        f"--output-filename={EXE}.exe",
        "--enable-plugin=pyside6",
        # the GUI without a console window; the commands (import, train) print into the
        # console they are started from
        "--windows-console-mode=attach",
        f"--windows-icon-from-ico={ROOT / 'packaging' / 'app.ico'}",
        f"--product-name={NAME}",
        "--file-description=Hiro Budget: local family budget",
        f"--product-version={version}",
        f"--file-version={version}",
        # files read from disk at run time
        f"--include-data-dir={PACKAGE / 'gui' / 'icons'}=hiro_budget/gui/icons",
        f"--include-data-dir={PACKAGE / 'gui' / 'fonts'}=hiro_budget/gui/fonts",
        f"--include-data-files={PACKAGE / 'i18n'}/*.qm=hiro_budget/i18n/",
        # config templates: the first start creates config/ from them
        f"--include-data-files={PACKAGE / 'templates'}/*.yaml=hiro_budget/templates/",
        # the user guide (Help > User guide), see gui/guide.py
        *(
            f"--include-data-files={ROOT / 'docs'}/{pattern}=hiro_budget/guide/"
            for pattern in ("workflows.*.md", "csv-import.*.md", "csv-formats.llm.md")
        ),
        f"--include-data-dir={ROOT / 'docs' / 'images' / 'workflows'}"
        "=hiro_budget/guide/images/workflows",
        # Alembic loads the migrations as files, so they go in as files, too
        f"--include-data-files={MIGRATIONS}/*.py={IN_EXE}/",
        f"--include-data-files={MIGRATIONS}/*.mako={IN_EXE}/",
        f"--include-data-files={MIGRATIONS / 'versions'}/*.py={IN_EXE}/versions/",
        # SQLAlchemy picks the driver from the URL at run time: only the SQLite one; the
        # test helpers of SQLAlchemy and Alembic are not needed (Alembic itself is found
        # through the imports)
        "--include-module=sqlalchemy.dialects.sqlite.pysqlite",
        # python-fints (LGPL) as source next to the exe (see post_build), its imports compiled
        "--nofollow-import-to=fints,enum_tools",
        # without this the exe refuses to import an excluded module even from source files
        "--no-deployment-flag=excluded-module-usage",
        *(f"--include-module={m}" for m in FINTS_IMPORTS),
        # importers are plugins, imported by name at run time (#24): the package and the
        # manifests (and CSV formats) of the built-in plugins
        "--include-package=hiro_budget.plugins",
        "--include-package-data=hiro_budget.plugins:*.yaml",
        "--nofollow-import-to=pytest,_pytest,fpdf,faker,sqlalchemy.testing,alembic.testing",
        # Never used, only reachable through numpy's lazy attributes (np.f2py, np.testing),
        # a debug main in pdfminer.ccitt (unittest) and defusedxml.xmlrpc (measured
        # 2026-11-01 in compilation-report.xml: 34 modules)
        "--nofollow-import-to=numpy.f2py,numpy.testing,unittest,xmlrpc",
        # Never used: syntax highlighting for Mako's error pages (it falls back without),
        # pdfplumber's page rendering (to_image: PDFium, Pillow; pdfminer and openpyxl
        # import Pillow only for images)
        "--nofollow-import-to=pygments,PIL,pdfplumber.display,pypdfium2,pypdfium2_raw",
        # PySide6 tries QtNetwork only to set up OpenSSL (no network here; it copes)
        "--nofollow-import-to=PySide6.QtNetwork",
        # pydantic's mypy plugin (type checking only): with mypy in the venv since #80 it
        # pulled in all of mypy (measured 2026-11-03: 23 MB, a 19 MB mypyc .pyd)
        "--nofollow-import-to=pydantic.mypy,mypy,mypyc,ast_serialize",
        # Qt: no network (TLS backends), only the Windows platform, SVG and ICO images
        "--noinclude-qt-plugins=tls",
        *(
            f"--noinclude-dlls=PySide6/qt-plugins/{plugin}.dll"
            for plugin in (
                "platforms/qdirect2d",
                "platforms/qminimal",
                "platforms/qoffscreen",
                *(
                    f"imageformats/{f}"
                    for f in ("qgif", "qicns", "qjpeg", "qpdf", "qtga", "qtiff", "qwbmp", "qwebp")
                ),
            )
        ),
        "--noinclude-dlls=qt6pdf.dll",
        str(ROOT / "packaging" / "launcher.py"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the exe with Nuitka.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    # measured: the exe 2 MB larger and the build 20 % longer, so not the default
    parser.add_argument("--lto", action="store_true", help="link-time optimisation")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    code = subprocess.call(nuitka_args(output, args.lto), cwd=ROOT)
    if code == 0:
        post_build(output / "launcher.dist")
    return code


def post_build(dist: Path) -> None:
    """Licences and the LGPL library python-fints as replaceable source files."""
    import fints

    source = Path(fints.__file__).resolve().parent
    target = dist / "fints"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT / "LICENSE", dist / "LICENSE.txt")
    subprocess.check_call(
        [
            sys.executable,
            str(ROOT / "packaging" / "third_party_licenses.py"),
            str(dist / "THIRD_PARTY_LICENSES.txt"),
        ],
        cwd=ROOT,
    )
    demo(dist)
    print(f"post-build: python-fints source, licences, demo in {dist}")


def demo(dist: Path) -> None:
    """The generated demo next to the exe (``hiro-budget demo``) and a file to start it."""
    target = dist / "demo"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(ROOT / "demo" / "config", target / "config")
    subprocess.check_call(
        [
            sys.executable,
            str(ROOT / "demo" / "generate.py"),
            "--db",
            str(target / "data" / "demo.db"),
        ],
        cwd=ROOT,
    )
    for leftover in ("backups",):  # made by the generator's imports
        shutil.rmtree(target / "data" / leftover, ignore_errors=True)
    (dist / "Start demo.cmd").write_text(
        '@echo off\r\nstart "" "%~dp0hiro-budget.exe" demo\r\n', encoding="ascii"
    )


if __name__ == "__main__":
    sys.exit(main())
