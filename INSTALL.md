# Backer — Installation

Backer stores rsync backups on a Raspberry Pi (`framboise`) with a USB SSD, and serves a web dashboard showing what is stored and how to get it back.

## Prerequisites

- Raspberry Pi running Raspberry Pi OS (or any Debian-based distro)
- USB SSD attached and visible to the OS (e.g. `/dev/sda1`)
- SSH access to framboise from your client machines, ideally with key-based auth
- Python 3 on framboise (pre-installed on Raspberry Pi OS)
- `rsync` on both framboise and your client machines

## 1. Prepare the USB SSD

SSH into framboise first, then work through the steps below.

```bash
ssh framboise
```

### Discover the drive

Plug in the USB SSD, then run:

```bash
lsblk
```

Look for a disk roughly the right size with no mount point. It will appear as `/dev/sda`, `/dev/sdb`, etc. A drive that already has a partition will show a child entry like `/dev/sda1`. Example output:

```
NAME   MAJ:MIN RM   SIZE RO TYPE MOUNTPOINT
sda      8:0    0 931.5G  0 disk
└─sda1   8:1    0 931.5G  0 part
mmcblk0...
```

If you are not sure which device just appeared, check the kernel log immediately after plugging in:

```bash
dmesg | tail -20
```

You will see lines like `[sda] Attached SCSI disk` identifying the device name.

For a detailed view of existing partitions and filesystems:

```bash
sudo fdisk -l /dev/sda
```

### Partition the drive (if it has no partition table)

Skip this step if `lsblk` already shows a partition (e.g. `/dev/sda1`).

```bash
sudo fdisk /dev/sda
```

Inside `fdisk`:

```
Command: g        # create a new GPT partition table
Command: n        # add a new partition
Partition number: 1
First sector:     (press Enter for default)
Last sector:      (press Enter to use the whole disk)
Command: w        # write and exit
```

You should now have `/dev/sda1`.

### Format the drive

**ext4** is recommended — it is the standard Linux filesystem, handles large files well, and is fully supported on the Pi. Use exFAT only if you need to read the drive directly on a Mac or Windows machine without going through SSH.

```bash
# ext4 — recommended
sudo mkfs.ext4 -L backup /dev/sda1

# exFAT — cross-platform alternative (install tools first if needed)
sudo apt install exfatprogs
sudo mkfs.exfat -n backup /dev/sda1
```

The `-L backup` flag sets a human-readable label; the installer uses the UUID for fstab so the label is just cosmetic.

### Test the mount

```bash
sudo mount /dev/sda1 /mnt/backup
df -h /mnt/backup
```

You should see the drive's capacity reported. Unmount again before running the installer (the installer will re-mount it):

```bash
sudo umount /mnt/backup
```

If the mount fails with `wrong fs type` or `unknown filesystem`, the drive may need formatting (see above) or may need `exfatprogs` installed.

## 2. Clone the repo onto framboise

```bash
ssh framboise
git clone https://github.com/nryberg/backer.git
cd backer
```

## 3. Run the installer

The installer mounts the USB SSD, copies the server script to `/opt/backer/`, and registers a systemd service that starts on boot.

```bash
# Find your USB SSD device name first
lsblk

# Run the installer as root, passing the device
sudo bash framboise/install.sh /dev/sda1
```

The installer will:
- Mount `/dev/sda1` at `/mnt/backup` and add it to `/etc/fstab` with `nofail`
- Copy `server.py` to `/opt/backer/`
- Install and start the `backup-dashboard` systemd service on port **8765**

If your SSD is already mounted elsewhere, pass both arguments:

```bash
sudo bash framboise/install.sh /dev/sda1 /mnt/myssd
```

If you want to handle mounting yourself and only install the service:

```bash
sudo bash framboise/install.sh
```

### Verify it is running

```bash
sudo systemctl status backup-dashboard
```

Then open `http://framboise:8765` in a browser. The dashboard will say "No backups found" until you push something.

## 4. Set up SSH key access (if you haven't already)

The client push script connects to framboise twice per run (once for `mkdir`, once to write the manifest). Password prompts will interrupt the sync, so key-based auth is strongly recommended.

On each client machine:

```bash
ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)"   # skip if you already have a key
ssh-copy-id framboise
```

## 5. Push data from a client machine

Copy `client/push.sh` to any machine you want to back up, then run it pointing at the directory you want to sync.

```bash
# Create ~/bin if it doesn't exist, then copy the script
mkdir -p ~/bin
scp framboise:~/backer/client/push.sh ~/bin/push-to-framboise
chmod +x ~/bin/push-to-framboise

# If ~/bin was just created, reload your shell config so it appears in $PATH
source ~/.bashrc   # or: source ~/.profile
```

If `push-to-framboise` is still not found after reloading, `~/bin` is not in your `$PATH`. Add it permanently using the block below for your shell, then reload:

**bash** (`~/.bashrc`):
```bash
cat >> ~/.bashrc <<'EOF'

# add ~/bin to PATH
export PATH="$HOME/bin:$PATH"
EOF
source ~/.bashrc
```

**zsh** (`~/.zshrc`):
```bash
cat >> ~/.zshrc <<'EOF'

# add ~/bin to PATH
export PATH="$HOME/bin:$PATH"
EOF
source ~/.zshrc
```

Confirm it worked:
```bash
which push-to-framboise   # should print ~/bin/push-to-framboise
```

```bash
# Back up a directory
push-to-framboise /home/nick/documents
push-to-framboise /etc
push-to-framboise /var/www/html
```

The script will print the source and destination paths before syncing:

```
Source : nick@mylaptop:/home/nick/documents/
Dest   : framboise:/mnt/backup/mylaptop/home/nick/documents/

sending incremental file list
...
Done. Dashboard: http://framboise:8765
```

If `framboise` is not resolvable by that name on your network, pass the hostname or IP as a second argument:

```bash
push-to-framboise /home/nick/documents framboise.local
push-to-framboise /home/nick/documents 192.168.1.42
```

## Storage layout

Each backed-up path is stored under the source machine's hostname, preserving the full original path:

```
/mnt/backup/
  mylaptop/
    home/nick/documents/
    etc/
  webserver/
    var/www/html/
```

A hidden `.backer-info` file is written at each backup root so the dashboard can display the original source path and last-push timestamp.

## Dashboard

Open `http://framboise:8765` to see:

- Total machines, size, and file count across all backups
- Per-machine breakdown of every backed-up path with size, file count, and last sync time
- **Copy buttons** for the rsync commands to restore or re-push each path

The **restore** command pulls data from framboise back to its original location:

```bash
rsync -avz --progress framboise:/mnt/backup/mylaptop/home/nick/documents/ /home/nick/documents/
```

The **push** command re-syncs from the source to framboise (same as running `push.sh` manually):

```bash
rsync -avz --progress --delete nick@mylaptop:/home/nick/documents/ framboise:/mnt/backup/mylaptop/home/nick/documents/
```

The dashboard page reloads on demand via the refresh button — it scans the disk on every request, so it always reflects the current state.

## Service management

```bash
# View logs
sudo journalctl -u backup-dashboard -f

# Restart after updating server.py
sudo systemctl restart backup-dashboard

# Stop / disable
sudo systemctl stop backup-dashboard
sudo systemctl disable backup-dashboard
```

## Configuration

The service reads three environment variables. Edit `/etc/systemd/system/backup-dashboard.service` to change them, then reload:

| Variable | Default | Description |
|---|---|---|
| `BACKUP_ROOT` | `/mnt/backup` | Path where backups are stored |
| `PORT` | `8765` | HTTP port for the dashboard |
| `FRAMBOISE_HOST` | `framboise` | Hostname used in generated rsync commands |

```bash
sudo systemctl daemon-reload
sudo systemctl restart backup-dashboard
```
