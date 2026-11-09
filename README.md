# Desktop Pet

Desktop Pet is a Windows desktop companion that keeps small, interactive pets
near the taskbar. Each pet shows activity or status from a different app or
part of the system.

## Pets

- **Claude** — Claude Code activity and usage gauges.
- **VLC, Spotify, YouTube, Chrome, Discord, VS Code, and CurseForge** — app or
  media status.
- **System** — CPU, memory, disk, battery, and network information.
- **Bambu** — printer status and camera support.

Some pets require their corresponding application to be installed and running.
The YouTube pet also requires the companion Chrome extension; see
[`youtube-extension/README.md`](./youtube-extension/README.md).

## Install

Requirements: Windows, PowerShell, and Python 3 available through the `py`
launcher or `python` command.

1. Clone or download this repository.
2. Open PowerShell in the project folder.
3. Run:

   ```powershell
   .\install.ps1
   ```

The installer installs the Python dependencies, copies the app to
`%LOCALAPPDATA%\DesktopPet`, creates a shortcut to start it at login, registers
the Claude usage-refresh scheduled task, and starts the app. Re-run the
installer to update an existing installation.

By default, the app opens all pets. To run one pet while developing:

```powershell
py -3 pet_app.py youtube
```

Replace `youtube` with a pet name from the list in `pet_app.py`.

### YouTube setup

Enabling the Chrome extension alone does not connect it. Start Desktop Pet,
open the YouTube Pet settings with a middle-click, copy its local bridge key,
and paste and save the key in the extension's options. Then reload the
extension and refresh a YouTube video tab. If Chrome asks for local network
access, allow it. The extension's options page shows connection errors.

### Uninstall

Run:

```powershell
.\uninstall.ps1
```

Uninstall removes the startup shortcut, scheduled task, and
`%LOCALAPPDATA%\DesktopPet` folder. That folder also contains your settings and
integration credentials; back up `config.json` first if you want to keep them.

## Development

Install dependencies and run the app:

```powershell
py -3 -m pip install -r requirements.txt
py -3 pet_app.py
```

Run the Windows self-checks with:

```powershell
py -3 test_pet_app.py
```

The self-checks create an invisible native Windows window and are intended to
run on Windows.

## Privacy and external integrations

- App settings, integration credentials, usage/activity snapshots, and logs
  are stored under `%LOCALAPPDATA%\DesktopPet`. The JSON settings file is not
  encrypted by the app.
- The YouTube extension sends video details only to the local receiver at
  `127.0.0.1`; its bridge key is stored in Chrome's local extension storage.
- Bambu cloud features connect to Bambu services. Claude usage refresh starts
  the local Claude CLI on a scheduled basis and consumes Claude usage; it can
  be disabled in the Claude pet settings.
- The System pet's optional Boost action downloads and launches the current
  `main.py` from
  [100RAMoptimal](https://github.com/giamat13/100RAMoptimal) in QuickClean
  mode. That script may request administrator permission.
- When VLC's configuration file already exists, the installer enables its
  HTTP interface on localhost and sets a local password so the VLC pet can
  read playback status. If it does not exist yet, run VLC once and re-run the
  installer.

This repository does not include the Bambu TLS certificate bundle referenced
by the app: `*.pem` files are intentionally ignored to avoid accidentally
committing private keys. As a result, the Bambu LAN camera requires a
separately obtained, appropriately licensed `pets/bambu-ca.pem` file. Do not
place private keys in the repository.

## Third-party components

Python dependencies are listed in [`requirements.txt`](./requirements.txt) and
retain their respective licenses. The Bambu camera code references the
certificate bundle from
[Doridian/OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI); see that
project's license before redistributing its files.

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for development and contribution
guidelines. Please do not include credentials, private keys, or personal
configuration files in issues or pull requests. See [`SECURITY.md`](./SECURITY.md)
to report security concerns.

## License

Desktop Pet is licensed under the [MIT License](./LICENSE).
