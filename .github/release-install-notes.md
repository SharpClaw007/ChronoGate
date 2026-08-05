## Install

ChronoGate is free and open-source, so the installers are **unsigned** — the OS
shows a one-time warning you can clear. Nothing is wrong with the app.

### macOS (`.dmg`)
Pick the build that matches your Mac's chip:
- **Apple Silicon** (M1/M2/M3/M4, 2020+): `ChronoGate-<version>-arm64.dmg`
- **Intel** (pre-2020, e.g. a 2016 MacBook Pro): `ChronoGate-<version>-x86_64.dmg`

Not sure? Apple menu ▸ **About This Mac** — "Apple M…" = Apple Silicon,
"Intel" = Intel. The wrong build fails to open with *"not supported on this
Mac."*

1. Download the matching `.dmg` below, open it, and drag **ChronoGate**
   into your **Applications** folder.
2. Launch it. macOS blocks the first open: *"Apple cannot check it for malicious
   software."*
3. Allow it **once**:
   - **macOS 15 (Sequoia):** open **System Settings ▸ Privacy & Security**, scroll
     to the ChronoGate message, click **Open Anyway**, then confirm.
   - **macOS 14 (Sonoma) or earlier:** right-click (Control-click) the app ▸
     **Open** ▸ **Open**.
   - If you ever see *"ChronoGate is damaged"*, run in Terminal:
     `xattr -dr com.apple.quarantine /Applications/ChronoGate.app`

   After that it launches normally every time.

### Windows (`.exe`)
Run the installer. At *"Windows protected your PC"* click **More info ▸ Run
anyway** (the app is unsigned; SmartScreen warns until it builds reputation).

**Deploying to managed/lab machines (IT):** the installer is Inno Setup, so it
takes the standard unattended flags:
```
ChronoGate-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ALLUSERS
```
Use `/CURRENTUSER` instead of `/ALLUSERS` to install per-user without admin
rights. Because the build is unsigned, allowlist it by hash if the fleet runs
AppLocker or WDAC — those policies block unsigned binaries outright, with no
"Run anyway" escape. Crash logs land in `%LOCALAPPDATA%\ChronoGate\logs`.

### Run from source (no warnings)
With Python 3.12+:
```
pip install -e .
chronogate
```
(A PyPI package — `pip install chronogate` — is planned.)

---
