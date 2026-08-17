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
import shutil
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

BACKUP_ROOT = Path(os.environ.get("BACKUP_ROOT", "/mnt/backup"))
PORT = int(os.environ.get("PORT", "8765"))
HOST = "0.0.0.0"
FRAMBOISE_HOST = os.environ.get("FRAMBOISE_HOST", "framboise")
MANIFEST_FILENAME = ".backer-info"
# Where reported job failures are kept. Outside BACKUP_ROOT so dashboard state
# never mixes with backed-up data, and in $HOME so the service user can write it.
FAILURE_LOG = Path(
    os.environ.get("BACKER_FAILURE_LOG", str(Path.home() / ".backer-failures.json"))
)
MAX_FAILURES = 25


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
            # is_file(follow_symlinks=…) needs Python 3.13; spell it out for older
            if entry.is_file() and not entry.is_symlink():
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


def days_since(newest: float) -> int | None:
    """Whole days between the newest file's mtime and now; None if unknown."""
    if not newest:
        return None
    delta = datetime.now() - datetime.fromtimestamp(newest)
    return max(delta.days, 0)


def age_class(days: int | None) -> str:
    """Staleness bucket used to colour the hero number."""
    if days is None:
        return "age-unknown"
    if days <= 7:
        return "age-ok"
    if days <= 30:
        return "age-warn"
    return "age-bad"


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
    age = days_since(newest)
    return {
        "source_path": source_path,
        "source_user": source_user,
        "backup_dir": dest,
        "size": size,
        "size_human": human_size(size),
        "files": count,
        "days_since": age,
        "age_class": age_class(age),
        "age_label": "day" if age == 1 else "days",
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
# Deletion
# ---------------------------------------------------------------------------

def delete_backup(raw_path: str) -> tuple[bool, str]:
    """
    Delete one backup directory tree. Returns (ok, message).

    Only paths that the dashboard currently lists as a backup root can be
    deleted: the request path must match one of them exactly. That rules out
    traversal, symlink escapes, and deleting BACKUP_ROOT or a whole host dir,
    without needing to reason about string prefixes.
    """
    if not raw_path:
        return False, "No path given."

    known = {
        e["backup_dir"]
        for entries in get_backups().values()
        for e in entries
    }
    if raw_path not in known:
        return False, "That path is not a listed backup — nothing was deleted."

    target = Path(raw_path)
    # Belt and braces: the real path must still sit inside BACKUP_ROOT.
    try:
        root = BACKUP_ROOT.resolve(strict=True)
        resolved = target.resolve(strict=True)
        if resolved != root and root not in resolved.parents:
            return False, "Refusing to delete a path outside the backup root."
    except OSError:
        return False, "Could not resolve that path."
    if resolved == root:
        return False, "Refusing to delete the backup root itself."

    try:
        shutil.rmtree(target)
    except OSError as exc:
        return False, f"Delete failed: {exc.strerror or exc}"

    _prune_empty_parents(target.parent)
    return True, f"Deleted {raw_path}"


def _prune_empty_parents(start: Path) -> None:
    """
    Remove now-empty parent directories left behind by a delete, stopping
    before BACKUP_ROOT and before any host directory that still has content.
    """
    try:
        root = BACKUP_ROOT.resolve()
        current = start.resolve()
    except OSError:
        return
    while current != root and root in current.parents:
        try:
            current.rmdir()  # raises OSError if not empty
        except OSError:
            return
        current = current.parent


# ---------------------------------------------------------------------------
# Reported job failures
#
# A scheduler (Clocktower) POSTs here when a backup job fails, so failures
# surface on the dashboard instead of only in the scheduler's own UI. One
# record is kept per job name — the newest — and it stays until dismissed.
# ---------------------------------------------------------------------------

def load_failures() -> list[dict]:
    try:
        data = json.loads(FAILURE_LOG.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_failures(failures: list[dict]) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        tmp = FAILURE_LOG.with_suffix(".tmp")
        tmp.write_text(json.dumps(failures, indent=2))
        tmp.replace(FAILURE_LOG)
    except OSError as exc:
        print(f"could not write failure log: {exc}", flush=True)


def record_failure(payload: dict) -> dict:
    """Store one failure report, replacing any earlier one for the same job."""
    def field(key: str, limit: int = 2000) -> str:
        value = payload.get(key)
        if value is None:
            return ""
        return str(value)[-limit:]

    entry = {
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "job_name": field("job_name", 120) or "unnamed job",
        "job_id": payload.get("job_id"),
        "run_id": payload.get("run_id"),
        "exit_code": payload.get("exit_code"),
        "stdout": field("stdout"),
        "stderr": field("stderr"),
    }
    failures = [f for f in load_failures() if f.get("job_name") != entry["job_name"]]
    failures.insert(0, entry)
    save_failures(failures[:MAX_FAILURES])
    return entry


def clear_failures() -> int:
    count = len(load_failures())
    save_failures([])
    return count


def render_failures(failures: list[dict]) -> str:
    if not failures:
        return ""
    rows = ""
    for f in failures:
        detail = (f.get("stderr") or f.get("stdout") or "").strip()
        exit_code = f.get("exit_code")
        meta = f'exit {html.escape(str(exit_code))}' if exit_code is not None else "failed"
        run = f' &middot; run {html.escape(str(f["run_id"]))}' if f.get("run_id") else ""
        rows += (
            f'<div class="fail-item">'
            f'  <div class="fail-head">'
            f'    <strong>{html.escape(str(f.get("job_name", "")))}</strong>'
            f'    <span class="fail-meta">{meta}{run} &middot; '
            f'{html.escape(str(f.get("received_at", "")))}</span>'
            f'  </div>'
            + (f'<pre class="fail-detail">{html.escape(detail[-600:])}</pre>' if detail else "")
            + f'</div>'
        )
    plural = "failure" if len(failures) == 1 else "failures"
    return (
        f'<div class="failures">'
        f'  <div class="fail-title">'
        f'    <span>&#9888; {len(failures)} reported backup {plural}</span>'
        f'    <form method="POST" action="/failures/clear">'
        f'      <button type="submit" class="fail-dismiss">dismiss</button>'
        f'    </form>'
        f'  </div>'
        f'  {rows}'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# HTML rendering — shared chrome
# ---------------------------------------------------------------------------

_CSS = """
  :root {
    --bg: #f8f9fa; --fg: #212529; --muted: #6c757d;
    --border: #dee2e6; --card: #fff; --accent: #0d6efd;
    --code-bg: #e9ecef; --btn: #0d6efd; --btn-fg: #fff; --btn-ok: #198754;
    --ok: #198754; --warn: #b76e00; --bad: #c62828;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --fg: #c9d1d9; --muted: #8b949e;
      --border: #30363d; --card: #161b22; --accent: #58a6ff;
      --code-bg: #1c2128; --btn: #1f6feb; --btn-fg: #fff; --btn-ok: #238636;
      --ok: #3fb950; --warn: #d29922; --bad: #f85149;
    }
  }
  html.dark {
    --bg: #0d1117; --fg: #c9d1d9; --muted: #8b949e;
    --border: #30363d; --card: #161b22; --accent: #58a6ff;
    --code-bg: #1c2128; --btn: #1f6feb; --btn-fg: #fff; --btn-ok: #238636;
    --ok: #3fb950; --warn: #d29922; --bad: #f85149;
  }
  html.light {
    --bg: #f8f9fa; --fg: #212529; --muted: #6c757d;
    --border: #dee2e6; --card: #fff; --accent: #0d6efd;
    --code-bg: #e9ecef; --btn: #0d6efd; --btn-fg: #fff; --btn-ok: #198754;
    --ok: #198754; --warn: #b76e00; --bad: #c62828;
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
  /* hero: days since last backup */
  .col-age  { text-align: center; white-space: nowrap; width: 6.5rem; }
  .age-num  { font-size: 2.1rem; font-weight: 700; line-height: 1;
              font-variant-numeric: tabular-nums; }
  .age-unit { font-size: 0.68rem; color: var(--muted); text-transform: uppercase;
              letter-spacing: 0.05em; margin-top: 0.2rem; }
  .age-date { font-size: 0.7rem; color: var(--muted); margin-top: 0.15rem; }
  .age-ok .age-num      { color: var(--ok); }
  .age-warn .age-num    { color: var(--warn); }
  .age-bad .age-num     { color: var(--bad); }
  .age-unknown .age-num { color: var(--muted); font-size: 1.6rem; }
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
  /* delete */
  .col-del { text-align: right; white-space: nowrap; }
  .del-btn { font-size: 0.75rem; font-weight: 600; padding: 0.3rem 0.6rem;
             background: none; color: var(--bad); border: 1px solid var(--bad);
             border-radius: 5px; cursor: pointer; }
  .del-btn:hover { background: var(--bad); color: #fff; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6);
             display: none; align-items: center; justify-content: center;
             padding: 1.5rem; z-index: 50; }
  .overlay.open { display: flex; }
  .modal { background: var(--card); border: 1px solid var(--border);
           border-top: 4px solid var(--bad); border-radius: 8px;
           max-width: 34rem; width: 100%; padding: 1.4rem 1.5rem;
           max-height: 90vh; overflow-y: auto; }
  .modal h3 { font-size: 1.1rem; margin-bottom: 0.9rem; color: var(--bad); }
  .modal p { font-size: 0.87rem; line-height: 1.6; margin-bottom: 0.7rem; }
  .modal .target { font-family: ui-monospace, monospace; font-size: 0.8rem;
                   background: var(--code-bg); border: 1px solid var(--border);
                   border-radius: 5px; padding: 0.6rem 0.75rem; margin: 0.4rem 0 0.9rem;
                   word-break: break-all; }
  .modal .facts { font-size: 0.82rem; color: var(--muted); margin-bottom: 0.9rem; }
  .modal ul.warn { list-style: none; padding: 0; margin: 0 0 1rem; }
  .modal ul.warn li { font-size: 0.85rem; line-height: 1.55; padding-left: 1.4rem;
                      position: relative; margin-bottom: 0.45rem; }
  .modal ul.warn li::before { content: "!"; position: absolute; left: 0; top: 0;
                              font-weight: 700; color: var(--bad); }
  .modal label { display: block; font-size: 0.82rem; margin-bottom: 0.35rem; }
  .modal label code { background: var(--code-bg); padding: 0.1rem 0.3rem; border-radius: 3px; }
  .modal input[type=text] { width: 100%; padding: 0.45rem 0.7rem; font-size: 0.9rem;
                            font-family: ui-monospace, monospace;
                            border: 1px solid var(--border); border-radius: 5px;
                            background: var(--bg); color: var(--fg); outline: none; }
  .modal input[type=text]:focus { border-color: var(--accent); }
  .modal-actions { display: flex; gap: 0.6rem; justify-content: flex-end;
                   margin-top: 1.2rem; }
  .btn-cancel { font-size: 0.85rem; padding: 0.45rem 1rem; background: none;
                color: var(--fg); border: 1px solid var(--border);
                border-radius: 5px; cursor: pointer; }
  .btn-danger { font-size: 0.85rem; font-weight: 600; padding: 0.45rem 1rem;
                background: var(--bad); color: #fff; border: none;
                border-radius: 5px; cursor: pointer; }
  .btn-danger:disabled { opacity: 0.4; cursor: not-allowed; }
  .banner { border-radius: 6px; padding: 0.7rem 1rem; margin-bottom: 1.25rem;
            font-size: 0.87rem; border: 1px solid var(--border); background: var(--card); }
  .banner.ok   { border-left: 4px solid var(--ok); }
  .banner.err  { border-left: 4px solid var(--bad); }
  .banner code { font-family: ui-monospace, monospace; font-size: 0.8rem; }
  /* reported failures */
  .failures { border: 1px solid var(--bad); border-left: 4px solid var(--bad);
              border-radius: 6px; background: var(--card); padding: 0.85rem 1rem;
              margin-bottom: 1.5rem; }
  .fail-title { display: flex; align-items: center; justify-content: space-between;
                gap: 1rem; font-size: 0.9rem; font-weight: 700; color: var(--bad);
                margin-bottom: 0.6rem; }
  .fail-dismiss { font-size: 0.75rem; padding: 0.25rem 0.7rem; background: none;
                  color: var(--muted); border: 1px solid var(--border);
                  border-radius: 5px; cursor: pointer; }
  .fail-dismiss:hover { color: var(--fg); }
  .fail-item { padding: 0.5rem 0; border-top: 1px solid var(--border); }
  .fail-item:first-of-type { border-top: none; }
  .fail-head { display: flex; align-items: baseline; gap: 0.6rem; flex-wrap: wrap;
               font-size: 0.88rem; }
  .fail-meta { font-size: 0.78rem; color: var(--muted); }
  .fail-detail { font-family: ui-monospace, monospace; font-size: 0.75rem;
                 background: var(--code-bg); border-radius: 4px; padding: 0.5rem 0.7rem;
                 margin-top: 0.4rem; white-space: pre-wrap; word-break: break-word;
                 max-height: 9rem; overflow-y: auto; color: var(--fg); }
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
    const overlay = document.getElementById('del-overlay');
    if (!overlay) return;
    const form    = document.getElementById('del-form');
    const input   = document.getElementById('del-confirm');
    const submit  = document.getElementById('del-submit');
    let expected  = '';

    function close() {
      overlay.classList.remove('open');
      input.value = '';
      submit.disabled = true;
    }
    function sync() {
      submit.disabled = input.value.trim() !== expected;
    }
    document.querySelectorAll('.del-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const d = btn.dataset;
        expected = d.token;
        form.querySelector('input[name=path]').value = d.path;
        document.getElementById('del-target').textContent = d.path;
        document.getElementById('del-host').textContent = d.host;
        document.getElementById('del-source').textContent = d.source;
        document.getElementById('del-facts').textContent =
          d.files + ' files, ' + d.size + ', last backed up ' + d.age;
        document.getElementById('del-token').textContent = d.token;
        overlay.classList.add('open');
        input.focus();
        sync();
      });
    });
    input.addEventListener('input', sync);
    document.getElementById('del-cancel').addEventListener('click', close);
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && overlay.classList.contains('open')) close();
    });
    form.addEventListener('submit', e => {
      if (input.value.trim() !== expected) e.preventDefault();
      else submit.disabled = true;  // guard against a double submit
    });
  })();

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


def _page(title: str, body: str, footer_extra: str = "") -> str:
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
  <footer>Backer &bull; backup root: <code>{html.escape(str(BACKUP_ROOT))}</code>{footer_extra}</footer>
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

def _row(e: dict, hostname: str) -> str:
    sp = html.escape(e["source_path"])
    restore = html.escape(e["restore_cmd"])
    fetch = html.escape(e["fetch_cmd"])
    push = html.escape(e["push_cmd"])
    days = e["days_since"]
    age_num = "—" if days is None else f"{days:,}"
    age_unit = "unknown" if days is None else f'{html.escape(e["age_label"])} ago'
    return (
        f'<tr>'
        f'<td class="col-path">{sp}</td>'
        f'<td class="col-age {e["age_class"]}">'
        f'  <div class="age-num">{age_num}</div>'
        f'  <div class="age-unit">{age_unit}</div>'
        f'  <div class="age-date">{html.escape(e["last_sync_str"])}</div>'
        f'</td>'
        f'<td class="col-num">{html.escape(e["size_human"])}</td>'
        f'<td class="col-num">{e["files"]:,}</td>'
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
        f'<td class="col-del">{_delete_button(e, hostname)}</td>'
        f'</tr>'
    )


def _delete_button(e: dict, hostname: str) -> str:
    """Per-row delete trigger. All confirmation happens in the shared modal."""
    days = e["days_since"]
    age = "never (empty)" if days is None else f'{days:,} {e["age_label"]} ago'
    # Typing this word is what unlocks the delete — the last path segment,
    # which is specific enough that it cannot be confirmed by reflex.
    token = e["source_path"].rstrip("/").rsplit("/", 1)[-1] or hostname
    return (
        f'<button class="del-btn" type="button"'
        f' data-path="{html.escape(e["backup_dir"])}"'
        f' data-source="{html.escape(e["source_path"])}"'
        f' data-host="{html.escape(hostname)}"'
        f' data-size="{html.escape(e["size_human"])}"'
        f' data-files="{e["files"]:,}"'
        f' data-age="{html.escape(age)}"'
        f' data-token="{html.escape(token)}"'
        f'>delete</button>'
    )


_DELETE_MODAL = """
<div class="overlay" id="del-overlay">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="del-title">
    <h3 id="del-title">Delete this backup?</h3>
    <p>This permanently removes the backed-up copy stored on this machine:</p>
    <div class="target" id="del-target"></div>
    <div class="facts" id="del-facts"></div>
    <ul class="warn">
      <li><strong>This cannot be undone.</strong> There is no trash and no
          snapshot &mdash; the files are erased from the backup drive.</li>
      <li>If the source machine (<strong><span id="del-host"></span></strong>) is
          gone, wiped, or its copy of
          <code><span id="del-source"></span></code> has changed, this backup is
          the only copy of what it held.</li>
      <li>Files still on the source machine are <em>not</em> touched. A later
          push will recreate this backup from scratch.</li>
    </ul>
    <form method="POST" action="/delete" id="del-form">
      <input type="hidden" name="path" value="">
      <label for="del-confirm">Type <code id="del-token"></code> to confirm:</label>
      <input type="text" id="del-confirm" autocomplete="off" spellcheck="false">
      <div class="modal-actions">
        <button type="button" class="btn-cancel" id="del-cancel">Cancel</button>
        <button type="submit" class="btn-danger" id="del-submit" disabled>Delete backup</button>
      </div>
    </form>
  </div>
</div>"""


def render_dashboard(backups: dict[str, list], notice: str = "", error: str = "") -> str:
    total_size = sum(e["size"] for entries in backups.values() for e in entries)
    total_files = sum(e["files"] for entries in backups.values() for e in entries)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    banner = ""
    if error:
        banner = f'<div class="banner err">{html.escape(error)}</div>'
    elif notice:
        banner = f'<div class="banner ok">{html.escape(notice)}</div>'

    machines_html = ""
    for hostname, entries in sorted(backups.items()):
        host_size = human_size(sum(e["size"] for e in entries))
        rows = "".join(
            _row(e, hostname) for e in sorted(entries, key=lambda x: x["source_path"])
        )
        machines_html += (
            f'<div class="machine" data-host="{html.escape(hostname)}">'
            f'  <h2><strong>{html.escape(hostname)}</strong>'
            f'      <span class="total">{html.escape(host_size)}</span></h2>'
            f'  <table>'
            f'    <thead><tr><th>Path</th><th class="col-age">Age</th>'
            f'               <th>Size</th><th>Files</th><th>Commands</th>'
            f'               <th></th></tr></thead>'
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
        f'{banner}'
        f'{render_failures(load_failures())}'
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
        f'{_DELETE_MODAL if backups else ""}'
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

    cron_root_only = _codeblock(
        f"# crontab -e (as root, since that is the only account)\n"
        f"0 2 * * *  push-to-{FRAMBOISE_HOST} /path/to/directory youruser@{FRAMBOISE_HOST} >> /var/log/backer.log 2>&1",
        label="crontab on a root-only host — land as youruser instead of root",
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

    root_only_copyid = _codeblock(
        f"ssh-copy-id youruser@{FRAMBOISE_HOST}",
        label="authorize root's key for your account on the destination",
    )

    root_only_push = _codeblock(
        f"push-to-{FRAMBOISE_HOST} /path/to/directory youruser@{FRAMBOISE_HOST}",
        label="push, landing as youruser instead of root",
    )

    tailscale_find_ip = _codeblock(
        f"tailscale status | grep {FRAMBOISE_HOST}",
        label="find framboise's Tailscale IP",
    )

    tailscale_push = _codeblock(
        f"push-to-{FRAMBOISE_HOST} /path/to/directory youruser@100.x.x.x",
        label="push using the IP instead of the hostname",
    )

    tailscale_check_resolv = _codeblock(
        "cat /etc/resolv.conf",
        label="check whether this device is actually using Tailscale's resolver",
    )

    tailscale_accept_dns = _codeblock(
        "sudo tailscale set --accept-dns=true",
        label="tell tailscaled on this device to manage DNS",
    )

    tailscale_hosts_entry = _codeblock(
        f"echo '100.x.x.x {FRAMBOISE_HOST}' | sudo tee -a /etc/hosts",
        label="pin framboise's Tailscale IP in /etc/hosts (one-time, survives reboots)",
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
    <h2>Pushing from a server with no non-root user</h2>
    <p>Routers, appliances, and some minimal Linux installs only have a
    <code>root</code> account. Since the push script has no per-host default user,
    it simply connects as whoever invoked it &mdash; so running it as root logs
    into {fh} as root too, landing your files under <code>/root/...</code> there.</p>
    <p>To land in a normal account on {fh} instead, pass <code>youruser@{fh}</code>
    as the second argument. Authorize root's SSH key for that account first:</p>
    {root_only_copyid}
    {root_only_push}
    <p>The local source path (and the <code>.backer-info</code> metadata) still records
    <code>root</code> as the source user &mdash; that just describes who ran the push on
    the source machine, and is unrelated to which account it logs into on {fh}.</p>
  </section>

  <section>
    <h2>Connecting over Tailscale ({fh} not resolving)</h2>
    <p>If <code>ssh</code> or <code>ssh-copy-id</code> fails with
    <code>Could not resolve hostname {fh}</code>, the source machine has no way to
    look up the plain hostname. This is common when connecting between two
    Tailscale nodes that aren't on the same LAN &mdash; on a Mac, <code>{fh}</code>
    might "just work" via Bonjour/mDNS locally, but that doesn't extend over
    Tailscale.</p>
    <p><strong>Quick fix:</strong> use {fh}'s Tailscale IP instead of its hostname:</p>
    {tailscale_find_ip}
    {tailscale_push}
    <p><strong>If MagicDNS is already enabled tailnet-wide</strong> (check the
    <a href="https://login.tailscale.com/admin/dns">admin console</a>) but one
    specific device still can't resolve <code>{fh}</code>, the tailnet setting isn't
    the problem &mdash; that device just isn't using it. Check what it's actually
    resolving with:</p>
    {tailscale_check_resolv}
    <p>Look for a <code>nameserver 100.100.100.100</code> line and a
    <code>search &hellip;.ts.net</code> line. If either is missing, tell tailscaled on
    that device to manage DNS:</p>
    {tailscale_accept_dns}
    <p>If <code>/etc/resolv.conf</code> still doesn't show Tailscale's resolver after
    that, the device likely has no DNS manager Tailscale can hook into &mdash; common
    on minimal, root-only installs (routers, appliances, embedded Linux) that lack
    <code>systemd-resolved</code> or NetworkManager. On those, the IP address above is
    the reliable option, not a workaround to eventually replace.</p>
    <p><strong>Recommended for those boxes:</strong> pin the IP once instead of typing
    it on every command:</p>
    {tailscale_hosts_entry}
    <p>After that, plain <code>ssh {fh}</code> and <code>push-to-{fh}</code> work with no
    per-command IP needed. This won't auto-update if {fh}'s Tailscale IP ever changes
    (rare, but possible if the node is removed and re-added) &mdash; if
    <code>ssh {fh}</code> suddenly stops connecting, re-check the IP and update the
    line in <code>/etc/hosts</code>.</p>
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
    <p><strong>Root-only hosts</strong> (routers, appliances) &mdash; pass
    <code>youruser@{fh}</code> as the second argument so the scheduled push
    doesn't land as root on {fh}:</p>
    {cron_root_only}
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

    page_updated = datetime.fromtimestamp(os.path.getmtime(__file__)).strftime("%Y-%m-%d")
    footer_extra = (
        f" &bull; page content last updated {page_updated}"
        f" &bull; restart the service if this looks stale"
    )
    return _page(f"How to back up — {FRAMBOISE_HOST}", body, footer_extra)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

MAX_POST_BYTES = 8192
# Clocktower's failure payload carries two output fields capped at 8000 chars each
MAX_WEBHOOK_BYTES = 65536


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            page = render_dashboard(
                get_backups(),
                notice=query.get("deleted", [""])[0],
                error=query.get("error", [""])[0],
            )
            body = page.encode("utf-8")
        elif parsed.path == "/how-to":
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

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/webhook/failure":
            self._webhook_failure()
            return
        if route == "/failures/clear":
            count = clear_failures()
            print(f"dismissed {count} failure report(s)", flush=True)
            self._redirect_home(notice=f"Dismissed {count} failure report(s).")
            return
        if route != "/delete":
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > MAX_POST_BYTES:
            self._redirect_home(error="Request too large.")
            return

        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        path = parse_qs(raw).get("path", [""])[0]

        ok, message = delete_backup(path)
        if ok:
            # flush so journald records the audit line immediately — stdout is
            # block-buffered when systemd hands us a pipe instead of a tty
            print(f"deleted backup: {path}", flush=True)
            self._redirect_home(notice=message)
        else:
            self._redirect_home(error=message)

    def _webhook_failure(self) -> None:
        """Receive a scheduler's failure notification (Clocktower's payload shape)."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_WEBHOOK_BYTES:
            self._json(400, {"error": "missing or oversized body"})
            return

        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": "body is not valid JSON"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "expected a JSON object"})
            return

        entry = record_failure(payload)
        print(
            f"failure reported: job={entry['job_name']!r} "
            f"run={entry['run_id']} exit={entry['exit_code']}",
            flush=True,
        )
        self._json(200, {"ok": True, "recorded_at": entry["received_at"]})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect_home(self, notice: str = "", error: str = "") -> None:
        key, value = ("deleted", notice) if notice else ("error", error)
        self.send_response(303)
        self.send_header("Location", f"/?{key}={quote(value)}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress per-request log noise; errors still go to stderr


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), DashboardHandler)
    print(f"Backer dashboard → http://{HOST}:{PORT}  (root: {BACKUP_ROOT})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
