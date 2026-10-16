#!/usr/bin/env bash
# Build the Windows setup zip for kairo-home (KICKOFF 2b, 6).
#
#   scripts/build-release.sh [--allow-dirty]
#
# Output: dist/Kairo-Setup-<version>.zip and .zip.sha256
# Layout inside: Kairo-Setup-<version>/{Install Kairo.bat, installer/, app/}
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)

allow_dirty=0
[[ "${1:-}" == "--allow-dirty" ]] && allow_dirty=1
if [[ $allow_dirty -eq 0 && -n "$(git status --porcelain)" ]]; then
  echo "Uncommitted changes. Commit first (or --allow-dirty for a local test build)." >&2
  exit 1
fi

sha=$(git rev-parse --short=7 HEAD)
version="$(date -u +%Y.%m.%d.%H%M)-$sha"
if [[ $allow_dirty -eq 1 && -n "$(git status --porcelain)" ]]; then version="$version-dirty"; fi

# owner/repo for "Update Kairo": from CI, else from the git remote.
repo=${GITHUB_REPOSITORY:-$(git remote get-url origin | sed -E 's#(\.git)?$##; s#^.*github\.com[:/]##; s#^.*/git/##')}

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || PY=python

echo "==> Web build"
(cd web && npm ci --silent --no-audit --no-fund && npm run -s build >/dev/null)

out="$REPO/dist"
name="Kairo-Setup-$version"
stage=$(mktemp -d)
root="$stage/$name"
mkdir -p "$root/app/server" "$root/app/web" "$root/installer"

echo "==> Staging $name"
cp -r server/app server/mcp_kairo server/hooks server/migrations server/tools server/alembic.ini server/requirements.txt server/requirements-voice.txt server/requirements-browser.txt server/requirements-memory.txt "$root/app/server/"
cp -r web/dist "$root/app/web/dist"
cp -r config "$root/app/config"
cp installer/*.ps1 installer/*.psm1 installer/versions.json "$root/installer/"
cp -r installer/bat "$root/installer/bat"
cp "installer/Install Kairo.bat" "$root/"
find "$root" -name '__pycache__' -type d -prune -exec rm -rf {} +

"$PY" - "$root/app/release.json" "$version" "$sha" "$repo" <<'PY'
import datetime, json, sys
path, version, sha, repo = sys.argv[1:5]
versions = json.load(open("installer/versions.json"))
json.dump({
    "version": version,
    "git_sha": sha,
    "update_repo": repo,
    "claude_code_version": versions["claude_code"],
    "python": versions["python"],
    "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
}, open(path, "w"), indent=2)
PY

mkdir -p "$out"
zip="$out/$name.zip"
"$PY" - "$stage" "$name" "$zip" <<'PY'
import os, sys, zipfile
stage, name, dest = sys.argv[1:4]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for base, _, files in os.walk(os.path.join(stage, name)):
        for f in sorted(files):
            full = os.path.join(base, f)
            z.write(full, os.path.relpath(full, stage).replace(os.sep, "/"))
PY
"$PY" - "$zip" <<'PY'
import hashlib, os, sys
p = sys.argv[1]
h = hashlib.sha256(open(p, "rb").read()).hexdigest()
open(p + ".sha256", "w").write(f"{h}  {os.path.basename(p)}\n")
PY
rm -rf "$stage"
# Nothing the app needs at run time may be missing (2026-10-14: no release ever had mcp_kairo/hooks).
"$PY" scripts/check_release.py "$zip"
echo "$version" > "$out/VERSION"
echo "==> $zip"
