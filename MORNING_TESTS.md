# Morning tests

The overnight work is built and tested in the cloud (Linux, no GPU, no iPhone). These are the
things only your PC and your iPhone can prove. Do them in this order. Write the numbers down (or
take a screenshot) and send them to me: they decide which switches stay on.

Everything new is behind a switch in **More → System → Experiments**. Plain fixes are on; anything
that needs your OK or a measurement is off. Back to default is one tap.

---

## 1. Install the new version (5 min)

1. Download the setup zip I sent (Kairo-Setup-…zip), right-click → Extract All.
2. Double-click **Install Kairo.bat** in the extracted folder and wait for "done". Nothing is
   asked again; your logins and keys stay.
3. Open a **new** Command Prompt (one opened before doesn't know the new `kairo` command).

## 2. Is the voice stack healthy? (2 min)

```
kairo diag
```

- The last line should say `NOT HEALTHY` or `HEALTHY` and the exit code. Every FAIL line says why.
- Expected WARNs that are fine for now: `system.hf_offline` (until the next update writes it),
  `speaker.enrolled` if you haven't taught Kairo your voice yet.
- The same page in the app: **More → System → Voice check**.
- **Send me:** every line that says FAIL.

## 3. How fast is a call really? (5 min)

Kairo must be running. Don't take a call while it runs.

```
kairo bench --clips 12
```

Then the full run (about 3 minutes):

```
kairo bench
```

- It prints p50 / p90 / p95 in ms (the target is p95 ≤ 1000 ms, p50 ≤ 650 ms) and one line per
  stage (speech end → Whisper → voice model → first sound).
- **Send me:** the table it prints. This is the "before" for every switch below.

## 4. Switches to try, one at a time (10-20 min)

For each one: More → System → Experiments → turn it on → run `kairo bench --clips 12` → keep it if
p95 got better, turn it back off if p95 got worse by more than 50 ms.

| Switch | What it should change | Needs |
|---|---|---|
| `g1.cli_no_summary` | Claude's answers finish about 2 s sooner (1.99 s measured) | your OK (undocumented Claude Code setting) |
| `g3.constant_tools` | no slow turn when the voice model switches between tools and no tools (estimate 0.3-0.75 s saved on those turns) | the voice lab before/after |
| `g4.jitter_patch` | about 60 ms off every turn, and 280 → ~130 ms after a lost packet | your OK (patches the call library's buffer) |
| `g2.single_encode_langid` | German / auto-language turns: Whisper finds the language in one pass instead of two | step 5 first |

## 5. Speech to text on the GPU: the biggest open question (15 min)

This decides whether the 0.65 s target is even reachable with the current design. Admin
PowerShell. It stops Kairo while it measures, then starts it again.

```powershell
mkdir C:\Temp\kairo-bench -Force
# copy the scripts from the kairo-bench folder I sent into C:\Temp\kairo-bench, then:
$py   = 'C:\Kairo\app\current\venv\Scripts\python.exe'
$site = (Get-ChildItem C:\Kairo\tools -Directory -Filter 'voice-*' | Sort-Object LastWriteTime -Desc | Select-Object -First 1).FullName
Stop-ScheduledTask -TaskPath '\Kairo\' -TaskName 'Kairo'
nvidia-smi --query-gpu=name,driver_version,memory.total,power.limit --format=csv
& $py -I C:\Temp\kairo-bench\bench_stt_pc.py --server C:\Kairo\app\current\server --models "$site\models" --site $site --out C:\Temp\kairo-bench\stt_bench.json
& $py -I C:\Temp\kairo-bench\kokoro_pc_bench.py "$site\models\kokoro" cuda 0 30
& $py -I C:\Temp\kairo-bench\kokoro_pc_bench.py "$site\models\kokoro" cpu 0 30
Start-ScheduledTask -TaskPath '\Kairo\' -TaskName 'Kairo'
```

- **Send me:** `C:\Temp\kairo-bench\stt_bench.json` and what the Kokoro lines print.
- What it decides:
  - if Whisper's final decode p95 is over 0.6 s, the 1.0 s target needs a different speech-to-text
    design;
  - whether `int8_float16` is worth it;
  - whether Kokoro may fall back to the CPU.

## 6. Memory: does it all fit in 16 GB? (25 min, mostly waiting)

After a fresh restart and 10 minutes idle, admin PowerShell:

```powershell
powershell -File C:\Temp\kairo-bench\sample_pc.ps1 -Minutes 10 -Tag idle
# then start a call from the iPhone and keep talking now and then:
powershell -File C:\Temp\kairo-bench\sample_pc.ps1 -Minutes 15 -Tag call
```

- **Send me:** the two summary lines it prints. In particular, Windows' own RAM use decides whether
  Home Assistant can live on this PC (the research found 3.3-8.5 GB reported on other PCs).

## 7. On the iPhone (15 min)

Call Kairo from the app.

- [ ] **Tap when done:** talk in a noisy room, tap the orb when you're done. Kairo should answer
  within about 2 s of the tap.
- [ ] **"Mhm" doesn't cut Kairo off:** while Kairo talks, say "mhm", "ja", "genau". It should keep
  talking. Say "stop" or a full sentence: it should stop, after about 1-2 s. If that is too slow,
  turn `g6.transcript_barge_in` off: then any sound stops Kairo at once again.
- [ ] **Silent on the loudspeaker:** stay silent through a long answer on the loudspeaker. Kairo
  must not cut itself off. If it does, turn `g7.call_audio_mode` off and tell me.
- [ ] **No warning line about echo cancelling:** if you see one, tell me.
- [ ] **Sound check:** More → System → Sound check. Test 2 should now say "Sound check two" over
  the call line.
- [ ] **Speaker check:**
  - teach Kairo your voice in More → Settings → Voice;
  - then have someone else talk on your call: Kairo should not answer them.
- [ ] **Plain speech:** no "tool call" words, no notes, no asterisks in what Kairo says.
- [ ] **iOS version:** Settings → General → About. iOS 27.0.x has a known crackle bug; tell me
  the version.

## 8. Claude's answers, 2 s sooner? (5 min)

1. Talk to Kairo for a few turns, then open More → System → Voice speed: a note says "Claude takes up
   to … s after its answer".
2. Turn `g1.cli_no_summary` on (More → System → Experiments), talk a few turns again.
3. The note should be gone.
4. If it isn't, search C:\Kairo\data\logs\kairo.log for "claude tail" and send me the lines.

---

All of this stays on your PC. Recordings of your voice are never sent anywhere and can be deleted
after.
