# Development Tools

A collection of desktop utilities built with Python to streamline development workflows.

## Projects

### 1. **Postman Merger** (`postman_merger/`)

Merges two Postman collection JSON files while intelligently preserving your saved responses and tests.

**Use case:** Keep your local Postman collection in sync with updated routes from your codebase without losing custom configurations.

**Quick start:**
```bash
cd postman_merger
python app.py  # Run GUI
```

Or build the executable:
```bash
cd postman_merger
./build.bat  # Windows
# Output: dist/PostmanMerger.exe
```

[Learn more →](postman_merger/README.md)

---

### 2. **Prisma Manager** (`prisma_manager/`)

Desktop application for scanning, monitoring, and managing Prisma schemas across your project directories.

**Features:**
- Detect all Prisma schemas in a directory tree
- Check database connection status from `.env` files
- Start/stop Prisma Studio instances with a single click
- Monitor uptime and access URLs
- Save preferences for scan patterns and auto-open behavior

**Quick start:**
```bash
cd prisma_manager
python app.py  # Run GUI
```

Or build the executable:
```bash
cd prisma_manager
./build.bat  # Windows
# Output: dist/PrismaManager.exe
```

[Learn more →](prisma_manager/README.md)

---

## Building Both Tools

To build both executables at once:

**Windows:**
```bash
./build.bat
```

**macOS/Linux:**
```bash
./build.sh
```

Both scripts will create `.exe` files in their respective `dist/` directories.

---

## Requirements

- Python 3.8+
- PyInstaller (for building executables)

**Install dependencies:**
```bash
pip install pyinstaller
```
---

## Development

Each tool is independently developed and can be modified in its own directory. To contribute:

1. Navigate to the project folder
2. Make your changes to `app.py` or related files
3. Test by running `python app.py`
4. Rebuild the executable with `pyinstaller`

---

## License & Notes

Each tool is designed for development workflows and tested on Windows. Cross-platform support may vary.
