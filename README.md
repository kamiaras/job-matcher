# Automated Job Matching System

A daily pipeline that scrapes public ATS job boards, applies deterministic
pre-filters, and uses Gemini to strictly check each posting’s **requirements**
against your resume. Matches show up in the GitHub Actions summary; every
processed job is remembered in `seen_jobs.json` so it isn’t scored again.

## What it does

Each run (cron at 08:00 UTC, or manual workflow dispatch):

1. **Fetch** openings from companies in `config.json` (Greenhouse / Ashby / Lever /
   Workable / Eightfold / Google Careers / Meta / Uber / Workday / Amazon / Apple /
   Microsoft / ByteDance / iCIMS API / Bloomberg Avature / LinkedIn / Citadel +
   Citadel Securities HTML / Two Sigma Avature / D. E. Shaw / Hudson River Trading /
   Goldman Sachs Higher GraphQL / Oracle Cloud CE e.g. JPMorgan).
2. **Title match** — keep jobs whose titles match phrases in `titles`.
3. **Title exclusion** — drop seniority / role markers in `title_exclusions`.
4. **US location** — drop postings that only list places in `non_us_locations`; keep
   multi-location jobs that still include a US option (and ambiguous / empty /
   bare-Remote listings).
5. **Skip already-seen jobs** — IDs stored in `seen_jobs.json` are ignored.
6. **Strict LLM match** — Gemini compares the job’s requirements (and US location
   eligibility) to lines in `resume.txt`. Soft preferences, culture blurbs, and
   “nice to haves” are ignored; a job matches only if every hard requirement is
   supported by the resume.
7. **Report & remember** — matches are written to the Actions run Summary; all
   evaluated jobs are appended to `seen_jobs.json` and committed back to `main`.

If live boards return nothing, a few placeholder sample jobs exercise the matcher
end-to-end.

## Project layout

| File | Role |
| --- | --- |
| `scraper.py` | Fetch → filter → match → persist |
| `config.json` | Target titles, exclusions, non-US locations, ATS boards |
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
   - `config.json` — titles, title exclusions, non-US locations, ATS boards
   - `resume.txt` — atomic experience lines the matcher can cite

4. **Run the workflow**  
   Actions → **Daily Job Search** → Run workflow (or wait for the daily cron).

5. **View results**  
   Matches appear in the Actions run **Summary** tab. Processed IDs land in
   `seen_jobs.json` on `main`.

## Deterministic dry-run

To inspect what survives title/location filters without calling Gemini or
updating `seen_jobs.json`:

```bash
python scraper.py --deterministic-only
```

Writes survivors to `candidates.json` (gitignored).

## Optional

- Models default to `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` (round-robin
  + failover on 429). Override with `GEMINI_MODELS` (comma-separated) or
  `GEMINI_MODEL`.
- Pace Gemini calls with `GEMINI_REQUEST_INTERVAL_SEC` (default: `7`, ~5 RPM per
  model).
- Cap work per run with `GEMINI_MAX_JOBS_PER_RUN` (default: `40`); leftovers stay
  unseen for the next run.
- Tune 429 retries with `GEMINI_MAX_RETRIES` (default: `8`). Rate-limited jobs are
  **not** written to `seen_jobs.json`.

## Notes

- Deterministic filters run before the LLM (keeps API spend down).
- Free-tier Gemini quotas are usually per-model (~5 RPM each); the scraper
  round-robins Flash-Lite models, fails over on `429`, and stops early rather
  than burning unseen jobs.
- Never commit `.env` — it is listed in `.gitignore`.
