# Automated Job Matching System

A daily pipeline that scrapes public ATS job boards, filters for roles that match your target titles, and uses Gemini to strictly check each posting’s **requirements** against your resume. Matches show up in the GitHub Actions summary; every processed job is remembered in `seen_jobs.json` so it isn’t scored again.

## What it does

Each run (cron at 08:00 UTC, or manual workflow dispatch):

1. **Fetch** openings from companies listed in `config.json` via public APIs:
   - **Greenhouse** (`board_token`)
   - **Ashby** (`board_name`)
   - **Lever** (`site`)
2. **Pre-filter by title** — keeps jobs whose titles match phrases in `config.json` (e.g. “PhD”, “Optimization”, “Software Engineer”), then drops seniority markers listed in `title_exclusions` (e.g. Senior, Staff, Principal), so the LLM isn’t called on every posting.
3. **Skip already-seen jobs** — IDs stored in `seen_jobs.json` are ignored.
4. **Strict LLM match** — Gemini compares the job’s requirements (and US location eligibility) to lines in `resume.txt`. Soft preferences, culture blurbs, and “nice to haves” are ignored; a job matches only if every hard requirement is supported by the resume.
5. **Report & remember** — matches are written to the Actions run Summary; all evaluated jobs are appended to `seen_jobs.json` and committed back to `main`.

If live boards return nothing, a few placeholder sample jobs exercise the matcher end-to-end.

## Project layout

| File | Role |
| --- | --- |
| `scraper.py` | Fetch → filter → match → persist |
| `config.json` | Target titles, title exclusions, company ATS boards, location (`US`) |
| `resume.txt` | One experience claim per line (`#` comments / blanks ignored) |
| `seen_jobs.json` | Dedup memory of processed jobs |
| `.github/workflows/daily_job_search.yml` | Daily GitHub Actions runner |

## Setup

1. **Push this repo to GitHub** (default branch `main`).

2. **Add the repository secret** `GEMINI_API_KEY`  
   GitHub → Settings → Secrets and variables → Actions → New repository secret.

   For local runs:

   ```bash
   echo 'GEMINI_API_KEY=your_key_here' > .env
   pip install -r requirements.txt
   python scraper.py
   ```

3. **Edit targeting and resume**
   - `config.json` — titles, ATS boards, `location_requirement`
   - `resume.txt` — atomic experience lines the matcher can cite

4. **Run the workflow**  
   Actions → **Daily Job Search** → Run workflow (or wait for the daily cron).

5. **View results**  
   Matches appear in the Actions run **Summary** tab. Processed IDs land in `seen_jobs.json` on `main`.

## Optional

- Models default to `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` (round-robin + failover on 429). Override with `GEMINI_MODELS` (comma-separated) or `GEMINI_MODEL`.
- Pace Gemini calls with `GEMINI_REQUEST_INTERVAL_SEC` (default: `7`, ~5 RPM per model).
- Cap work per run with `GEMINI_MAX_JOBS_PER_RUN` (default: `40`); leftovers stay unseen for the next run.
- Tune 429 retries with `GEMINI_MAX_RETRIES` (default: `8`). Rate-limited jobs are **not** written to `seen_jobs.json`.

## Notes

- Titles in `config.json` pre-filter before the LLM runs (keeps API spend down).
- Free-tier Gemini quotas are usually per-model (~5 RPM each); the scraper round-robins Flash-Lite models, fails over on `429`, and stops early rather than burning unseen jobs.
- Never commit `.env` — it is listed in `.gitignore`.
