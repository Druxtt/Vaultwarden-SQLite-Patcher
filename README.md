# Vaultwarden SQLite Patcher

A Python script to restore `creationDate` and `revisionDate` timestamps in your Vaultwarden database after migrating from Bitwarden.

## The problem

When migrating from Bitwarden to a self-hosted Vaultwarden instance using the standard export/import workflow, all entry timestamps (`Created` and `Last modified`) are reset to the import date. This is a [known limitation](https://github.com/dani-garcia/vaultwarden/discussions) — even though both exports share the exact same JSON fields (`creationDate`, `revisionDate`), the import process simply ignores them.

If you have years of password history and care about knowing when credentials were created or last changed, this is a problem.

## How it works

Since entry IDs and folder IDs differ between Bitwarden and Vaultwarden exports, a direct mapping isn't possible. The script matches entries across both exports using a composite key:

```
(folder_name, name, username)
```

For each clean match, it generates a SQL `UPDATE` targeting the Vaultwarden entry's UUID in the `ciphers` table. Ambiguous matches (duplicates sharing the same key) are flagged for manual review and excluded from the patch.

### Steps performed

1. **WAL merge** — if `db.sqlite3-wal` / `-shm` files are present, flushes them into the main database before touching anything
2. **Automatic backup** — saves a timestamped copy of the database before any changes
3. **Matching** — cross-references both JSON exports by composite key
4. **Report** — outputs a summary (clean matches, ambiguities, unmatched entries) to console and to a `.txt` file
5. **SQL generation** — writes all `UPDATE` statements to `restore_timestamps.sql`
6. **Apply** — executes the SQL in a single atomic transaction (only with `--apply`)

## Requirements

- Python 3.8+
- No external dependencies (standard library only)
- Your Vaultwarden `db.sqlite3` file (from a backup)
- A JSON export from **Bitwarden** (source of truth for timestamps)
- A JSON export from **Vaultwarden** (source of truth for current UUIDs)

## Usage

> ⚠️ Always stop Vaultwarden before patching the database.

**Step 1 — Dry run** (no changes made, just the report and SQL file)

```bash
python restore_timestamps.py \
  --bitwarden  bitwarden_export.json \
  --vaultwarden vaultwarden_export.json \
  --db          db.sqlite3 \
  --dry-run
```

**Step 2 — Review** the generated `restore_timestamps.sql` and `restore_timestamps_report.txt`

**Step 3 — Apply**

```bash
python restore_timestamps.py \
  --bitwarden  bitwarden_export.json \
  --vaultwarden vaultwarden_export.json \
  --db          db.sqlite3 \
  --apply
```

The script automatically backs up the database as `db_BACKUP_YYYYMMDD_HHMMSS.sqlite3` before applying anything.

## Output files

| File | Description |
|---|---|
| `db_BACKUP_*.sqlite3` | Automatic database backup |
| `restore_timestamps.sql` | Generated SQL UPDATE statements |
| `restore_timestamps_report.txt` | Full matching report |

## Reading the report

```
════════════════════════════════════════════════════════════
  MATCHING REPORT
════════════════════════════════════════════════════════════
  ✅  Clean matches     : 561
  ⚠️   Ambiguous         : 1
  ❌  Unmatched (VW)    : 8
════════════════════════════════════════════════════════════
```

- **Clean matches** — timestamps will be restored
- **Ambiguous** — multiple entries share the same key; excluded from the patch, listed in the report for manual review
- **Unmatched** — entries present in Vaultwarden but not in the Bitwarden export (e.g. credentials created after the migration); their timestamps are left untouched

## Redeploying the patched database

```bash
# Copy the patched database into the running container
docker stop vaultwarden
docker cp db.sqlite3 vaultwarden:/data/db.sqlite3

# Remove stale WAL files if present
docker run --rm --volumes-from vaultwarden alpine \
  rm -f /data/db.sqlite3-shm /data/db.sqlite3-wal

docker start vaultwarden
docker logs vaultwarden --tail 20
```

## A note on the timestamp format

This is the part that isn't documented anywhere. Vaultwarden stores timestamps in SQLite as:

```
2023-03-05 12:38:36.496000000
```

That's **9-digit nanosecond precision, no UTC suffix**. Writing `603000 UTC` (microseconds + suffix) or `603 UTC` (milliseconds + suffix) will cause Vaultwarden to panic on startup with a deserialization error:

```
Error loading ciphers: DeserializationError(..., "Invalid datetime 2024-01-16 18:19:13.603 UTC")
```

The script handles this correctly.

## Limitations

- Matching relies on `(folder_name, name, username)` — entries with identical values on all three fields are flagged as ambiguous and skipped
- Only `login` type entries expose a `username` field; other types (secure notes, cards, identities) match on `(folder_name, name)` alone, which increases ambiguity risk if you have duplicates
- Timestamps from Bitwarden have millisecond precision; the 6 trailing zeros in the nanosecond field are expected

## Contributing

Pull requests welcome, especially for:
- Support for other import formats (Firefox CSV, KeePass, 1Password...)
- Improved matching strategies for non-login entry types
- A `--interactive` mode to manually resolve ambiguous matches