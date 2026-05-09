#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "================================================"
echo "  Interactive Python Project EXE Builder (Bash)"
echo "================================================"
echo

if command -v python >/dev/null 2>&1; then
  PYTHON="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  echo "[ERROR] Python not found. Install Python first."
  exit 1
fi

echo "[1/6] Checking PyInstaller..."
if ! "$PYTHON" -m pip show pyinstaller >/dev/null 2>&1; then
  echo "PyInstaller not found. Installing..."
  "$PYTHON" -m pip install pyinstaller
fi

echo
echo "[2/6] Scanning buildable projects..."

declare -a project_names=()
declare -a project_dirs=()
declare -a project_mains=()

shopt -s nullglob
for d in */; do
  dir="${d%/}"
  main_script=""

  if [[ -f "$dir/app.py" ]]; then
    main_script="$ROOT_DIR/$dir/app.py"
  elif [[ -f "$dir/$dir.py" ]]; then
    main_script="$ROOT_DIR/$dir/$dir.py"
  else
    for f in "$dir"/*.py; do
      base_name="$(basename "$f")"
      if [[ "$base_name" == "__init__.py" ]]; then
        continue
      fi
      main_script="$ROOT_DIR/$f"
      break
    done
  fi

  if [[ -n "$main_script" ]]; then
    project_names+=("$dir")
    project_dirs+=("$ROOT_DIR/$dir")
    project_mains+=("$main_script")
  fi
done

count="${#project_names[@]}"
if [[ "$count" -eq 0 ]]; then
  echo "[ERROR] No buildable Python projects found under:"
  echo "        $ROOT_DIR"
  exit 1
fi

echo
echo "Buildable projects:"
for i in "${!project_names[@]}"; do
  idx=$((i + 1))
  echo "  $idx) ${project_names[$i]}  [main: ${project_mains[$i]}]"
done

echo
read -r -p "Choose project number (1-$count): " choice

if [[ ! "$choice" =~ ^[0-9]+$ ]]; then
  echo "[ERROR] Invalid selection: $choice"
  exit 1
fi
if (( choice < 1 || choice > count )); then
  echo "[ERROR] Selection out of range."
  exit 1
fi

selected_index=$((choice - 1))
selected_name="${project_names[$selected_index]}"
selected_dir="${project_dirs[$selected_index]}"
selected_main="${project_mains[$selected_index]}"

echo
echo "Selected project: $selected_name"
echo "Main script:      $selected_main"

read -r -p "EXE name (default: $selected_name): " exe_name
if [[ -z "$exe_name" ]]; then
  exe_name="$selected_name"
fi

echo
echo "Build mode:"
echo "  1) GUI app (no cmd/terminal window)"
echo "  2) Console app (terminal window visible)"
read -r -p "Choose build mode [1-2] (default: 1): " mode_choice
mode_choice="${mode_choice:-1}"

pyi_mode=()
case "$mode_choice" in
  1)
    pyi_mode=(--windowed)
    ;;
  2)
    pyi_mode=()
    ;;
  *)
    echo "[ERROR] Invalid build mode: $mode_choice"
    exit 1
    ;;
esac

dist_dir="$selected_dir/dist"
work_dir="$selected_dir/build"
spec_dir="$selected_dir"
final_exe="$dist_dir/$exe_name.exe"

echo
echo "[3/6] Creating dist folder..."
mkdir -p "$dist_dir"

echo "[4/6] Building EXE..."
"$PYTHON" -m PyInstaller --noconfirm --clean --onefile "${pyi_mode[@]}" --name "$exe_name" --distpath "$dist_dir" --workpath "$work_dir" --specpath "$spec_dir" "$selected_main"

echo
echo "[5/6] Build completed."
if [[ -f "$final_exe" ]]; then
  echo "[6/6] Done!"
  echo "SUCCESS: EXE created at:"
  echo "$final_exe"
else
  echo "[6/6] Done with warnings."
  echo "[WARN] Build finished but EXE was not found at expected path:"
  echo "$final_exe"
fi
