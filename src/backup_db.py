"""
backup_db.py — daily snapshot of the SQLite store.

Uses SQLite's online backup API, so it is safe while the 30s flash loop is writing.
Writes fin_news-YYYY-MM-DD.db to BACKUP_DIR (default ~/backups/fin-news-assistant)
and keeps the newest --keep files.

Usage:
    python src/backup_db.py
    python src/backup_db.py --keep 14 --dir /path/to/backups
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import db_path  # noqa: E402


def main():
    p = argparse.ArgumentParser(description="Snapshot the SQLite DB and prune old snapshots")
    p.add_argument("--keep", type=int, default=14, help="snapshots to keep (default 14)")
    p.add_argument("--dir", default=os.getenv("BACKUP_DIR", "~/backups/fin-news-assistant"))
    args = p.parse_args()

    src = Path(db_path())
    if not src.exists():
        print(f"[backup] DB not found at {src} — nothing to back up.", file=sys.stderr)
        sys.exit(1)

    dest_dir = Path(args.dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"fin_news-{datetime.now().strftime('%Y-%m-%d')}.db"
    tmp = dest.with_suffix(".db.tmp")

    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        d.close()
        s.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        print(f"[backup] integrity check failed: {ok}", file=sys.stderr)
        sys.exit(1)
    tmp.replace(dest)  # same-day rerun overwrites that day's snapshot

    snaps = sorted(dest_dir.glob("fin_news-*.db"))
    for old in snaps[: max(0, len(snaps) - args.keep)]:
        old.unlink()
    kept = min(len(snaps), args.keep)
    print(f"[backup] wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB), keeping {kept} snapshot(s)")


if __name__ == "__main__":
    main()
