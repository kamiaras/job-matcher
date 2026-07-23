# Automated Job Matching System

Daily GitHub Actions pipeline that fetches jobs, strictly matches requirements against your resume lines via Gemini structured outputs, and records seen jobs in `seen_jobs.json`.

## Setup

1. **Push this repo to GitHub** (ensure the default branch is `main`):

   ```bash
   git init
   git add .
   git commit -m "Initial job matcher scaffold"
   git branch -M main
   git remote add origin git@github.com:<YOU>/<REPO>.git
   git push -u origin main
   ```

2. **Add the repository secret** `GEMINI_API_KEY`:
   - GitHub → Settings → Secrets and variables → Actions → New repository secret
   - Name: `GEMINI_API_KEY`
   - Value: your Google Gemini API key

   For local runs, put the same key in a gitignored `.env`:

   ```bash
   echo 'GEMINI_API_KEY=your_key_here' > .env
   ```

3. **Edit targeting and resume**:
   - `config.json` — titles, companies, ATS boards (`greenhouse` / `ashby` / `lever`), `location_requirement`
   - `resume.txt` — one experience line per line (`#` comments and blank lines are ignored)

4. **Run the workflow**:
   - Actions → **Daily Job Search** → Run workflow (or wait for the daily cron at 08:00 UTC)

5. **View results**:
   - Matches appear in the Actions run **Summary** tab (`GITHUB_STEP_SUMMARY`)
   - Memory of processed jobs is committed back to `main` in `seen_jobs.json`

## Optional

- Override the model with `GEMINI_MODEL` (default: `gemini-flash-latest`).

## Notes

- Live ATS fetchers: Greenhouse (`board_token`), Ashby (`board_name`), Lever (`site`).
- Titles in `config.json` pre-filter jobs before the LLM runs (keeps API spend down).
- If live sources return nothing, placeholder sample jobs exercise the matcher end-to-end.
- Never commit `.env` — it is listed in `.gitignore`.
