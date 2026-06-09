#!/usr/bin/env python3
"""
Backer Dashboard — serves an HTML summary of backups stored on framboise.
Listens on 0.0.0.0:8765 by default; configure via environment variables.

  BACKUP_ROOT    path to USB SSD mount     default: /mnt/backup
  PORT           HTTP port                 default: 8765
  FRAMBOISE_HOST hostname used in commands default: framboise
"""

import html
import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BACKUP_ROOT = Path(os.environ.get("BACKUP_ROOT", "/mnt/backup"))
PORT = int(os.environ.get("PORT", "8765"))
HOST = "0.0.0.0"
FRAMBOISE_HOST = os.environ.get("FRAMBOISE_HOST", "framboise")
MANIFEST_FILENAME = ".backer-info"


def human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024.0:
            return f"{size_bytes:.0f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.0f} PB"


def dir_stats(path: Path) -> tuple[int, int, float]:
    """Return (total_bytes, file_count, newest_mtime) for a directory tree."""
    total, count, newest = 0, 0, 0.0
    try:
        for entry in path.rglob("*"):
            if entry.name == MANIFEST_FILENAME:
                continue
            if entry.is_file(follow_symlinks=False):
                try:
                    st = entry.stat()
                    total += st.st_size
                    count += 1
                    if st.st_mtime > newest:
                        newest = st.st_mtime
                except OSError:
                    pass
    except OSError:
        pass
    return total, count, newest


def get_backups() -> dict[str, list[dict]]:
    """
    Walk BACKUP_ROOT for .backer-info marker files written by client/push.sh.
    Falls back to showing top-level directories for hosts without markers.
    Returns {hostname: [entry, ...]}
    """
    result: dict[str, list] = {}
    if not BACKUP_ROOT.exists():
        return result

    seen_dirs: set[Path] = set()

    # Primary: look for .backer-info markers placed by push.sh
    for manifest_file in sorted(BACKUP_ROOT.rglob(MANIFEST_FILENAME)):
        backup_dir = manifest_file.parent
        seen_dirs.add(backup_dir)
        try:
            info = json.loads(manifest_file.read_text())
        except (json.JSONDecodeError, OSError):
            info = {}

        # Derive hostname from the path relative to BACKUP_ROOT if not in manifest
        try:
            rel = backup_dir.relative_to(BACKUP_ROOT)
            hostname = rel.parts[0]
        except (ValueError, IndexError):
            hostname = info.get("source_host", "unknown")

        source_path = info.get("source_path", "/" + "/".join(rel.parts[1:]))
        source_user = info.get("source_user", "")

        size, count, newest = dir_stats(backup_dir)
        result.setdefault(hostname, []).append(
            _make_entry(hostname, source_path, source_user, backup_dir, size, count, newest)
        )

    # Filesystem artifacts that appear at the root of ext4/exFAT/NTFS volumes
    _FS_SKIP = {"lost+found", "$RECYCLE.BIN", ".Spotlight-V100", ".fseventsd", "System Volume Information"}

    # Fallback: host dirs that have no markers at all
    try:
        top_entries = sorted(BACKUP_ROOT.iterdir())
    except PermissionError:
        return result

    for host_dir in top_entries:
        if not host_dir.is_dir():
            continue
        if host_dir.name in _FS_SKIP or host_dir.name.startswith("."):
            continue
        hostname = host_dir.name
        if hostname in result:
            continue
        # Show top-level subdirs as backup roots
        try:
            children = sorted(host_dir.iterdir())
        except PermissionError:
            continue
        for child in children:
            if not child.is_dir() or child in seen_dirs:
                continue
            source_path = "/" + child.name
            size, count, newest = dir_stats(child)
            result.setdefault(hostname, []).append(
                _make_entry(hostname, source_path, "", child, size, count, newest)
            )

    return result


def _make_entry(
    hostname: str,
    source_path: str,
    source_user: str,
    backup_dir: Path,
    size: int,
    count: int,
    newest: float,
) -> dict:
    dest = str(backup_dir)
    user_prefix = f"{source_user}@{hostname}:" if source_user else f"{hostname}:"
    return {
        "source_path": source_path,
        "source_user": source_user,
        "backup_dir": dest,
        "size": size,
        "size_human": human_size(size),
        "files": count,
        "last_sync_str": (
            datetime.fromtimestamp(newest).strftime("%Y-%m-%d") if newest else "—"
        ),
        # pull from framboise to original location
        "restore_cmd": (
            f"rsync -avz --progress {FRAMBOISE_HOST}:{dest}/ {source_path}/"
        ),
        # pull from framboise into current working directory
        "fetch_cmd": (
            f"rsync -avz --progress {FRAMBOISE_HOST}:{dest}/ ./"
        ),
        # push from original location back to framboise
        "push_cmd": (
            f"rsync -avz --progress --delete {user_prefix}{source_path}/ {FRAMBOISE_HOST}:{dest}/"
        ),
    }


# ---------------------------------------------------------------------------
# HTML rendering — shared chrome
# ---------------------------------------------------------------------------

_CSS = """
  :root {
    --bg: #f8f9fa; --fg: #212529; --muted: #6c757d;
    --border: #dee2e6; --card: #fff; --accent: #0d6efd;
    --code-bg: #e9ecef; --btn: #0d6efd; --btn-fg: #fff; --btn-ok: #198754;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --fg: #c9d1d9; --muted: #8b949e;
      --border: #30363d; --card: #161b22; --accent: #58a6ff;
      --code-bg: #1c2128; --btn: #1f6feb; --btn-fg: #fff; --btn-ok: #238636;
    }
  }
  html.dark {
    --bg: #0d1117; --fg: #c9d1d9; --muted: #8b949e;
    --border: #30363d; --card: #161b22; --accent: #58a6ff;
    --code-bg: #1c2128; --btn: #1f6feb; --btn-fg: #fff; --btn-ok: #238636;
  }
  html.light {
    --bg: #f8f9fa; --fg: #212529; --muted: #6c757d;
    --border: #dee2e6; --card: #fff; --accent: #0d6efd;
    --code-bg: #e9ecef; --btn: #0d6efd; --btn-fg: #fff; --btn-ok: #198754;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--fg); padding: 1.5rem 2rem; }
  a { color: var(--accent); }
  header { margin-bottom: 1.5rem; }
  h1 { font-size: 1.5rem; font-weight: 700; }
  h1 em { color: var(--accent); font-style: normal; }
  nav.topnav { font-size: 0.83rem; margin-top: 0.5rem; }
  nav.topnav a { margin-right: 1.2rem; }
  .meta { color: var(--muted); font-size: 0.83rem; margin-top: 0.3rem; }
  /* dashboard */
  .stats { display: flex; gap: 1.25rem; margin: 1.25rem 0 2rem; flex-wrap: wrap; }
  .stat { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 0.7rem 1.2rem; min-width: 7rem; }
  .stat .val { font-size: 1.35rem; font-weight: 700; color: var(--accent); }
  .stat .lbl { font-size: 0.75rem; color: var(--muted); margin-top: 0.15rem;
               text-transform: uppercase; letter-spacing: 0.04em; }
  .machine { background: var(--card); border: 1px solid var(--border);
             border-radius: 8px; margin-bottom: 1.5rem; overflow: hidden; }
  .machine h2 { padding: 0.85rem 1.1rem; border-bottom: 1px solid var(--border);
                font-size: 1rem; display: flex; align-items: baseline; gap: 0.6rem; }
  .machine h2 .total { font-size: 0.82rem; color: var(--muted); font-weight: 400; }
  table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
  thead { background: var(--code-bg); }
  th { text-align: left; padding: 0.45rem 0.85rem; font-weight: 600;
       font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;
       color: var(--muted); }
  td { padding: 0.55rem 0.85rem; border-top: 1px solid var(--border); vertical-align: top; }
  .col-path { font-family: ui-monospace, monospace; font-size: 0.84rem; }
  .col-num  { text-align: right; white-space: nowrap; color: var(--muted); }
  .col-cmds { min-width: 26rem; }
  .cmd-row  { display: flex; align-items: center; gap: 0.35rem; margin-bottom: 0.3rem; }
  .cmd-row:last-child { margin-bottom: 0; }
  .cmd-label { font-size: 0.7rem; color: var(--muted); width: 3rem; flex-shrink: 0;
               text-transform: uppercase; letter-spacing: 0.03em; }
  .cmd-text { font-family: ui-monospace, monospace; font-size: 0.75rem;
              background: var(--code-bg); padding: 0.2rem 0.45rem; border-radius: 4px;
              flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
              max-width: 34rem; display: block; color: var(--fg); }
  .copy-btn { flex-shrink: 0; font-size: 0.82rem; font-weight: 600;
              padding: 0.35rem 0.85rem;
              background: var(--btn); color: var(--btn-fg); border: none;
              border-radius: 5px; cursor: pointer; white-space: nowrap; }
  .copy-btn:hover  { opacity: 0.85; }
  .copy-btn.copied { background: var(--btn-ok); }
  /* search */
  .search-wrap { margin-bottom: 1.5rem; }
  #search { width: 100%; max-width: 28rem; padding: 0.5rem 0.85rem;
            font-size: 0.95rem; border: 1px solid var(--border);
            border-radius: 6px; background: var(--card); color: var(--fg);
            outline: none; }
  #search:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent); }
  .machine.hidden { display: none; }
  tr.hidden { display: none; }
  .empty { padding: 2rem; color: var(--muted); text-align: center; }
  .refresh { font-size: 0.8rem; color: var(--accent); cursor: pointer;
             background: none; border: none; text-decoration: underline; }
  .hdr-actions { float: right; display: flex; gap: 0.75rem; align-items: center; }
  .theme-toggle { font-size: 0.8rem; color: var(--muted); cursor: pointer;
                  background: none; border: 1px solid var(--border);
                  border-radius: 4px; padding: 0.15rem 0.5rem; }
  /* how-to page */
  .guide { max-width: 52rem; }
  .guide section { margin-bottom: 2.25rem; }
  .guide h2 { font-size: 1.05rem; font-weight: 700; margin-bottom: 0.75rem;
              padding-bottom: 0.4rem; border-bottom: 1px solid var(--border); }
  .guide p { line-height: 1.65; margin-bottom: 0.7rem; }
  .guide p:last-child { margin-bottom: 0; }
  .guide ul { padding-left: 1.4rem; line-height: 1.8; }
  .codeblock { position: relative; background: var(--code-bg); border: 1px solid var(--border);
               border-radius: 6px; margin: 0.75rem 0; }
  .codeblock pre { font-family: ui-monospace, monospace; font-size: 0.82rem; line-height: 1.55;
                   padding: 0.85rem 3.5rem 0.85rem 1rem; overflow-x: auto; color: var(--fg); }
  .codeblock .copy-btn { position: absolute; top: 0.45rem; right: 0.45rem; }
  .codeblock .label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em;
                      color: var(--muted); padding: 0.35rem 1rem 0 1rem; display: block; }
  .mapping { font-family: ui-monospace, monospace; font-size: 0.82rem; line-height: 1.9;
             background: var(--code-bg); border: 1px solid var(--border);
             border-radius: 6px; padding: 0.9rem 1.1rem; margin: 0.75rem 0; }
  .mapping .arr { color: var(--accent); }
  /* shared footer */
  footer { color: var(--muted); font-size: 0.78rem; margin-top: 2rem; }
"""

_JS = """
  function markCopied(btn) {
    const orig = btn.textContent;
    btn.textContent = 'copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 1600);
  }
  function copyFallback(text, btn) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try { document.execCommand('copy'); markCopied(btn); } catch(e) {}
    document.body.removeChild(ta);
  }
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const cmd = btn.dataset.cmd;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(cmd).then(() => markCopied(btn)).catch(() => copyFallback(cmd, btn));
      } else {
        copyFallback(cmd, btn);
      }
    });
  });
  const refreshBtn = document.getElementById('refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', () => location.reload());

  const searchBox = document.getElementById('search');
  if (searchBox) {
    searchBox.addEventListener('input', () => {
      const q = searchBox.value.trim().toLowerCase();
      document.querySelectorAll('.machine').forEach(machine => {
        const host = (machine.dataset.host || '').toLowerCase();
        let anyVisible = false;
        machine.querySelectorAll('tbody tr').forEach(row => {
          const path = (row.querySelector('.col-path') || {}).textContent || '';
          const match = !q || host.includes(q) || path.toLowerCase().includes(q);
          row.classList.toggle('hidden', !match);
          if (match) anyVisible = true;
        });
        machine.classList.toggle('hidden', !anyVisible);
      });
    });
    searchBox.addEventListener('keydown', e => {
      if (e.key === 'Escape') { searchBox.value = ''; searchBox.dispatchEvent(new Event('input')); }
    });
  }

  (function() {
    const root = document.documentElement;
    const btn  = document.getElementById('theme-toggle');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const saved = localStorage.getItem('backer-theme');
    const isDark = saved ? saved === 'dark' : prefersDark;
    function apply(dark) {
      root.classList.toggle('dark', dark);
      root.classList.toggle('light', !dark);
      btn.textContent = dark ? 'light' : 'dark';
    }
    apply(isDark);
    btn.addEventListener('click', () => {
      const nowDark = !root.classList.contains('dark');
      localStorage.setItem('backer-theme', nowDark ? 'dark' : 'light');
      apply(nowDark);
    });
  })();
"""


def _page(title: str, body: str) -> str:
    fh = html.escape(FRAMBOISE_HOST)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <header>
    <div class="hdr-actions">
      <button class="theme-toggle" id="theme-toggle" title="Toggle dark/light mode">dark</button>
    </div>
    <h1>Backer &mdash; <em>{fh}</em></h1>
    <nav class="topnav">
      <a href="/">Dashboard</a><a href="/how-to">How to back up</a>
    </nav>
  </header>
  {body}
  <footer>Backer &bull; backup root: <code>{html.escape(str(BACKUP_ROOT))}</code></footer>
  <script>{_JS}</script>
</body>
</html>"""


def _codeblock(code: str, label: str = "") -> str:
    escaped_html = html.escape(code)
    # Encode newlines as &#10; so the attribute value is safe across all browsers.
    # (Literal newlines in attributes are spec-valid but handled inconsistently.)
    escaped_attr = escaped_html.replace('\n', '&#10;')
    lbl = f'<span class="label">{html.escape(label)}</span>' if label else ""
    return (
        f'<div class="codeblock">{lbl}'
        f'<pre>{escaped_html}</pre>'
        f'<button class="copy-btn" data-cmd="{escaped_attr}">copy</button>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Dashboard page
# ---------------------------------------------------------------------------

def _row(e: dict) -> str:
    sp = html.escape(e["source_path"])
    restore = html.escape(e["restore_cmd"])
    fetch = html.escape(e["fetch_cmd"])
    push = html.escape(e["push_cmd"])
    return (
        f'<tr>'
        f'<td class="col-path">{sp}</td>'
        f'<td class="col-num">{html.escape(e["size_human"])}</td>'
        f'<td class="col-num">{e["files"]:,}</td>'
        f'<td class="col-num">{html.escape(e["last_sync_str"])}</td>'
        f'<td class="col-cmds">'
        f'  <div class="cmd-row">'
        f'    <button class="copy-btn" data-cmd="{restore}">copy</button>'
        f'    <span class="cmd-label">restore</span>'
        f'    <code class="cmd-text" title="{restore}">{restore}</code>'
        f'  </div>'
        f'  <div class="cmd-row">'
        f'    <button class="copy-btn" data-cmd="{fetch}">copy</button>'
        f'    <span class="cmd-label">fetch</span>'
        f'    <code class="cmd-text" title="{fetch}">{fetch}</code>'
        f'  </div>'
        f'  <div class="cmd-row">'
        f'    <button class="copy-btn" data-cmd="{push}">copy</button>'
        f'    <span class="cmd-label">push</span>'
        f'    <code class="cmd-text" title="{push}">{push}</code>'
        f'  </div>'
        f'</td>'
        f'</tr>'
    )


def render_dashboard(backups: dict[str, list]) -> str:
    total_size = sum(e["size"] for entries in backups.values() for e in entries)
    total_files = sum(e["files"] for entries in backups.values() for e in entries)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    machines_html = ""
    for hostname, entries in sorted(backups.items()):
        host_size = human_size(sum(e["size"] for e in entries))
        rows = "".join(_row(e) for e in sorted(entries, key=lambda x: x["source_path"]))
        machines_html += (
            f'<div class="machine" data-host="{html.escape(hostname)}">'
            f'  <h2><strong>{html.escape(hostname)}</strong>'
            f'      <span class="total">{html.escape(host_size)}</span></h2>'
            f'  <table>'
            f'    <thead><tr><th>Path</th><th>Size</th><th>Files</th>'
            f'               <th>Last&nbsp;Sync</th><th>Commands</th></tr></thead>'
            f'    <tbody>{rows}</tbody>'
            f'  </table>'
            f'</div>'
        )

    if not backups:
        machines_html = (
            '<p class="empty">No backups found. '
            f'Is <code>{html.escape(str(BACKUP_ROOT))}</code> mounted? '
            f'See <a href="/how-to">how to back up</a>.</p>'
        )

    body = (
        f'<p class="meta">Last loaded: {now}</p>'
        f'<button id="refresh" class="refresh">&#x21bb; refresh</button>'
        f'<div class="stats">'
        f'  <div class="stat"><div class="val">{len(backups)}</div><div class="lbl">Machines</div></div>'
        f'  <div class="stat"><div class="val">{human_size(total_size)}</div><div class="lbl">Total size</div></div>'
        f'  <div class="stat"><div class="val">{total_files:,}</div><div class="lbl">Files</div></div>'
        f'</div>'
        f'<div class="search-wrap">'
        f'  <input id="search" type="search" placeholder="Filter by hostname or path&hellip;" autocomplete="off">'
        f'</div>'
        f'{machines_html}'
    )
    return _page(f"Backer — {FRAMBOISE_HOST}", body)


# ---------------------------------------------------------------------------
# How-to page
# ---------------------------------------------------------------------------

def render_howto() -> str:
    fh = html.escape(FRAMBOISE_HOST)
    br = html.escape(str(BACKUP_ROOT))

    get_script = _codeblock(
        f"mkdir -p ~/bin\n"
        f"scp {FRAMBOISE_HOST}:~/backer/client/push.sh ~/bin/push-to-{FRAMBOISE_HOST}\n"
        f"chmod +x ~/bin/push-to-{FRAMBOISE_HOST}",
        label="get the script (run on your machine)",
    )

    use_script = _codeblock(
        f"push-to-{FRAMBOISE_HOST} /path/to/directory\n\n"
        f"# examples\n"
        f"push-to-{FRAMBOISE_HOST} ~/documents\n"
        f"push-to-{FRAMBOISE_HOST} ~/projects\n"
        f"push-to-{FRAMBOISE_HOST} /etc\n"
        f"push-to-{FRAMBOISE_HOST} /var/www/html",
        label="run it",
    )

    manual_rsync = _codeblock(
        f"rsync -avz --progress --delete \\\n"
        f"    /path/to/directory/ \\\n"
        f"    {FRAMBOISE_HOST}:{BACKUP_ROOT}/$(hostname -s)/path/to/directory/",
        label="manual rsync (equivalent)",
    )

    ssh_setup = _codeblock(
        "ssh-keygen -t ed25519 -C \"$(whoami)@$(hostname)\"  # skip if you have a key already\n"
        f"ssh-copy-id {FRAMBOISE_HOST}",
        label="one-time SSH key setup",
    )

    cron_daily = _codeblock(
        f"# crontab -e\n"
        f"0 2 * * *  push-to-{FRAMBOISE_HOST} ~/documents >> ~/logs/backer.log 2>&1\n"
        f"0 2 * * *  push-to-{FRAMBOISE_HOST} ~/projects  >> ~/logs/backer.log 2>&1",
        label="crontab — run daily at 2 am",
    )

    cron_launchd = _codeblock(
        "# save as ~/Library/LaunchAgents/com.backer.documents.plist\n"
        "# then: launchctl load ~/Library/LaunchAgents/com.backer.documents.plist\n\n"
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\"\n"
        "  \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\"><dict>\n"
        "  <key>Label</key><string>com.backer.documents</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        f"    <string>/Users/YOU/bin/push-to-{FRAMBOISE_HOST}</string>\n"
        "    <string>/Users/YOU/documents</string>\n"
        "  </array>\n"
        "  <key>StartCalendarInterval</key>\n"
        "  <dict><key>Hour</key><integer>2</integer>"
        "<key>Minute</key><integer>0</integer></dict>\n"
        "  <key>StandardOutPath</key><string>/tmp/backer.log</string>\n"
        "  <key>StandardErrorPath</key><string>/tmp/backer.log</string>\n"
        "</dict></plist>",
        label="macOS launchd — run daily at 2 am",
    )

    mapping = (
        f'<div class="mapping">'
        f'your machine: ~/documents/<br>'
        f'<span class="arr">&nbsp;&nbsp;&darr; rsync</span><br>'
        f'{fh}: {br}/&lt;hostname&gt;/Users/YOU/documents/'
        f'</div>'
    )

    restore_cmd = (
        f"rsync -avz --progress \\\n"
        f"    {FRAMBOISE_HOST}:{BACKUP_ROOT}/<hostname>/path/to/directory/ \\\n"
        f"    /path/to/directory/"
    )
    restore_example = _codeblock(restore_cmd)

    path_bash = _codeblock(
        "cat >> ~/.bashrc <<'EOF'\n\n"
        "# add ~/bin to PATH\n"
        "export PATH=\"$HOME/bin:$PATH\"\n"
        "EOF\n"
        "source ~/.bashrc",
        label="bash (~/.bashrc)",
    )

    path_zsh = _codeblock(
        "cat >> ~/.zshrc <<'EOF'\n\n"
        "# add ~/bin to PATH\n"
        "export PATH=\"$HOME/bin:$PATH\"\n"
        "EOF\n"
        "source ~/.zshrc",
        label="zsh (~/.zshrc)",
    )

    path_verify = _codeblock(
        f"which push-to-{FRAMBOISE_HOST}",
        label="confirm it worked",
    )

    body = f"""
<div class="guide">

  <section>
    <h2>Prerequisites</h2>
    <p>The push script uses SSH to connect to {fh}. Set up a key so it
    does not prompt for a password on every run:</p>
    {ssh_setup}
    <p>You also need <code>rsync</code> installed on your machine
    (<code>brew install rsync</code> on macOS, or it is usually pre-installed on Linux).</p>
  </section>

  <section>
    <h2>Quick start with push.sh</h2>
    <p><code>client/push.sh</code> wraps rsync with sensible defaults and writes a small
    marker file so the dashboard can track the original source path.</p>
    {get_script}
    {use_script}
    <p>The script prints the source and destination before syncing, then updates the
    dashboard automatically. Open <a href="/">the dashboard</a> to confirm.</p>
  </section>

  <section>
    <h2>If push-to-{fh} is not found</h2>
    <p><code>~/bin</code> is only added to <code>$PATH</code> at login time, so if you
    just created it you need to add it permanently and reload your shell config.</p>
    <p><strong>bash</strong></p>
    {path_bash}
    <p><strong>zsh</strong> (default on macOS)</p>
    {path_zsh}
    {path_verify}
    <p>Not sure which shell you are using? Run <code>echo $SHELL</code>.</p>
  </section>

  <section>
    <h2>Where files go</h2>
    <p>The full source path is preserved under your machine's hostname, so restoring
    to the original location is always unambiguous:</p>
    {mapping}
    <p>A hidden <code>.backer-info</code> file is written at each backup root recording
    the original host, path, user, and timestamp.</p>
  </section>

  <section>
    <h2>Manual rsync (without push.sh)</h2>
    <p>If you prefer to call rsync directly, mirror the same path convention:</p>
    {manual_rsync}
    <p><code>--delete</code> removes files from the backup that no longer exist at the
    source, keeping the backup an exact mirror. Omit it if you want the backup to
    accumulate deleted files.</p>
  </section>

  <section>
    <h2>Automating backups</h2>
    <p><strong>Linux / Raspberry Pi OS (cron)</strong></p>
    {cron_daily}
    <p><strong>macOS (launchd)</strong></p>
    {cron_launchd}
  </section>

  <section>
    <h2>Restoring files</h2>
    <p>Every backed-up path on the <a href="/">dashboard</a> has a pre-built
    <strong>restore</strong> copy button. The general form is:</p>
    {restore_example}
    <p>Add <code>--dry-run</code> first to preview what would change without
    touching any files.</p>
  </section>

</div>"""

    return _page(f"How to back up — {FRAMBOISE_HOST}", body)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = render_dashboard(get_backups()).encode("utf-8")
        elif self.path == "/how-to":
            body = render_howto().encode("utf-8")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request log noise; errors still go to stderr


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), DashboardHandler)
    print(f"Backer dashboard → http://{HOST}:{PORT}  (root: {BACKUP_ROOT})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
