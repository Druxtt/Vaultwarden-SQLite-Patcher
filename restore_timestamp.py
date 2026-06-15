#!/usr/bin/env python3
"""
restore_timestamps.py
─────────────────────
Restores the creationDate / revisionDate fields in the Vaultwarden SQLite database
from a Bitwarden JSON export (source of truth).

Usage:
    python restore_timestamps.py \
        --bitwarden  bitwarden_export.json \
        --vaultwarden vaultwarden_export.json \
        --db          db.sqlite3 \
        [--dry-run]   \
        [--apply]

Steps:
    1. Automatic WAL merge (db.sqlite3-wal / -shm if they exist)
    2. Item matching by (folder_name, name, username)
    3. Report generation (matches, ambiguities, unmatched)
    4. SQL file generation
    5. Optional application to the DB (--apply)
"""

import argparse
import json
import sqlite3
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def parse_dt(s: str) -> str:
    """
    Normalizes any ISO 8601 timestamp to the format
    that Vaultwarden stores in SQLite: "YYYY-MM-DD HH:MM:SS.ffffff UTC"
    Accepts:   "2021-03-15T10:23:45.123Z"
               "2021-03-15T10:23:45Z"
               "2021-03-15T10:23:45.123456+00:00"
    Returns None if the string is empty / None.
    """
    if not s:
        return None
    s = s.strip()
    # Replaces the trailing Z with +00:00 for fromisoformat (Python < 3.11)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # Ensures it is UTC
    dt = dt.astimezone(timezone.utc)
    # Vaultwarden native SQLite format: 9-digit nanoseconds, without UTC suffix
    # Ex: "2023-03-05 12:38:36.496000000"
    ns = f"{dt.microsecond * 1000:09d}"
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + ns


def make_key(folder_name: str, name: str, username: str) -> tuple:
    """Case-insensitive matching key."""
    return (
        (folder_name or "").strip().lower(),
        (name or "").strip().lower(),
        (username or "").strip().lower(),
    )


def load_export(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────────────────────────────────────
# Export Parsing
# ──────────────────────────────────────────────────────────────────────────────

def parse_export(data: dict) -> tuple[dict, list]:
    """
    Returns:
      - folders : {folder_id -> folder_name}
      - items   : list of normalized dicts
    """
    folders = {f["id"]: f["name"] for f in data.get("folders", [])}
    # null folder_id → no folder
    folders[None] = ""

    items = []
    for item in data.get("items", []):
        login = item.get("login") or {}
        items.append({
            "id":           item.get("id"),
            "name":         item.get("name") or "",
            "username":     login.get("username") or "",
            "folder_id":    item.get("folderId"),
            "folder_name":  folders.get(item.get("folderId"), ""),
            "creationDate": item.get("creationDate"),
            "revisionDate": item.get("revisionDate"),
            "type":         item.get("type"),
        })
    return folders, items


# ──────────────────────────────────────────────────────────────────────────────
# WAL Merge
# ──────────────────────────────────────────────────────────────────────────────

def merge_wal(db_path: Path):
    """
    Opens the DB in WAL mode and forces a checkpoint to merge
    the -wal / -shm files into the main file.
    """
    wal = db_path.with_suffix(".sqlite3-wal")
    shm = db_path.with_suffix(".sqlite3-shm")
    if wal.exists() or shm.exists():
        print("[WAL] WAL files detected — merging...")
        con = sqlite3.connect(str(db_path))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        print("[WAL] Merge completed.")
    else:
        print("[WAL] No WAL files — DB is already consistent.")


# ──────────────────────────────────────────────────────────────────────────────
# Fetch Vaultwarden IDs from the DB
# ──────────────────────────────────────────────────────────────────────────────

def load_db_cipher_ids(db_path: Path) -> dict:
    """
    Returns {vaultwarden_uuid -> (name, created_at, updated_at)}
    from the ciphers table.
    """
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT uuid, name, created_at, updated_at FROM ciphers"
    ).fetchall()
    con.close()
    return {r["uuid"]: dict(r) for r in rows}


# ──────────────────────────────────────────────────────────────────────────────
# Matching
# ──────────────────────────────────────────────────────────────────────────────

def build_index(items: list) -> dict:
    """
    Builds an index {key -> [items]} to detect duplicates.
    """
    idx = defaultdict(list)
    for item in items:
        k = make_key(item["folder_name"], item["name"], item["username"])
        idx[k].append(item)
    return idx


def match_items(bw_items: list, vw_items: list) -> tuple[list, list, list]:
    """
    Returns:
      matches    : [(bw_item, vw_item), …]
      ambiguous  : [(key, [bw_items], [vw_items]), …]  — multiple candidates
      unmatched  : [vw_item, …]                        — no BW correspondence
    """
    bw_idx = build_index(bw_items)
    vw_idx = build_index(vw_items)

    matches   = []
    ambiguous = []
    matched_vw_ids = set()

    for key, bw_candidates in bw_idx.items():
        vw_candidates = vw_idx.get(key, [])

        if not vw_candidates:
            continue  # BW entry missing from VW (deleted?)

        if len(bw_candidates) == 1 and len(vw_candidates) == 1:
            matches.append((bw_candidates[0], vw_candidates[0]))
            matched_vw_ids.add(vw_candidates[0]["id"])
        else:
            ambiguous.append((key, bw_candidates, vw_candidates))
            for item in vw_candidates:
                matched_vw_ids.add(item["id"])  # marked as "seen"

    unmatched = [
        item for item in vw_items
        if item["id"] not in matched_vw_ids
    ]

    return matches, ambiguous, unmatched


# ──────────────────────────────────────────────────────────────────────────────
# SQL Generation
# ──────────────────────────────────────────────────────────────────────────────

def generate_sql(matches: list) -> list[str]:
    """
    Generates UPDATE statements for each clean match.
    Vaultwarden uses the 'uuid' column as PK in 'ciphers'.
    The timestamp columns are 'created_at' and 'updated_at'.
    """
    statements = []
    for bw, vw in matches:
        created  = parse_dt(bw["creationDate"])
        updated  = parse_dt(bw["revisionDate"])
        vw_id    = vw["id"]

        if not created and not updated:
            continue  # no timestamps in BW, skip

        parts = []
        if created:
            parts.append(f"created_at = '{created}'")
        if updated:
            parts.append(f"updated_at = '{updated}'")

        sql = (
            f"UPDATE ciphers SET {', '.join(parts)} "
            f"WHERE uuid = '{vw_id}';"
        )
        statements.append(sql)
    return statements


# ──────────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────────

def print_report(matches, ambiguous, unmatched):
    print("\n" + "═" * 60)
    print("  MATCHING REPORT")
    print("═" * 60)
    print(f"  ✅  Clean matches     : {len(matches)}")
    print(f"  ⚠️   Ambiguities       : {len(ambiguous)}")
    print(f"  ❌  Unmatched (VW)    : {len(unmatched)}")
    print("═" * 60)

    if ambiguous:
        print("\n── AMBIGUITIES (manual intervention required) ──")
        for key, bw_cands, vw_cands in ambiguous:
            folder, name, user = key
            print(f"\n  Folder='{folder}'  Name='{name}'  User='{user}'")
            print(f"  → {len(bw_cands)} Bitwarden entry(ies)  /  {len(vw_cands)} Vaultwarden entry(ies)")
            for i, b in enumerate(bw_cands):
                print(f"    BW[{i}] id={b['id']}  created={b['creationDate']}  modified={b['revisionDate']}")
            for i, v in enumerate(vw_cands):
                print(f"    VW[{i}] id={v['id']}")

    if unmatched:
        print("\n── UNMATCHED in Vaultwarden (missing from Bitwarden) ──")
        for item in unmatched:
            print(f"  VW id={item['id']}  folder='{item['folder_name']}'  name='{item['name']}'  user='{item['username']}'")

    print()


def write_report(matches, ambiguous, unmatched, out_path: Path):
    """Writes the full report to a text file."""
    lines = []
    lines.append("REPORT restore_timestamps.py")
    lines.append(f"Generated on {datetime.now().isoformat()}")
    lines.append("")
    lines.append(f"Clean matches    : {len(matches)}")
    lines.append(f"Ambiguities      : {len(ambiguous)}")
    lines.append(f"Unmatched (VW)   : {len(unmatched)}")
    lines.append("")

    if ambiguous:
        lines.append("=== AMBIGUITIES ===")
        for key, bw_cands, vw_cands in ambiguous:
            folder, name, user = key
            lines.append(f"Folder='{folder}'  Name='{name}'  User='{user}'")
            for i, b in enumerate(bw_cands):
                lines.append(f"  BW[{i}] id={b['id']}  created={b['creationDate']}  modified={b['revisionDate']}")
            for i, v in enumerate(vw_cands):
                lines.append(f"  VW[{i}] id={v['id']}")
            lines.append("")

    if unmatched:
        lines.append("=== UNMATCHED (VW) ===")
        for item in unmatched:
            lines.append(f"  id={item['id']}  folder='{item['folder_name']}'  name='{item['name']}'  user='{item['username']}'")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[REPORT] Written to: {out_path}")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Restores Bitwarden timestamps into the Vaultwarden SQLite DB."
    )
    parser.add_argument("--bitwarden",   required=True, help="Bitwarden JSON export")
    parser.add_argument("--vaultwarden", required=True, help="Vaultwarden JSON export")
    parser.add_argument("--db",          required=True, help="Vaultwarden db.sqlite3 file")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Generates SQL but does not apply it")
    parser.add_argument("--apply",       action="store_true",
                        help="Applies the SQL directly to the DB")
    args = parser.parse_args()

    bw_path  = Path(args.bitwarden)
    vw_path  = Path(args.vaultwarden)
    db_path  = Path(args.db)

    for p in (bw_path, vw_path, db_path):
        if not p.exists():
            print(f"[ERROR] File not found: {p}")
            sys.exit(1)

    # ── 1. Automatic DB Backup ──
    backup_path = db_path.with_name(db_path.stem + "_BACKUP_" +
                                    datetime.now().strftime("%Y%m%d_%H%M%S") + ".sqlite3")
    shutil.copy2(db_path, backup_path)
    print(f"[BACKUP] DB backed up → {backup_path}")

    # ── 2. WAL Merge ──
    merge_wal(db_path)

    # ── 3. Loading exports ──
    print("[LOAD] Loading JSON exports...")
    bw_data = load_export(bw_path)
    vw_data = load_export(vw_path)

    _, bw_items = parse_export(bw_data)
    _, vw_items = parse_export(vw_data)

    print(f"       Bitwarden   : {len(bw_items)} entries")
    print(f"       Vaultwarden : {len(vw_items)} entries")

    # ── 4. Matching ──
    print("[MATCH] Matching in progress...")
    matches, ambiguous, unmatched = match_items(bw_items, vw_items)

    # ── 5. Console report ──
    print_report(matches, ambiguous, unmatched)

    # ── 6. File report ──
    report_path = db_path.parent / "restore_timestamps_report.txt"
    write_report(matches, ambiguous, unmatched, report_path)

    # ── 7. SQL Generation ──
    statements = generate_sql(matches)
    sql_path   = db_path.parent / "restore_timestamps.sql"

    sql_content = "-- restore_timestamps.sql\n"
    sql_content += f"-- Generated on {datetime.now().isoformat()}\n"
    sql_content += f"-- {len(statements)} UPDATE(s)\n\n"
    sql_content += "BEGIN TRANSACTION;\n\n"
    sql_content += "\n".join(statements)
    sql_content += "\n\nCOMMIT;\n"

    sql_path.write_text(sql_content, encoding="utf-8")
    print(f"[SQL]    {len(statements)} UPDATE(s) written to: {sql_path}")

    if args.dry_run:
        print("\n[DRY-RUN] No modifications applied.")
        return

    # ── 8. Application to the DB ──
    if args.apply:
        print(f"\n[APPLY] Applying {len(statements)} UPDATE(s) to {db_path}...")
        con = sqlite3.connect(str(db_path))
        try:
            con.executescript(sql_content)
            print("[APPLY] ✅ Completed successfully.")
        except Exception as e:
            print(f"[APPLY] ❌ Error: {e}")
            print("       The backup DB is available at:", backup_path)
        finally:
            con.close()
    else:
        print("\n[INFO] SQL generated but not applied.")
        print("       Check restore_timestamps.sql then run again with --apply")
        print(f"       Or apply manually:")
        print(f"         sqlite3 {db_path} < {sql_path}")


if __name__ == "__main__":
    main()