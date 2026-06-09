# Backer

Simple rsync backup storage for a Raspberry Pi with a USB SSD, plus a web dashboard to see what's stored and copy the commands to get it back.

## How it works

Run `push.sh` from any machine to sync a directory to framboise. The full source path is preserved under your machine's hostname:

```
/mnt/backup/
  mylaptop/
    home/nick/documents/
    home/nick/projects/
  webserver/
    var/www/html/
```

The dashboard at `http://framboise:8765` shows every backed-up path with size, file count, last sync time, and copy buttons for three commands:

| Command | What it does |
|---|---|
| **restore** | Pull from framboise back to the original path |
| **fetch** | Pull from framboise into your current directory |
| **push** | Re-sync from the original source to framboise |

## Quick start

**On framboise** — install the dashboard service:

```bash
git clone https://github.com/nryberg/backer.git
cd backer
sudo bash framboise/install.sh /dev/sda2   # pass your USB SSD partition
```

**On any machine you want to back up** — get the push script:

```bash
mkdir -p ~/bin
scp framboise:~/backer/client/push.sh ~/bin/push-to-framboise
chmod +x ~/bin/push-to-framboise
source ~/.bashrc

push-to-framboise ~/documents
push-to-framboise /etc
```

See [INSTALL.md](INSTALL.md) for the full setup guide including USB drive discovery, formatting, SSH key setup, and automation with cron/launchd.

## Files

```
framboise/
  server.py                  web dashboard (stdlib only, no dependencies)
  install.sh                 setup script — mounts SSD, installs systemd service
  backup-dashboard.service   systemd unit file
client/
  push.sh                    rsync wrapper to run on source machines
```

## Dashboard

`http://framboise:8765` — summary of all backups with copy buttons.

`http://framboise:8765/how-to` — step-by-step CLI backup guide with ready-to-copy commands.

Both pages support dark/light mode with a toggle button that persists your preference.
