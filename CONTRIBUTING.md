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
  installed (with `--verbose`, so you see file, line and rule; its output is already redacted).
- `commit-msg` checks the commit message (a term in a message stays in the history for good). Every line of it is
  checked, also lines that start with `#` and text below a scissors line (`# ---- >8 ----`): `git commit -m`, `-F`
  and `git merge -m` keep them. Only when an editor was opened are the lines git itself wrote into the file left out
  (its comment block, the diff of `commit -v`), and only while they still end the file unchanged: `prepare-commit-msg`
  saves the file as git hands it to the editor, so a typed line is always checked, whatever it starts with. One gap:
  `--cleanup=whitespace` or `--cleanup=verbatim` typed on the command line makes git keep its comment block (it lists
  file names and the branch), and a hook cannot see that option. Set `commit.cleanup` in the git config instead (the
  hook reads it); CI's history scan checks every message in any case.
- They need Python 3 on the PATH. Git for Windows runs them with its own `sh`; no `.cmd` or `.ps1` is needed.
- `git commit --no-verify` skips them, but CI scans the whole history again, so it only delays the red build.

**Old branches and other worktrees.** `core.hooksPath scripts/hooks` is a path inside the checkout. On an old branch
(one that was cut before `scripts/hooks` existed) git finds no hook file and commits without any scan, and the same is
true for branches of other tools (`claude/*`, `.claude/worktrees`). To protect every branch and every worktree of the
clone, install a copy of the hooks and of the scanner into the shared git folder instead (or as well):

```bash
python scripts/privacy_scan.py --install-hooks
```

This sets `core.hooksPath` to an absolute folder inside `.git`. The hooks use the scanner of the checkout when it has
one and the installed copy otherwise; if neither exists they refuse the commit. Run it again after the scanner changes.

## The personal-terms list

One term per line, kept OUTSIDE the repo. The scanner uses the first of these that exists:

1. `--terms-file FILE`
2. the file named by the environment variable `KAIRO_PERSONAL_TERMS_FILE`
3. `<data folder>/config/personal-terms.txt` (the data folder is `KAIRO_DATA_DIR`; on Windows `C:\Kairo\data`;
   Kairo does not copy this file into its effective config)
4. `~/.config/kairo/personal-terms.txt`
5. the environment variable `KAIRO_PERSONAL_TERMS` (the same list as text; CI fills it from the repository secret
   `PERSONAL_TERMS`)

A list named on purpose (1 or 2) that does not exist is an error, not a silent skip.

**Format.** Save the list as UTF-8. (Windows PowerShell 5.1 saves `Set-Content` as Windows-1252 and `>` / `Out-File` as
UTF-16; both are read, with a warning to save as UTF-8. Notepad: Save As, Encoding UTF-8.) Lines starting with `#` and
empty lines are ignored. **One term per line.** A line with a comma or a semicolon is an error, because a list written on
one line would otherwise be read as one term that matches nothing. (A secret set with a literal `\n` between the terms
is read as separate terms, with a warning.)

**How a term matches.** Without regard to case; an umlaut also matches its two-letter form and the plain vowel (`ü` =
`ue` = `u`, `ß` = `ss`); never inside a longer word; with or without punctuation or spaces between the words (`first
last`, `first.last`, `first_last`, `FirstLast`, `firstlast@...`), and a two or three word name also as `last, first`.
A number matches with any grouping (`079 555 12 34` = `0795551234`), and a number that starts with `0` also with a
country code (`+41 79 ...`, `0041 79 ...`). Text is also read after decoding: `\u00fc`, `\xFC`, `%C3%BC`, `&uuml;`,
`=C3=BC`, RFC 2047 headers and base64 (two layers; also right after `=` and wrapped over lines, as in a mail).
Invisible characters (every format character, combining mark and control character: zero-width, tag and variation
characters too), accents and look-alike letters are ignored. A line `regex:<pattern>` is a regular expression instead
(it sees the text as written and with umlauts folded).

A matched term is never printed, not even in CI logs: the report shows `file:line` and the rule number
(`personal term #4 (list line 7)`) so you can find the term in your own list. A file name, a ref name or a git error
message that holds a term is shown as `<redacted name #3>` or hidden. Without a list the check is skipped
with a warning (exit 0), but in CI on `main` it is an error (exit 2), so a missing secret is noticed.

## Running it by hand

```bash
python scripts/privacy_scan.py --staged                       # what the next commit adds
python scripts/privacy_scan.py --tree .                       # the files as they are (what git would commit)
python scripts/privacy_scan.py --history                      # every commit on every ref: files, commit objects, messages, identities
python scripts/privacy_scan.py --history origin/main..HEAD    # only the commits a branch adds
python scripts/privacy_scan.py --zip dist/Kairo-Setup-*.zip   # a release zip (scripts/build-release.sh does this itself)
python scripts/privacy_scan.py --gitleaks-report r.json       # a gitleaks JSON report without the matched text
```

Exit codes: 0 clean, 1 findings, 2 the check could not run (bad list, no list on `main` in CI, shallow clone in CI, or,
with `--ci`, `--zip` or `--strict`, something that could not be scanned). `--secrets-only` leaves the terms check out on
purpose (for a step that has no list; another job checks the terms).

## What it reads, and what it cannot

It reads text files of any size (in pieces), UTF-8, Windows-1252 and UTF-16/32 text (with or without a BOM), the
strings of binaries (text chunks of PNG, tags of WAV and MP3, EXIF, SQLite files), PDFs (metadata and the text of
their compressed streams), office files, zip, gzip, bzip2, xz and tar files inside each other (a `.tar.gz`, an
installer stub in front of a zip ...), zip comments, commit objects (all headers, not only author and message),
annotated tags and the names of files, refs and zip members (also symlinks and files that only a merge commit
introduced).

What it **cannot** do: 7z, rar and zstd archives, encrypted or damaged members and anything nested deeper than 8
archives are reported as "not scanned" (an error in CI and for a zip; a warning on your machine). PDFs whose text is in
fonts without a text map, scanned pages and photos cannot be searched at all, so documents and photos
(`.pdf`, `.jpg`, `.heic`, `.docx` ...) are forbidden outside `web/public/`. A name that is cut apart (`"Example" + "town"`),
hyphenated over a line break, abbreviated (`Muellerstr.`), compounded (`Exampletowner`), reduced to initials, or
transliterated or reversed letter by letter is not found. Keep such text out of the repo.

## Secrets

Besides the terms, the scanner flags private-key blocks (also when only the body of a template was filled in), Google,
GitHub, Tailscale, OpenAI, AWS, Slack and Stripe keys, JSON web tokens, VAPID and JWK private keys, and
password / secret / token / `*_KEY` assignments with long random values. Placeholders and the fake values in
`server/tests` are allowed. In `server/tests/`, `docs/` and `installer/ci/` a deliberate fake can carry
`privacy-scan: allow` on its line (this never silences a personal term, and it is ignored everywhere else). Build fake
keys in tests from pieces (`"gh" + "p_" + ...`) so the file itself stays clean.

Paths that must never be committed or shipped: `private/`, `secrets/`, `vault/`, `data/`, `devdata/`, `.private/`,
`*.local.*`, `.env`, `*.env` and `.envrc` (not `.env.example`), databases (`*.db`, `*.sqlite*`, and any file with the
SQLite header), the terms list, key stores, Google client secrets, Kairo's own secret stores (by their header), the
capture token, `.gitleaksignore` and documents or photos outside `web/public/`.

In tests use made-up names and places only.

## CI

- Job `privacy`: pull requests must add clean commits (`origin/<base>..HEAD`, hard) and report the whole old history
  (not blocking while the old history still holds the `private/` file); a push to `main` scans the whole history, hard.
  It runs `privacy_scan.py --ci` with the terms from the repository secret `PERSONAL_TERMS` and a pinned,
  checksum-verified [gitleaks](https://github.com/gitleaks/gitleaks) (`.gitleaks.toml`, needs gitleaks 8.25 or newer,
  looks into archives 3 deep). gitleaks never runs with `--verbose` there (that prints the matched line, the file name
  and the author, which are not masked in the log); only rule, file, line and commit are printed.
- A pull request is judged by the scanner and the gitleaks config of its **base branch**, not by its own copy, so
  changing them does not help it pass. A `.gitleaksignore` file (any folder) and gitleaks' inline allow comment
  (outside `server/tests/`, `docs/` and `installer/ci/`) are findings, and CI deletes the ignore file before gitleaks
  runs, because gitleaks honours both whatever config it is given.
- **What only review can stop.** The workflow file of a pull request comes from the pull request itself: a pull
  request that edits `.github/` (or adds a workflow) can change or skip these steps, and could read the
  `PERSONAL_TERMS` secret. No scanner can prevent that. So a pull request that touches `.github/`,
  `scripts/privacy_scan.py`, `scripts/hooks/`, `.gitleaks.toml` or `scripts/build-release.sh` is reviewed and merged by
  the owner by hand, never merged automatically on green CI. (A code-owner rule would not help here: pull requests are
  opened with the owner's own account, and nobody can approve their own pull request.)
- Job `privacy-zip` (needs `build`): the exact zip the build made, scanned with the terms (`--ci`) and unpacked for
  gitleaks. The `release` job needs it, so a release is never published without the terms check; without the secret
  it is red on `main` and nothing is released. A pull request from this repository is red too when the secret is
  missing or empty, so that shows before the merge and not as releases that stop. The terms secret is given to no
  other job (the build job runs npm scripts).
- The `release` job does not wait for the job `privacy` (the whole history): that job stays red until the old history
  is rewritten (task 97). What ships is the zip, and `privacy-zip` checks exactly that file. Add `privacy` to the
  release's `needs` once it is green on `main`.
- Job `build`: the setup zip is scanned (secrets and paths) before it is uploaded; `scripts/build-release.sh` deletes a
  zip that fails (it also checks the terms if this machine has a list).
- Because a pull request is judged by the old scanner, a pull request that changes the scanner and needs a new
  exception (for example another folder in `DOCUMENT_ALLOWED_DIRS`, or a new fake value) cannot pass its own check: the
  owner reviews and merges it by hand.
- To move gitleaks to a newer release, change `GITLEAKS_VERSION` and `GITLEAKS_SHA256` together in both jobs (the
  checksum is in the release's `gitleaks_<version>_checksums.txt`).
- A branch or tag named after a person or place shows up in the log of `actions/checkout`, which the scanner cannot
  hide. Do not name branches after personal data.

**The GitHub secret** (a step for the owner): repository Settings, Secrets and variables, Actions, New repository secret,
name `PERSONAL_TERMS`, value = the same list as the file, one term per line (paste it with real line breaks).
