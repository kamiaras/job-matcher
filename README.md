# Job Matcher

Fetch job listings, filter them by title and location, and use Gemini to compare their hard requirements with a supplied resume. Run it locally when needed.

Daily automation has been removed. The tool runs only when you invoke it.

The repository includes adapters for several public recruiting sites. Their behavior varies by site, and site changes can break an adapter. Review the results before using them.

## Setup

Use Python 3.12 and a virtual environment:

```bash
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on macOS/Linux or `.venv\Scripts\Activate.ps1` in PowerShell. Then install the dependencies and copy the examples:

```bash
python -m pip install -r requirements.txt
cp config.example.json config.json
cp resume.example.txt resume.txt
cp .env.example .env
```

Edit the local copies:

- `config.json`: choose title filters and career sites.
- `resume.txt`: write one factual qualification or experience claim per line. Blank lines and lines starting with `#` are ignored.
- `.env`: replace the example `GEMINI_API_KEY` value with your key for Gemini matching.

The supplied resume is fictional. The example configuration has no live career sites, so it returns no candidates until you add a source.

## Configure career sites

The `websites` array selects the sources. For example, replace the placeholder below with a public Greenhouse board identifier:

```json
{
  "name": "Example Employer",
  "type": "greenhouse",
  "board_token": "replace-with-public-board-token"
}
```

Add that object to `websites` in `config.json`. A board identifier is a public URL component, not an API key. Other adapter fields are defined in [`job_matcher/sources/__init__.py`](job_matcher/sources/__init__.py).

Configuration fields:

| Field | Meaning |
| --- | --- |
| `titles` | Keep titles containing one of these phrases; an empty list accepts all titles. |
| `title_exclusions` | Drop titles containing one of these exclusion phrases. |
| `non_us_locations` | Drop listings containing these location phrases unless a US location is also detected. Empty or ambiguous locations are kept. |
| `websites` | Career-site entries passed to the source adapters. |

The location filter is US-oriented. It does not verify work authorization or eligibility. If no jobs survive fetching and filtering, the result is an empty candidate list.

## Run

For Gemini matching:

```bash
python scraper.py
```

The program prints results and short reasons to the terminal. It records evaluated jobs in the local `seen_jobs.json` file, including match status and processing time. Later runs skip those jobs. Normal matching does not create `candidates.json`.

To inspect title and location filtering without calling Gemini:

```bash
python scraper.py --deterministic-only
```

This mode still contacts the configured career sites. It prints the filtered candidates and writes `candidates.json`, but does not update `seen_jobs.json`. It does not need a Gemini API key.

## Inputs and results

Gemini receives the loaded resume claims and each job's requirements. Supply only the professional information needed for matching. Terminal output may include reasons based on those claims.

Keep your real `config.json`, `resume.txt`, `.env`, `seen_jobs.json`, and `candidates.json` local. These runtime files are ignored by Git. The tracked example files are for sharing and setup.

The matcher uses the supplied resume without a fixed experience level or degree status. It accepts an alternative degree or experience path only when the job states that path and the resume supports it. Missing information is treated as unsupported, so review a rejection before drawing conclusions about a candidate.

## Model settings

The default model choices are `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite`. The code rotates between them and retries rate-limit errors. Model availability and request limits depend on your API account.

Optional environment settings:

| Variable | Purpose | Default |
| --- | --- | --- |
| `GEMINI_MODELS` | Comma-separated model choices; `GEMINI_MODEL` sets a single choice. | Models listed above |
| `GEMINI_REQUEST_INTERVAL_SEC` | Delay between jobs. | `7` |
| `GEMINI_MAX_JOBS_PER_RUN` | Limit evaluated jobs per run. | `40` |
| `GEMINI_MAX_RETRIES` | Maximum attempts for a job. | `8` |

## Offline tests

Run the tests without contacting career sites or Gemini:

```bash
python -m unittest discover -s tests -v
```

## Code layout

- [`scraper.py`](scraper.py): command-line entry point.
- [`job_matcher/sources/`](job_matcher/sources/): recruiting-site adapters and sample jobs.
- [`job_matcher/filters.py`](job_matcher/filters.py): title and location filters.
- [`job_matcher/matcher.py`](job_matcher/matcher.py): Gemini requests and result parsing.
- [`job_matcher/prompts/system_match.txt`](job_matcher/prompts/system_match.txt): matching rules.
- [`job_matcher/persistence.py`](job_matcher/persistence.py): local inputs and state.
