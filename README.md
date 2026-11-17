# Running the evaluation set unattended

These steps set up this repository on a new machine. A single command then processes every ticket in the evaluation file with no human input, logs every decision, writes four report files, and exits.

| Phase | Time |
|---|---|
| One-time setup (steps 1 to 5) | 20 to 40 minutes, mostly installing packages and downloading models |
| The run (step 6) | About 1 minute to load the models, then about 3.5 seconds per ticket (about 8 minutes for 120 tickets) |

Run every command from the repository folder, in the order given. If you open a new terminal window, `cd` into the repository folder first. Each step says what you should see before you move on.

## 1. Check the requirements

| Item | Requirement |
|---|---|
| Operating system | 64-bit Windows 10 or 11, or x86_64 Linux with glibc 2.28 or newer (Ubuntu 20.04+, Debian 10+, RHEL 8+) |
| Python | **3.14** (any 3.14.x, 64-bit). Not 3.15 or 3.13: every pinned package was installed and tested on 3.14 |
| Git | Any recent version |
| Hardware | 8 GB RAM and 5 GB of free disk space. No GPU is needed |
| Network | Outbound HTTPS to `pypi.org`, `files.pythonhosted.org`, `github.com`, `huggingface.co` and `api.groq.com`. Linux also needs `download.pytorch.org` |
| Groq API key | On Groq's paid **Developer plan**. Each ticket takes about 4 to 5 model calls and 8,000 tokens; the free plan allows 200,000 tokens a day for this model, which covers only about 25 tickets |
| Evaluation file | A JSON array of tickets in the same format as `data/validation_tickets.json`. The `labels` field is optional; without it, the report leaves out the sections that compare against labels |

macOS on Apple Silicon (macOS 14 or newer) should also work, because every package is available for it, but it has not been tested on a Mac. Follow the Linux steps and skip the CPU-only PyTorch line.

## 2. Get the code

```
git clone <repository-url> cloudserve-triage
cd cloudserve-triage
```

Replace `<repository-url>` with this repository's GitHub URL. These two commands are the same on Windows and Linux.

## 3. Install (one time)

**Windows (PowerShell)**

```powershell
py -3.14 --version
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

- The first command must print `Python 3.14.` followed by a number. If `py` is not recognised, or 3.14 is not found, install Python 3.14 (64-bit) from https://www.python.org/downloads/ with the "py launcher" option ticked. Then open a new PowerShell window and `cd` back into the repository folder.
- The install takes 10 to 25 minutes and ends with `Successfully installed ...`. A notice that a newer version of pip is available may follow; ignore it.
- You never need to activate the virtual environment, because every command calls `.\.venv\Scripts\python.exe` directly.

**Linux (bash)**

```bash
python3.14 --version
python3.14 -m venv .venv
.venv/bin/python -m pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r requirements.txt
```

- The first command must print `Python 3.14.` followed by a number. On Ubuntu, if `python3.14` is missing, install it with `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update && sudo apt install python3.14 python3.14-venv`.
- The PyTorch line installs the CPU-only build first, which avoids a multi-gigabyte GPU download.
- The install ends with `Successfully installed ...`. A notice that a newer version of pip is available may follow; ignore it.

## 4. Add the API key (one time)

**Windows (PowerShell)**

```powershell
Copy-Item .env.example .env
notepad .env
```

**Linux (bash)**

```bash
cp .env.example .env
nano .env
```

In the editor, change exactly these two lines, then save the file:

```
GROQ_API_KEY=<your Groq API key>
LLM_CACHE_ENABLED=false
```

- Leave every other line as it is. `OPENROUTER_API_KEY` is an optional fallback provider: leave it empty unless you have a funded OpenRouter key.
- `LLM_CACHE_ENABLED=false` makes every model call live, so the latency figures are real. The published results were produced the same way.
- For production or any shared network deployment, also set `APP_ENV=production`, a long random `API_KEY` (clients send it as the `X-API-Key` header) and a long random `CUSTOMER_ID_SALT`. The service refuses to start in production without both.
- `.env` holds your key, so never commit or share it.

## 5. Check the installation (about 5 minutes)

**Windows (PowerShell)**

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_kb_manifest
.\.venv\Scripts\python.exe -m scripts.check_providers
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\python.exe -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/preflight --limit 3
```

**Linux (bash)**

```bash
.venv/bin/python -m scripts.verify_kb_manifest
.venv/bin/python -m scripts.check_providers
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/preflight --limit 3
```

| Command | What you should see |
|---|---|
| `scripts.verify_kb_manifest` | `OK: 29 official articles match kb_manifest.lock` |
| `scripts.check_providers` | A line starting with `OK    groq` |
| `pytest` | `71 passed, 4 skipped`. The 4 skipped tests exercise the real local models and are optional |
| `evaluation.harness ... --limit 3` | A last line that starts with `Processed 3` and shows `decisions logged 3`. This trial run processes 3 sample tickets. The first time, it also downloads the three local models (about 700 MB) |

If any check fails, fix it using the Troubleshooting table at the end before you continue.

## 6. Run the evaluation set (unattended)

First, copy the evaluation file into the repository under a fixed name, because the run command uses that name. Then start the run.

**Windows (PowerShell)**

```powershell
Copy-Item "<path to the evaluation file>" data\evaluation_tickets.json
$env:PYTHONUTF8 = "1"
cmd /c ".venv\Scripts\python.exe -m evaluation.harness --input data\evaluation_tickets.json --output evaluation\results\final_run > evaluation\results\final_run.log 2>&1"
$code = $LASTEXITCODE
"exit code: $code" | Out-File -Append -Encoding ascii evaluation\results\final_run.log
"exit code: $code"
```

The `cmd /c` command returns only when the run has finished. Until then, keep the window open and the computer awake: plug it in and turn sleep off. The last two lines record the exit code at the end of the log and print it.

**Linux (bash)**

```bash
cp "<path to the evaluation file>" data/evaluation_tickets.json
nohup sh -c '.venv/bin/python -m evaluation.harness --input data/evaluation_tickets.json --output evaluation/results/final_run; echo "exit code: $?"' > evaluation/results/final_run.log 2>&1 &
```

The run continues in the background, even if you log out or the SSH session drops. It has finished when the last line of the log starts with `exit code:`. To watch it, run `tail -f evaluation/results/final_run.log`; Ctrl+C stops watching but does not stop the run.

On both systems, the log shows progress every 10 tickets.

## 7. Check the result

**Windows:** `Get-Content evaluation\results\final_run.log -Tail 3`

**Linux:** `tail -n 3 evaluation/results/final_run.log`

The run succeeded if all of these are true:
- The last line of the log is `exit code: 0`.
- The line `Processed N | answered ... | decisions logged N -> ...` shows the number of tickets in the evaluation file in both places.
- In `evaluation/results/final_run/metrics_report.md`, both lines under **Invariants** say **True**, and `pipeline_errors` is `0`.

`pipeline_errors` above 0 means some model calls kept failing (usually the API key's limits). The run still finishes and those tickets are safely escalated to a human, but they were not really assessed. In that case, fix the cause and run step 6 again with a new output name. The **Wall clock** line in the report explains the run time, including any rate-limit waiting.

The output folder `evaluation/results/final_run/` contains:

| File | Contents |
|---|---|
| `predictions.jsonl` | One line per ticket: the predicted intent and confidence, the routing decision and its plain-English reason, the reply or the escalation brief, the cited articles, and the latency |
| `decision_audit_log.jsonl` | The tamper-evident (hash-chained) audit record of every decision |
| `metrics_report.md` and `metrics_report.json` | Every metric, calculated in code: volume, business, technical and governance, plus accuracy, safety and fairness against the labels when the file has labels |

| Exit code | Meaning | What to do |
|---|---|---|
| 0 | Success | Nothing |
| 1 | A ticket ended without an outcome, or a decision was not logged. This should never happen | Send the log and the output folder to the project owner |
| 2 | The input file is missing or is not valid JSON, or the calibration file is missing | The `ERROR:` line in the log says which. Fix it and run step 6 again |

## Troubleshooting

| Problem | Fix |
|---|---|
| `py -3.14` or `python3.14` is not found | Install Python 3.14 (64-bit) as described in step 3 |
| pip reports `No matching distribution found` (often for `torch`) | The virtual environment was created with the wrong Python version. Delete the `.venv` folder and redo step 3 with Python 3.14 |
| `check_providers` shows `FAIL  groq` with `401` | The key is wrong. Correct `GROQ_API_KEY` in `.env` |
| The API returns `401 missing or invalid API key` | The service has `API_KEY` set: send it as the `X-API-Key` header (or clear `API_KEY` for local development only) |
| `check_providers` shows `429`, or the report shows a long rate-limit wait | The key's plan limits are too low. Use a key on the Developer plan |
| `check_providers` shows `404` | The model `openai/gpt-oss-120b` is not enabled for the key's organisation. Enable it in the Groq console, under model permissions |
| The trial run fails while downloading models | The machine cannot reach `huggingface.co`. Allow it through the firewall, or set the `HTTPS_PROXY` environment variable |
| `FATAL` mentioning `kb_manifest.lock` | `data/documentation.json` was modified. Restore it with `git checkout -- data/documentation.json kb_manifest.lock` |
| Exit code 2 with `unreadable input` | The evaluation file is not a JSON array of tickets. A JSON object with a `tickets` array also works |
| The run was interrupted (sleep, closed window, reboot) | Run step 6 again with a new output name, for example `final_run_2`. Nothing needs cleaning up first |
| Warnings about "unauthenticated requests to the HF Hub" or "`torch.jit.script` is not supported in Python 3.14+" | These are expected and harmless |

## Good to know

- **Don't change these files:**
  - `artifacts/thresholds_v1.json` is the frozen calibration, and every routing threshold comes from it.
  - `data/documentation.json` and `kb_manifest.lock`: the system refuses to start if the knowledge base no longer matches the lock.
- **One run at a time:** don't run two evaluations at once from the same folder. They share the local database and index in `storage/`.
- **Parallel processing:** `--concurrency N` processes N tickets at once. It is faster, but the latency figures then include queueing, and it uses N times as many tokens per minute. Keep the default (one at a time) for official runs.
- **Alternatives to the flags:** instead of `--input` and `--output`, you can set `EVAL_INPUT_PATH` and `EVAL_OUTPUT_DIR` (in the environment or in `.env`). `python scripts/run_final_evaluation.py` is an alias for the same command.
