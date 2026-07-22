## Install

ChronoGate is free and open-source, so the installers are **unsigned** — the OS
shows a one-time warning you can clear. Nothing is wrong with the app.

### macOS (`.dmg`)
1. Download `ChronoGate-<version>.dmg` below, open it, and drag **ChronoGate**
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

### Run from source (no warnings)
With Python 3.12+:
```
pip install -e .
chronogate
```
(A PyPI package — `pip install chronogate` — is planned.)

---
