# Deterministic Job Pre-Filter (no LLM)

Branch for iterating on **deterministic** filters only. Fetches public ATS boards,
applies title match + title exclusion from `config.json`, and dumps survivors to
stdout and `candidates.json`. **Does not call Gemini** and **does not update**
`seen_jobs.json`.

## Pipeline

1. **Fetch** openings from companies in `config.json` (Greenhouse / Ashby / Lever).
2. **Title match** — keep jobs whose titles match phrases in `titles`.
3. **Title exclusion** — drop seniority / role markers in `title_exclusions`.
4. **Report** — print candidates and write `candidates.json` (id, title, company, location, url).

Jobs already in `seen_jobs.json` are labeled `[seen]` vs `[new ]` for context only.

## Run locally

```bash
pip install -r requirements.txt
python scraper.py
```

No API key required. Inspect `candidates.json`, tighten `titles` /
`title_exclusions` (or add more deterministic rules in `scraper.py`), re-run.

## Next

Once the deterministic pool looks right, port the extra filters back to `main`
before the LLM step.
