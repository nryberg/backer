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
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


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

    # Fallback: host dirs that have no markers at all
    for host_dir in sorted(BACKUP_ROOT.iterdir()):
        if not host_dir.is_dir():
            continue
        hostname = host_dir.name
        if hostname in result:
            continue
        # Show top-level subdirs as backup roots
        for child in sorted(host_dir.iterdir()):
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
            datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M") if newest else "—"
        ),
        # pull from framboise to original location
        "restore_cmd": (
            f"rsync -avz --progress {FRAMBOISE_HOST}:{dest}/ {source_path}/"
        ),
        # push from original location back to framboise
        "push_cmd": (
            f"rsync -avz --progress --delete {user_prefix}{source_path}/ {FRAMBOISE_HOST}:{dest}/"
        ),
    }


# ---------------------------------------------------------------------------
# HTML rendering
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
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--fg); padding: 1.5rem 2rem; }
  a { color: var(--accent); }
  header { margin-bottom: 1.5rem; }
  h1 { font-size: 1.5rem; font-weight: 700; }
  h1 em { color: var(--accent); font-style: normal; }
  .meta { color: var(--muted); font-size: 0.83rem; margin-top: 0.3rem; }
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
  .copy-btn { flex-shrink: 0; font-size: 0.73rem; padding: 0.2rem 0.55rem;
              background: var(--btn); color: var(--btn-fg); border: none;
              border-radius: 4px; cursor: pointer; white-space: nowrap; }
  .copy-btn:hover  { opacity: 0.85; }
  .copy-btn.copied { background: var(--btn-ok); }
  .empty { padding: 2rem; color: var(--muted); text-align: center; }
  footer { color: var(--muted); font-size: 0.78rem; margin-top: 2rem; }
  .refresh { float: right; font-size: 0.8rem; color: var(--accent); cursor: pointer;
             background: none; border: none; text-decoration: underline; }
"""

_JS = """
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(btn.dataset.cmd).then(() => {
        const orig = btn.textContent;
        btn.textContent = 'copied!';
        btn.classList.add('copied');
        setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 1600);
      });
    });
  });
  document.getElementById('refresh').addEventListener('click', () => location.reload());
"""


def _row(e: dict) -> str:
    sp = html.escape(e["source_path"])
    restore = html.escape(e["restore_cmd"])
    push = html.escape(e["push_cmd"])
    return (
        f'<tr>'
        f'<td class="col-path">{sp}</td>'
        f'<td class="col-num">{html.escape(e["size_human"])}</td>'
        f'<td class="col-num">{e["files"]:,}</td>'
        f'<td class="col-num">{html.escape(e["last_sync_str"])}</td>'
        f'<td class="col-cmds">'
        f'  <div class="cmd-row">'
        f'    <span class="cmd-label">restore</span>'
        f'    <code class="cmd-text" title="{restore}">{restore}</code>'
        f'    <button class="copy-btn" data-cmd="{restore}">copy</button>'
        f'  </div>'
        f'  <div class="cmd-row">'
        f'    <span class="cmd-label">push</span>'
        f'    <code class="cmd-text" title="{push}">{push}</code>'
        f'    <button class="copy-btn" data-cmd="{push}">copy</button>'
        f'  </div>'
        f'</td>'
        f'</tr>'
    )


def render_html(backups: dict[str, list]) -> str:
    total_size = sum(e["size"] for entries in backups.values() for e in entries)
    total_files = sum(e["files"] for entries in backups.values() for e in entries)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    machines_html = ""
    for hostname, entries in sorted(backups.items()):
        host_size = human_size(sum(e["size"] for e in entries))
        rows = "".join(_row(e) for e in sorted(entries, key=lambda x: x["source_path"]))
        machines_html += (
            f'<div class="machine">'
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
            f'Is <code>{html.escape(str(BACKUP_ROOT))}</code> mounted?</p>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Backer &mdash; {html.escape(FRAMBOISE_HOST)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <header>
    <h1>Backer &mdash; <em>{html.escape(FRAMBOISE_HOST)}</em>
      <button id="refresh" class="refresh">&#x21bb; refresh</button>
    </h1>
    <p class="meta">Last loaded: {now} &bull; {html.escape(str(BACKUP_ROOT))}</p>
  </header>
  <div class="stats">
    <div class="stat"><div class="val">{len(backups)}</div><div class="lbl">Machines</div></div>
    <div class="stat"><div class="val">{human_size(total_size)}</div><div class="lbl">Total size</div></div>
    <div class="stat"><div class="val">{total_files:,}</div><div class="lbl">Files</div></div>
  </div>
  {machines_html}
  <footer>
    Backer &bull; backup root: <code>{html.escape(str(BACKUP_ROOT))}</code>
  </footer>
  <script>{_JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        backups = get_backups()
        body = render_html(backups).encode("utf-8")
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
