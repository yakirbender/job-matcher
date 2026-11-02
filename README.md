# job-matcher

Pulls software job postings (English and French searches) for Montreal, Florida, and remote
Canada/US roles into SQLite. Later milestones add bilingual filtering, clearance and French
tagging, and CV-based LLM scoring.

## Setup

Requires [uv](https://docs.astral.sh/uv/). From the project root:

```bash
uv sync
```

Copy `.env.example` to `.env` in the project root and add your Adzuna `app_id` and `app_key`
(developer.adzuna.com → Dashboard → API Access Details). `.env` is git-ignored.

## Usage

```bash
uv run jm fetch --plan                          # list searches and request budget, call nothing
uv run jm fetch -r orlando --limit 3            # one region, at most 3 API calls
uv run jm fetch -r remote_ca -t "développeur junior"   # one exact term from the config
uv run jm fetch                                 # every enabled region
uv run jm stats                                 # what's in the database
```

| Option | Effect |
|---|---|
| `-r, --region` | Only these regions (repeatable). |
| `-t, --term` | Only these exact terms from `searches.example.yaml` (repeatable). |
| `--limit N` | Stop after N API calls in this run. |
| `--max-pages N` | Pages per search (50 results each). |
| `--all-pages` | Don't stop a search at the first page with nothing new. |
| `--plan` | Show what would run, without calling the API. |

Searches, term lists, and limits live in `config/searches.example.yaml`. The database is
`data/jobs.db` (override with `JM_DB_PATH`).

## Request budgets

Every call is counted in the database. A run stops at whichever is reached first:

- `limits.per_day`: rolling 24 hours
- `limits.per_month`: rolling 30 days
- `--limit` for this run

The Adzuna trial key ("Pending Activation") allows 25 test calls in total, so the config
ships with `per_month: 20`. After upgrading to API Access (500/day, 5,000/month), set
`per_day: 450` and `per_month: 4500`. A full default run is 80 calls.

Fetches are incremental: results come newest first, and a search stops at the first page
with nothing new.

## How results are labeled

- **Metro**: `montreal` (including Laval, Longueuil, and other nearby cities), `orlando`,
  `tampa`, `miami`, or `florida_other`, from Adzuna's country › state › county › city data.
- **Work mode**: `remote`, `hybrid`, `onsite`, or unknown, read from the title and snippet in
  English and French. Unknown means the snippet doesn't say, not that the job is on-site.
- **Likely duplicates**: same title, company, and city are flagged with `duplicate_of`,
  not merged.

## Limitations

- Adzuna returns only a ~500-character description snippet ending in "…". Requirements
  and work mode stated later in a posting are invisible here.
- Adzuna matches search words against the full posting, which we can't see. A search for
  "développeur junior" can return senior roles that mention juniors somewhere, and remote
  searches return jobs whose snippet never says "remote". Treat search terms as a wide net;
  filtering happens locally (milestone 2).
- Adzuna has no remote filter. Remote searches add "remote"/"télétravail" as keywords.
- Some regions are high-volume: one page of remote US results covers well under an hour
  of new postings.

## Development

```bash
uv run pytest
uv run ruff check .
```

`tests/fixtures/adzuna_search_page.json` holds invented results in Adzuna's response format.

Job listings come from the [Adzuna API](https://www.adzuna.com).
