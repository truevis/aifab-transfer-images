# Import Photos and Videos

Transfer photos and videos from a USB-connected Android phone (MTP) to a local drive on Windows.

The primary interface is the command-line script [`transfer_images.py`](transfer_images.py). A Streamlit UI ([`app.py`](app.py)) is also available.

## Requirements

- Windows 10/11
- Python 3.10+
- Phone connected via USB in **File transfer** mode

## Setup

```powershell
cd C:\GitHub\aifab-transfer-images
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install --upgrade -r requirements.txt
```

The `mtp` package is vendored in [`mtp/`](mtp/) because the upstream GitHub package does not install cleanly via pip.

## CLI usage

Activate the virtual environment, then run `transfer_images.py`:

```powershell
.\.venv\Scripts\Activate.ps1
python transfer_images.py --help
```

Only one process should access the phone at a time. Stop Streamlit (or any other MTP client) before running the CLI, or transfers may fail with *resource is in use* errors.

### Commands

| Command | Description |
|---------|-------------|
| `list-devices` | List connected MTP phones |
| `list-folders` | List DCIM folders on the selected device |
| `import` | Copy photos and videos to the destination |
| `verify` | Confirm transferred files exist and match sizes |
| `delete` | Remove selected folders from the phone |

Global options (before the subcommand):

- `--device INDEX` — device to use (default: `0`; see `list-devices`)
- `--refresh-devices` — clear cached MTP handles before scanning

### Typical workflow

```powershell
# 1. Discover the phone and folders
python transfer_images.py list-devices
python transfer_images.py list-folders

# 2. Import (defaults: D:\Album-F, Camera + OpenCamera + Expert RAW)
python transfer_images.py import

# 3. Verify before deleting from the phone
python transfer_images.py verify

# 4. Delete (runs verify first unless --force-delete)
python transfer_images.py delete --confirm-backup
```

### Import / verify / delete options

| Option | Default | Description |
|--------|---------|-------------|
| `--dest PATH` | `D:\Album-F` | Destination root folder |
| `--folders NAME ...` | Camera, OpenCamera, Expert RAW | DCIM folders to process |
| `--rename` / `--no-rename` | rename on | Apply rename template |
| `--template` | `$F-$Y-$M-$D_$H.$N.$S` | Rename pattern |
| `--ext-lower` / `--no-ext-lower` | lower on | Lowercase file extensions |
| `--skip-existing` / `--no-skip-existing` | skip on | Skip files already at destination |
| `--skip-trashed` / `--no-skip-trashed` | skip on | Skip trashed files |
| `--skip-thumbnails` / `--no-skip-thumbnails` | skip on | Skip thumbnails |
| `--skip-screenshots` / `--no-skip-screenshots` | skip on | Skip screenshots |
| `--limit N` | — | Process at most N importable files (testing) |
| `--quiet` | off | Print only phase and summary lines |

Delete-only options:

- `--confirm-backup` — required; confirms folders are backed up
- `--force-delete` — skip the verify check (dangerous)

### Examples

```powershell
# Import to a custom folder, only Camera
python transfer_images.py import --dest E:\Photos --folders Camera

# Test run: copy at most 10 files to a temp folder
python transfer_images.py import --dest $env:TEMP\aifab-transfer-test --limit 10

# Import without renaming, overwriting skip-existing behavior
python transfer_images.py import --no-rename --no-skip-existing

# Second phone on the system
python transfer_images.py --device 1 list-folders
python transfer_images.py --device 1 import --dest D:\Album-F
```

### Defaults

- Skip existing files, trashed files, thumbnails, and screenshots
- Rename template: `$F-$Y-$M-$D_$H.$N.$S` (lowercase extension)
- Month subfolders: `YYYY-MM`
- Example renamed file: `IMG_1234-2026-06-08_19.14.27.jpg` → `D:\Album-F\2026-06\IMG_1234-2026-06-08_19.14.27.jpg`

## Streamlit UI (optional)

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run app.py
```

The sidebar mirrors the CLI options. Use **Refresh devices**, **Start Import**, **Verify Transfer**, then confirm and **Delete from Phone**. Do not run the CLI and Streamlit at the same time.

## Update dependencies

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install --upgrade -r requirements.txt
```
