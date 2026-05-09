# Postman Collection Merger

Merges two Postman collection JSON files — preserving your saved responses
while syncing new/removed routes from an updated collection.

## What it does

| Scenario | Behavior |
|---|---|
| Route exists in both files | Kept from **target** (preserves saved responses, tests, etc.) |
| Route is NEW in "new" collection | **Added** to merged output |
| Route was DELETED from "new" collection | **Removed** from merged output |

## How to use

1. Open `PostmanMerger.exe`
2. **TARGET** = your existing Postman file (the one with saved responses you want to keep)
3. **NEW** = the updated collection from your codebase (new routes added/removed)
4. Optionally check **"Replace target file in-place"** — it will auto-backup before overwriting
5. Click **Merge Collections**
6. If not replacing in-place, click **Save As** to export the merged file

## Build from source (Windows)

Requirements: Python 3.8+

```
pip install pyinstaller
pyinstaller --onefile --windowed --name PostmanMerger postman_merger.py
```

Or just double-click `build.bat`.

The EXE will be at `dist/PostmanMerger.exe`.

## Run without building (any OS)

```
python postman_merger.py
```

## Notes

- Works with Postman Collection **v2 and v2.1** format
- Route identity is based on `METHOD + URL path` (host/baseURL differences are ignored)
- Folder structure from "new" collection is preserved, including nested folders, when adding new routes
- Backups are timestamped: `your_file.backup_20250405_143022.json`
