# Windows setup (development PC)

The PC is a development machine only. After deployment, nothing in production
depends on it: GitHub Actions runs the pipeline, Supabase stores memory, Drive
stores backups.

## 1. Prerequisites

| Tool | Version | Why |
|---|---|---|
| Windows | 10 or 11 | — |
| Python | 3.12 (3.13 also supported) | CI tests both; `requires-python = ">=3.12"` |
| Git | any current | version control and the push to GitHub |
| GitHub account | free plan is enough | Actions, Secrets |
| Editor | VS Code or OpenCode | no build step required |

Python 3.12 is the reference version. 3.13 is tested in CI, and this repository
was developed on 3.13.15; both are acceptable. Anything older fails on `StrEnum`.

Check what you have:

```powershell
python --version
git --version
```

## 2. Get the code

```powershell
cd $HOME\Desktop
git clone https://github.com/<you>/ai-radar.git "AI Radar"
cd "AI Radar"
```

> **OneDrive warning.** If the project sits inside a synced OneDrive folder
> (`C:\Users\<you>\OneDrive\Desktop\...`), OneDrive will try to sync `.venv`,
> `artifacts/` and `.env`. That causes file locks during `pip install`, slow
> syncs, and — worst — a copy of your secrets in cloud storage. Either move the
> repository to `C:\dev\ai-radar` or exclude the folder from OneDrive before you
> create `.env`.

## 3. Virtual environment and dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.lock
pip install -e .
```

If PowerShell refuses to run the activation script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

`requirements.lock` contains exact pins, which is what CI installs. If `pip
install -e .` fails with `BackendUnavailable: Cannot import
'setuptools.build_meta'` while offline, install the build backend first and skip
build isolation:

```powershell
python -m pip install setuptools wheel
python -m pip install --no-deps --no-build-isolation -e .
```

Verify both entry points work:

```powershell
ai-radar --help
python -m ai_radar --help
```

They are the same code path (`cli.main`); the workflows use `python -m ai_radar`
with `PYTHONPATH=src`, so no install step is required in CI.

## 4. Configuration

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` is git-ignored. Fill in only what you have; every integration reports
itself as NOT CONFIGURED until its values are present. Never commit `.env`, and
never paste its contents into a chat, an issue or a commit.

| Variable | Where to get it |
|---|---|
| `DATABASE_URL` | Supabase → Connect → **Shared pooler, session mode** (port `5432`, user `postgres.<ref>`). Keep `sslmode=require`. Port `6543` is the transaction pooler and is refused: no prepared statements, no session advisory lock. |
| `TELEGRAM_BOT_TOKEN` | Telegram → `@BotFather` → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id` |
| `FREEAI_API_KEY` | <https://free.ai/api/> — free plan 30K tokens/day; optional but preferred when present |
| `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> |
| `GROQ_API_KEY` | <https://console.groq.com/keys> |
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → Personal access tokens (read-only, public repo scope is enough) |
| `GOOGLE_OAUTH_*` | See [backup-restore.md](backup-restore.md); the Drive folder id is optional |
| `HEALTHCHECKS_PING_URL` | Create a check at <https://healthchecks.io>; copy its ping URL |
| `RESTORE_DATABASE_URL` | A second, empty database used only for restore drills |

## 5. Verify, in this order

```powershell
python -m compileall -q src                 # syntax across every module
python -m pytest tests -q                   # offline suite: 295 passed, 20 skipped
ai-radar sources --validate                 # 24 sources, 7 categories, no problems
ai-radar doctor                             # PASS/FAIL/WARN per integration
ai-radar collect --source openai_news       # one real collector, nothing persisted
ai-radar migrate                            # create the schema (needs DATABASE_URL)
ai-radar notify --test                      # one Telegram message (needs the bot)
ai-radar research --dry-run                 # full pipeline, never sends Telegram
```

Expected `doctor` output on a fresh machine without credentials:

```
[PASS] configuration    24 sources, 7 categories, confidence threshold 0.57 (calibrated)
[PASS] source registry  structure valid
[PASS] schedule policy  workflow crons match config/settings.yaml
[FAIL] database         DATABASE_URL not set
[WARN] Gemini           API key not set
[WARN] Groq             API key not set
[FAIL] telegram         bot token / chat id not set
[WARN] google drive     OAuth client / refresh token / folder id not set
[WARN] heartbeat        HEALTHCHECKS_PING_URL not set
```

Exit codes: `0` success, `1` runtime failure, `2` configuration/precondition
problem. `doctor` returns `2` whenever a required integration is missing, which
is why `research.yml` runs it with `continue-on-error: true` and lets the
pipeline itself decide.

## 6. Tests that need more than a PC

```powershell
$env:TEST_DATABASE_URL = "postgresql://...:5432/postgres"
python -m pytest tests/integration/test_postgres.py -m postgres -q

$env:GEMINI_API_KEY = "..."; $env:GROQ_API_KEY = "..."
python -m pytest tests/integration/test_live_services.py -m live -q
```

Without those variables the tests skip rather than pass vacuously; the skip
reason appears in `pytest -rs`.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named ai_radar.__main__` | running `python -m ai_radar` from a package without the module | this repository has `src/ai_radar/__main__.py`; make sure you are on the current commit and either installed the package or set `PYTHONPATH=src` |
| `psycopg.OperationalError: ... timeout` | wrong host/port, or IPv6-only Supabase host on an IPv4 network | use the shared pooler in session mode on `5432`, keep `?sslmode=require` |
| `could not connect to PostgreSQL: password authentication failed` | password in the DSN was URL-encoded twice, or reset in the dashboard | copy the DSN again from Supabase |
| `port 6543 is a transaction-mode pooler` | the DSN points at the transaction pooler | copy the session-mode string (port 5432) from Supabase → Connect |
| `Telegram rejected the credentials (404)` | chat id wrong, or the bot was never started | message the bot once, re-read `getUpdates` |
| `Telegram rejected the payload: can't parse entities` | unescaped `<` or `&` in a title | formatting is escaped in `notifications/format.py`; report it with the offending title |
| Tests pass but `ai-radar` is an old version | editable install pointing at a moved folder | `pip install -e .` again |
| Workflow fails at `python -m ai_radar` | `PYTHONPATH` not set in that job | every job in `.github/workflows/` sets `PYTHONPATH: src` |

## 8. What not to do on this PC

- Do not run a scheduler, cron or long-lived process as the production engine.
- Do not point production at a local PostgreSQL.
- Do not keep the only copy of `.env` here — the production copy belongs in
  GitHub encrypted secrets.
- Do not store backups only on this disk.
