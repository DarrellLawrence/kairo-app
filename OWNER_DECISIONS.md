# Owner decisions

Things only the owner can decide or do: money, accounts, hardware, irreversible actions, other
people's data. Each item says what, why, and the exact steps. Newest first.

## 1. GitHub Actions is blocked by billing (blocks everything else)

- **What:** every CI job fails in about 3 s without a runner ("recent account payments have failed
  or your spending limit needs to be increased").
- **Why it matters:** no CI means no merge (the rule is green CI only) and no release. Every branch
  from tonight is waiting on it.
- **Measured cost:** from the last green run (2026-10-18, 13 jobs). Each job is rounded up to the
  minute. Linux jobs: 16 min. Windows jobs: 68 min, and Windows counts double against the free
  quota, so 136. That's **152 quota minutes per CI run**. The Windows full test suite alone is
  37 min, the Windows install test 17 min.
- **What that means:** the free plan's 2,000 min/month cover about **13 CI runs**. Every PR push
  is one run and every merge to main another, so one busy night uses that up. Past the quota a run
  costs about **$1.2** (GitHub's list price: Linux $0.008/min, Windows $0.016/min; the 2026 price
  cut may lower this by up to 39%). At about 40 runs a month that's **≈ $30/month**.
- **Steps:** github.com → your avatar → Settings → Billing and plans → fix the payment method, then
  set an Actions spending limit (suggestion: $20/month).
- **Optional, cuts the cost in half (your call, changes a rule):** run the 37-min Windows full suite
  only on main and nightly, not on every PR push. PRs would still run the Windows voice and install
  tests. Saves about 74 quota minutes per PR run. The trade-off: a Windows-only bug can reach main
  before it is caught (the release still waits for it).
- **Alternative:** a public repo gets free minutes. Only after item 3, and see the verdict there.

## 2. Old tags and releases still hold the old history (irreversible: deletes releases)

- **What:** the history rewrite replaced every branch. The 47 release tags still point at the old
  commits, and the "Source code" download of every release contains the old docs. 35 of the 47
  setup zips also contain the old profile. GitHub refused the tag force-push from the cloud
  session (HTTP 403, proxy policy), so this has to be done from your laptop.
- **Plan:** keep only the newest release (v2026.10.18.1219-e9f997a: its zip scans clean). Move
  its tag to the rewritten commit. Delete the other 46 releases and their tags, plus the old CI
  artifacts. The PC keeps working: the updater only reads "latest", and rollback uses the release
  folders on the PC.
- **Steps (WSL on the laptop; one time `sudo apt install gh && gh auth login`):**

```bash
R=DarrellLawrence/Kairo
KEEP=v2026.10.18.1219-e9f997a
# 1. Point the kept tag at the rewritten commit (same content, clean history)
gh api -X PATCH repos/$R/git/refs/tags/$KEEP -f sha=81a396f0adecb1bbe44ea5e5ec6271e04f80efca -F force=true
# 2. Delete every other release and its tag
gh release list -R $R --limit 200 --json tagName -q '.[].tagName' | grep -vx "$KEEP" |
  while read t; do gh release delete "$t" -R $R --cleanup-tag -y; done
# 3. Delete any tag left without a release
gh api repos/$R/git/matching-refs/tags --paginate -q '.[].ref' | grep -vx "refs/tags/$KEEP" |
  while read r; do gh api -X DELETE "repos/$R/git/$r"; done
# 4. Delete old CI artifacts (old setup zips)
gh api repos/$R/actions/artifacts --paginate -q '.artifacts[].id' |
  while read id; do gh api -X DELETE repos/$R/actions/artifacts/$id; done
# 5. Check: should print exactly one tag
gh api repos/$R/git/matching-refs/tags -q '.[].ref'
```

## 3. Before the repo can ever go public

- **What:** GitHub keeps the old commits reachable through closed pull requests
  (refs/pull/1…53). A force-push cannot remove them; only GitHub Support can. The old PR
  descriptions may also quote personal details.
- **Choose one:**
  - **(a)** Ask GitHub Support to purge cached views and refs/pull for the repo: support.github.com →
    "Remove sensitive data", name the repo and say the history was rewritten with git-filter-repo.
    Takes days.
  - **(b) Recommended if you go public:** create a new empty repo and push the clean branches to it.
    The old repo stays private (or gets deleted). Nothing old comes along, and you keep a
    known-clean start.
- **Going public is your decision.** My view: the only upside is free CI, about $30/month (item 1).
  The downside is permanent: one missed detail about you or the people around you, and the code
  shows exactly how your assistant's safety rules work. Pay for CI instead.

## 4. Guard rails that need you once

- **PERSONAL_TERMS secret:** repo Settings → Secrets and variables → Actions → New secret
  `PERSONAL_TERMS`, one term per line (your names, phone numbers, address parts, school, people's
  names). CI uses it to block any commit or release zip that contains one. It is never printed.
- **On the PC:** put the same list in `C:\Kairo\data\config\personal-terms.txt` (Kairo never copies
  it anywhere).
- **On the laptop:** `~/.config/kairo/personal-terms.txt`, then in `~/kairo` run
  `git config core.hooksPath scripts/hooks`, so every commit is checked before it is made.
- **Order matters:** add the secret *before* merging the guard rails. Without it CI goes red on
  purpose and nothing is released (fail closed).
- **Your call: add the guard rails to the protected list.** That list is never auto-merged (hooks,
  policy, approvals, updater, settings). I'd add `scripts/privacy_scan.py`, `scripts/hooks/`,
  `.gitleaks.toml` and the privacy jobs in `.github/workflows/ci.yml`. A change that weakens them
  should need your eyes. This edits CLAUDE.md, so it's yours to OK.

## 5. `kairo diag` and the speed test need small rule exceptions (label needs-owner)

- **One more module may use the network: `app/diag.py`.** It makes a single GET to Kairo's own
  server on 127.0.0.1. The address is fixed in code and no proxy is used. That is the only way the
  command can see what is loaded and on the GPU. The scan test that keeps the list of
  network-using modules short stays strict.
- **Two more: `bench_voice.py` and `bench_phone.py`** (the speed test). They are a fake phone that
  calls Kairo's own `/api/rtc/offer` on 127.0.0.1 or Kairo's own `*.ts.net` address: tailnet only,
  no STUN/TURN. Only you or `kairo diag` can start them; the model can't. A run refuses to start
  while a real call is on.
- **`C:\Kairo\bin` goes on the system PATH,** so `kairo diag` works in any terminal. The
  kairo-svc account can only read and run files there, never write them, so nothing Kairo runs can
  plant a program in that folder. The Windows CI install test checks this, and checks that
  uninstall removes it.
- **If you say no to either:** `python -m app.diag` from the release folder still works. It just
  can't see the live state.

## 6. Bigger local models, free cloud models, layering (your questions, 2026-10-23)

The full research with numbers is in the morning report. The voice works without any of these.

| ID | Decision | Needed for |
|---|---|---|
| D1 | Allow a model other than Claude for named non-voice jobs. Today the plan allows it only for calls and the voice button. | free cloud page summaries; the shared voice-model slot for background jobs |
| D2 | Free cloud: the two hosts (Groq, Cloudflare Workers AI), and who holds the accounts. Both require the account holder to be **18 or older**. | page summaries on a free model |
| D3 | Kairo may download a public page whose address you paste. This is a new kind of network destination. | page summaries on any model |
| D4 | Only if you want bigger local models: change the plan's phase 4 rule (bigger local models only with 32 GB RAM) and the quiet-hours rule (no jobs 23:00-06:00), and approve a 13-17 GB download. Run the benchmark first (one hour, Kairo stopped). | a 26-35B model as a background lane |

- Not used, with reasons in the report: **Colibri** needs the whole model in RAM for its fast mode, which is more than this PC has. **AirLLM** reads the whole model from disk for every word: seconds per word. **FreeLLM API itself:** it sends one request to up to 4 different providers, has no list of allowed providers, and adds Node.js.
