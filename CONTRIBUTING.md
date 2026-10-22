# Contributing

Kairo's rules for Claude Code are in [CLAUDE.md](CLAUDE.md); the spec is [docs/KICKOFF.md](docs/KICKOFF.md).
This page is about one thing: keeping personal data and secrets out of the repo and out of every release.

## Why

The code may become public later, and everything personal (names, places, numbers, the owner's profile) lives only
on the PC. So a commit, the history and a release must never hold any of it, and the checks below make that
mechanical instead of a matter of care.

## Turn on the git hooks (once per clone)

```bash
git config core.hooksPath scripts/hooks
```

- `pre-commit` runs `python scripts/privacy_scan.py --staged` on what the commit adds, and `gitleaks` too if it is
  installed.
- `commit-msg` checks the commit message (a term in a message stays in the history for good).
- They need Python 3 on the PATH. Git for Windows runs them with its own `sh`; no `.cmd` or `.ps1` is needed.
- `git commit --no-verify` skips them, but CI scans the whole history again, so it only delays the red build.

## The personal-terms list

One term per line, kept OUTSIDE the repo. The scanner uses the first of these that exists:

1. `--terms-file FILE`
2. the file named by the environment variable `KAIRO_PERSONAL_TERMS_FILE`
3. `<data folder>/config/personal-terms.txt` (the data folder is `KAIRO_DATA_DIR`; on Windows `C:\Kairo\data`)
4. `~/.config/kairo/personal-terms.txt`
5. the environment variable `KAIRO_PERSONAL_TERMS` (the same list as text; CI fills it from the repository secret
   `PERSONAL_TERMS`)

Rules: lines starting with `#` and empty lines are ignored. A term is matched without regard to case, with umlauts
folded (`ue` = u-umlaut, `ss` = sharp s), never inside a longer word, with any punctuation between its words
(`first last`, `first.last`, `first_last`). A line `regex:<pattern>` is a regular expression instead.

A matched term is never printed, not even in CI logs: the report shows `file:line` and the rule number
(`personal term #4 (list line 7)`) so you can find the term in your own list. Without a list the check is skipped
with a warning (exit 0), but in CI on `main` it is an error (exit 2), so a missing secret is noticed.

## Running it by hand

```bash
python scripts/privacy_scan.py --staged                       # what the next commit adds
python scripts/privacy_scan.py --tree .                       # the files as they are (what git would commit)
python scripts/privacy_scan.py --history                      # every commit on every ref: files, messages, identities
python scripts/privacy_scan.py --history main --ci            # one ref, CI behaviour
python scripts/privacy_scan.py --zip dist/Kairo-Setup-*.zip   # a release zip (scripts/build-release.sh does this itself)
```

Exit codes: 0 clean, 1 findings, 2 the check could not run (bad list, no list on `main` in CI, shallow clone in CI).

## What it cannot see

Text files, source, config, SQLite files, office files (`.docx`, `.xlsx` are zips of XML) and zips inside zips are
searched. Images, fonts, audio, PDFs and other compressed or packed binaries cannot be searched; only their names
are checked. Keep such files out of the repo if they could hold anything personal.

## Secrets

Besides the terms, the scanner flags private-key blocks, Google/GitHub/Tailscale/OpenAI/AWS keys, JSON web tokens and
password/secret/token assignments with long random values. Placeholders and the fake values in `server/tests` are
allowed. A deliberate fake that has to stay in the code can carry `privacy-scan: allow` on its line (this never
silences a personal term). Build fake keys in tests from pieces (`"gh" + "p_" + ...`) so the file itself stays clean.

In tests use made-up names and places only.

## CI

- Job `privacy`: the whole history (`fetch-depth: 0`) with `privacy_scan.py --ci --history` and a pinned, checksum-verified
  [gitleaks](https://github.com/gitleaks/gitleaks) (`.gitleaks.toml`, needs gitleaks 8.25 or newer).
- Job `build`: the setup zip is scanned before it is uploaded; `scripts/build-release.sh` deletes a zip that fails.
- To move gitleaks to a newer release, change `GITLEAKS_VERSION` and `GITLEAKS_SHA256` together (the checksum is in the
  release's `gitleaks_<version>_checksums.txt`).
