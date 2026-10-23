# Shared by the Kairo git hooks (sourced by them, never run on its own). LF endings only: sh reads it, also on Windows.
# Sets $py (a Python 3) and $scan (privacy_scan.py). Both hooks fail closed: no Python or no scanner means no commit.

kairo_find_python() {
  py=
  for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      py=$cand
      return 0
    fi
  done
  echo "$1: Python 3 was not found, so the privacy scan cannot run. Install Python 3, or commit with --no-verify." >&2
  return 1
}

# The scanner of this checkout; on a branch that predates it, the copy that `privacy_scan.py --install-hooks` put next
# to the installed hooks.
kairo_find_scanner() {
  scan=
  if [ -f "$2/scripts/privacy_scan.py" ]; then
    scan="$2/scripts/privacy_scan.py"
  elif [ -f "$3/privacy_scan.py" ]; then
    scan="$3/privacy_scan.py"
  else
    echo "$1: scripts/privacy_scan.py is missing in this checkout (an old branch?), so nothing can be scanned." >&2
    echo "$1: rebase onto main, run 'python scripts/privacy_scan.py --install-hooks' from a current checkout, or commit with --no-verify." >&2
    return 1
  fi
  return 0
}
