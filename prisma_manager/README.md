# Prisma Checker

Prisma Checker is now a desktop app built with Python `tkinter`. It scans a chosen root folder for Prisma schemas, checks `.env` datasource values, and lets you start or stop Prisma Studio per schema.

## Run

```powershell
python app.py
```

## Features

- Lists every detected schema in a desktop table.
- Shows whether datasource env vars have values.
- Shows whether Prisma Studio is on or off.
- Shows live uptime and detected URL.
- Includes `Start`, `Stop`, and `Open In Browser` actions.
- Saves scan patterns, run command, auto-open behavior, and scan root.

## Default Command

```text
npx prisma@6 studio --schema "{{PathOfSchema}}" --browser none
```

Available placeholders:

- `{{PathOfSchema}}`
- `{{ProjectDir}}`

## Build `.exe`

Install PyInstaller:

```powershell
pip install pyinstaller
```

Build:

```powershell
pyinstaller --noconsole --onefile --name PrismaChecker app.py
```

The executable will be created in `dist\PrismaChecker.exe`.

## Settings File

Settings are stored in `prisma-checker.settings.json`.
