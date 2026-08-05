# ChronoGate — offline / lab deployment sheet

For installing ChronoGate on a lab PC from a USB drive, with no internet access
on the target machine. Windows 10 or 11, 64-bit.

## What is on the drive

| File | Purpose |
|---|---|
| `ChronoGate-Setup-<version>.exe` | The installer. Self-contained. |
| `ChronoGate-SOP.pdf` | Operating guide — how to use the app. |
| `README.txt` | This sheet. |

The installer bundles its own Python runtime, Qt, numpy and scipy. **The lab PC
does not need Python installed**, and nothing is downloaded during setup.

Windows 10 already provides the C runtime the app links against, so no Visual
C++ redistributable is required.

## Installing (no admin rights needed)

1. Copy the whole folder from the USB drive to the PC (e.g. to the Desktop).
   Running an installer directly off removable media can trip security policy.
2. Double-click `ChronoGate-Setup-<version>.exe`.
3. Windows shows **"Windows protected your PC"** — the build is unsigned, which
   is expected. Click **More info ▸ Run anyway**.
4. When asked, choose **Install for me only** if you do not have an
   administrator password. This installs to your own profile and needs no
   elevation.
5. Launch from the Start menu.

## Installing (IT / imaging a fleet)

The installer is Inno Setup and takes the standard unattended flags:

```
ChronoGate-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ALLUSERS
```

Use `/CURRENTUSER` in place of `/ALLUSERS` for a per-user install without
elevation.

**If the fleet runs AppLocker or Windows Defender Application Control**, the
build is unsigned and those policies will block it outright — there is no "Run
anyway" for them. Allowlist the executable by hash before deploying.

## Checking it works

1. Launch ChronoGate. The main window should open with empty plots.
2. **File ▸ Open .ptu…** and load a known-good dataset.
3. An image should appear on the right and a decay curve on the left.

## If something goes wrong

ChronoGate writes a diagnostic log whenever it hits an unexpected error,
including a crash with no visible message. Send the newest file from:

```
%LOCALAPPDATA%\ChronoGate\logs
```

Paste that path into the File Explorer address bar to get there. The error
dialog also shows the exact filename. Only the last 20 logs are kept.

That log is the fastest route to a fix — it records the version, the operating
system, and exactly where the failure happened.

## Optional

The **Export & open in Fiji** feature needs Fiji/ImageJ installed separately
(<https://fiji.sc>). Everything else in ChronoGate works without it. If the lab
PC is offline and you want that integration, put the Fiji zip on the USB drive
too, then set its path in **ChronoGate ▸ Preferences**.
