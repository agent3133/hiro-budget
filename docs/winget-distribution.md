# Plan: distribution via winget

Status: **plan only, nothing implemented** (written 2026-10-23). Postponed until there is a
stable release. Goal: users install and update the app with

```
winget install <Publisher>.FamilienFinanzanalyse
winget upgrade <Publisher>.FamilienFinanzanalyse
```

winget does not host files. It installs from a public HTTPS download URL listed in a
manifest; the manifests live in Microsoft's community repository
[`microsoft/winget-pkgs`](https://github.com/microsoft/winget-pkgs) and are added by pull
request. So distribution via winget = **a public, versioned installer download + a manifest
PR per release**.

Verify the details below against the official docs when picking this up again; schema
versions and validation rules change:
[manifest docs](https://learn.microsoft.com/windows/package-manager/package/manifest),
[submitting](https://learn.microsoft.com/windows/package-manager/package/repository).

## What exists already

- `packaging/build_exe.py`: Nuitka standalone folder `dist/launcher.dist/` (204 MB, exe
  86.7 MB), ring icon, version from `hiro_budget.__version__` in the file properties.
- The exe runs without Python; Nuitka bundles the Windows runtime DLLs (still to be
  confirmed on a clean machine, see phase 1).
- The app is local-only (no network), which keeps the privacy statement short.

## Decisions to make first

| Decision | Options / notes |
|---|---|
| Package identifier | `<Publisher>.FamilienFinanzanalyse`; the publisher is a person or brand name, it cannot be changed later without a new package |
| Public download host | GitHub Releases of the public repository (stable URLs per version) |
| Licence of the app | GPL-3.0 (decided 2026-11-01, issue #26); goes into the manifest (`License`, `LicenseUrl`) |
| Signing | Not strictly required by winget, but unsigned Nuitka exes trigger SmartScreen warnings and antivirus false positives, which also hit the automated validation. Options: Azure Trusted Signing (≈10 $/month), OV certificate (≈100–300 €/year) |
| Product name | "Hiro Budget" (decided 2026-11-01, issue #26). Do not use "ING" in the product name or icon (trademark); "reads ING statements" in the description is fine |
| Install scope | Per user (recommended: no admin rights, installs to `%LOCALAPPDATA%\Programs`) or per machine |

## Phase 1: make the app installable

The app today expects `config/` next to the exe or in a folder above. An installed program
must not write into its install folder.

1. **Data folder:** config, database, classifier, backups and `ui_state.json` in
   `%APPDATA%\Hiro Budget\` for the installed exe. Lookup order in
   `config.default_config_dir()`: `$HIRO_BUDGET_CONFIG_DIR` → `./config` (development) →
   `config` next to the exe (portable use) → `%APPDATA%\…`. Paths in `config.yaml` stay
   relative to the config's parent folder, as now.
2. **First-run setup:** if no config exists, create it from the bundled `*.example.yaml`
   (done in #25: templates in the package and the build, created on start) and ask in a
   small dialog for: language, import
   folder, own accounts (or start with none and add them on the accounts page). The example
   config must work out of the box (`test_example_config.py` already guards the first
   import).
3. **Errors visible without a console:** message box for config/database errors at
   startup (config errors: done in #25; database errors still only stderr), plus a rotating log file in the data folder
   (`logging.handlers.RotatingFileHandler`). This is the postponed "configuration not
   found" item.
4. **Version as single source:** `__version__` (and `pyproject.toml`) bumped per release;
   shown in the About dialog; used by the build and the installer.
5. **Third-party licences:** collect the licence texts of all bundled packages (Qt/PySide6
   LGPL — keep Qt as separate DLLs, which the folder build does; numpy, SQLAlchemy,
   pdfplumber/pdfminer, pydantic, cryptography/OpenSSL, Inter font OFL, …) into
   `LICENSES/` in the build, generated from the installed distributions, and link them
   from the About dialog.
6. **Test on a clean Windows 10 and 11** (VM or Windows Sandbox) without Python or Visual
   Studio: start, first-run setup, import, budget, reports, uninstall.

## Phase 2: installer

**Inno Setup** (free, well supported by winget as `InstallerType: inno`), script in
`packaging/installer.iss`, compiled with `iscc`:

- `AppId` = a fixed GUID (never change it: winget and the uninstaller identify the app by
  it; the Windows "Apps" entry is `{AppId}_is1`).
- Per-user install: `PrivilegesRequired=lowest`,
  `DefaultDirName={localappdata}\Programs\Hiro Budget`.
- Files: the whole `dist\launcher.dist\` folder.
- Start-menu entry and optional desktop icon with the ring icon (this covers the postponed
  shortcut item).
- `CloseApplications=yes` so an upgrade closes a running app.
- Uninstall removes the program, **not** the data in `%APPDATA%` (or asks).
- Silent install must work (winget runs installers silently; Inno's standard switches are
  `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`, which winget applies for `inno` installers).
- Output: `Hiro-Budget-Setup-<version>-x64.exe`.

Alternative without an installer: winget also supports zip packages with a portable exe,
but a portable app has no Start-menu entry and the data-folder logic gets harder; the
installer is the better fit.

## Phase 3: signing

Sign both the app exe (before packing) and the installer with `signtool` and a timestamp
server. With Azure Trusted Signing this runs in CI with the signing action / `signtool`
plus the Trusted Signing client. Keep the signing secrets only in the CI secret store.

## Phase 4: release pipeline

A Windows CI runner (GitHub Actions `windows-latest`) per tag `vX.Y.Z`:

1. `uv sync`, `uv run pytest`, `uv run ruff check`
2. `uv run --group build python packaging/build_exe.py` (≈11 min; cache Nuitka's cache
   folder between runs)
3. smoke test of the built exe: `import`, `train`, `recategorize` on the test fixture
   (as done manually so far)
4. sign the exe, `iscc packaging/installer.iss`, sign the installer
5. compute the SHA-256 of the installer
6. publish a release with the installer at a **stable public URL** plus release notes

## Phase 5: first winget submission

The manifest is three YAML files under
`manifests/<first letter>/<Publisher>/FamilienFinanzanalyse/<version>/` in winget-pkgs:
version, installer and default-locale manifest. Create them with
[`wingetcreate`](https://github.com/microsoft/winget-create):

```
winget install Microsoft.WingetCreate
wingetcreate new <public installer URL>
```

It downloads the installer, detects the type and asks for the rest. Key fields
(placeholders; use the schema version current at the time):

```yaml
# installer manifest
PackageIdentifier: <Publisher>.FamilienFinanzanalyse
PackageVersion: 1.0.0
InstallerType: inno
Scope: user
UpgradeBehavior: install
Installers:
  - Architecture: x64
    InstallerUrl: https://<host>/releases/download/v1.0.0/Hiro-Budget-Setup-1.0.0-x64.exe
    InstallerSha256: <sha256>
    ProductCode: <AppId GUID>_is1

# default locale manifest
PackageLocale: de-DE
Publisher: <Publisher>
PackageName: Hiro Budget
License: <licence>
LicenseUrl: <url>
ShortDescription: Budget app for families; imports bank statements (PDF), categorizes them and shows budget and reports. Works offline.
Tags: [budget, finance, haushaltsbuch, ynab]
PrivacyUrl: <url>   # "no data leaves your computer"
ReleaseNotesUrl: <url>
```

Then:

1. `winget validate --manifest <folder>`
2. test install, start, upgrade and uninstall in **Windows Sandbox** with
   `winget install --manifest <folder>` (winget-pkgs has a sandbox test script)
3. submit: `wingetcreate submit` opens the PR (needs a GitHub account; one manifest per
   PR); the automated pipeline checks the manifest, downloads the installer and scans it,
   then a moderator may review
4. after the merge it is available via `winget install` within hours

## Phase 6: updates

Per release, after phase 4:

```
wingetcreate update <Publisher>.FamilienFinanzanalyse --version X.Y.Z --urls <new installer URL> --submit
```

This can run in the release pipeline (with a GitHub token as secret). Users update with
`winget upgrade`. Optional later: an in-app "new version available" hint, opt-in, since
the app has no network access today (mention in the privacy statement).

## Checklist

- [ ] Decisions: identifier, public host, licence, signing, name, scope
- [ ] Data folder in `%APPDATA%`, first-run setup, error message box + log file
- [ ] Version bump process, About dialog with version and third-party licences
- [ ] Clean-VM test (Windows 10 and 11)
- [ ] `packaging/installer.iss` (Inno Setup), Start menu + desktop icon, silent install
- [ ] Code signing set up
- [ ] CI release pipeline with smoke test and public release URL
- [ ] Manifests via `wingetcreate`, `winget validate`, Sandbox test, PR to winget-pkgs
- [ ] Update step in the pipeline (`wingetcreate update --submit`)

## Effort (rough)

Phase 1: 1–2 days. Phase 2: half a day. Phase 3: half a day plus waiting for the signing
account verification. Phase 4: 1 day. Phase 5: half a day plus review time. Updates
afterwards: automatic.
