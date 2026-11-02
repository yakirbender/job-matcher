# job-matcher

Pulls software job postings (English and French searches) for Montreal, Florida, and remote
Canada/US roles into SQLite, then tags and filters them against a bilingual profile.
CV-based LLM scoring comes in milestone 3.

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
uv run jm filter                                # tag and filter every stored job
uv run jm list                                  # jobs that passed, by full-/part-time
uv run jm list --employment part_time           # one employment type only
uv run jm list --rejected                       # ...or were rejected, with reasons
uv run jm list --reason seniority --limit 20    # rejected for one reason
uv run jm show 98                               # one job: text, tags + evidence, verdict
uv run jm stats                                 # what's in the database
```

| Option | Effect |
|---|---|
| `-r, --region` | Only these regions (repeatable). |
| `-t, --term` | Only these exact terms from `searches.example.yaml` (repeatable). |
| `--limit N` | Stop after N API calls in this run. |
| `--title-only / --full-text` | Match terms in job titles (config default) or anywhere in the posting. |
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

## Filtering (`jm filter`)

`config/profile.example.yaml` holds the rules. Matching ignores case and accents, works on whole
words, and folds French inclusive titles first ("Développeur.euse principal.e",
"Concepteur(trice)", "Développeur · euse" → "développeur principal", "concepteur").
Every stored job is re-screened on each run, so profile changes apply to old jobs too.

**Tags** (facts, with the matching text as evidence; see `jm show`):

| Tag | Values |
|---|---|
| `role_family` | `qa`, `support`, `implementation`, `training`, `core_dev` |
| `entry_level` | `title` (strong) or `description` (weaker) |
| `seniority` | `senior`, with the word that matched |
| `experience_years` | the largest minimum stated ("3-5 years" → 3), or `unknown` |
| `clearance` | `level:requirement`, e.g. `secret:active`, `ts_sci:obtainable`, `reliability:required` |
| `clearance_polygraph`, `citizenship_required` | when stated |
| `french` | `required` or `asset`; a posting written in French counts as required |
| `posting_language` | `en` or `fr` |
| `salary`, `work_mode` | `listed` / `estimated` / `none`; `remote` / `hybrid` / `onsite` / `unknown` |
| `employment` | `full_time`, `part_time`, `either`, or `unknown` |
| `suspicious` | `mass_template` or `scam_phrase` (shown as SUSPECT in `jm list`) |

**Rejection reasons**: `not_software` (title matches no role family), `seniority`,
`experience` (more than `max_years_required`), `internship`, `excluded_keyword`, `company`,
`location` (hybrid or on-site outside the target metros), `salary` (listed salaries only,
never Adzuna's estimates), `clearance` (only with `mode: exclude_active`), `duplicate`.

**Notes** (kept, but flagged): `experience_unknown`, `remote_unconfirmed`, `salary_estimated`,
`suspicious` (or a rejection, with `suspicious.mode: exclude`).

**Suspicious postings** are marked for a closer look, not proven fraudulent:

- `mass_template`: one company posts the same text with only the job title swapped in,
  across at least `min_template_titles` (5) different titles, plus that company's other
  postings reusing the template. Company boilerplate ("About us…") is normal for big
  employers and doesn't count; on stored data, a plain "same opening text" check would
  have flagged several large employers.
- `scam_phrase`: upfront or training fees, gift cards, check deposits, interviews over
  WhatsApp or Telegram, and French equivalents.

Clearance and French are tagged, not filtered: ranking (milestone 3) decides how much they
count. All role families (dev, QA, support, implementation, training) are included equally;
none is boosted.

**Employment type** comes from the posting text first ("Full-time", "Part-Time", "temps
plein", "temps partiel"), then Adzuna's `contract_time` field, which is missing for most
postings and was wrong on at least one. "Contract with possible conversion to full-time"
doesn't count as full-time. `jm list` shows full-time, part-time, either, and not-stated
jobs as separate sections.

## Limitations

- Adzuna returns only a ~500-character description snippet ending in "…". Requirements
  and work mode stated later in a posting are invisible here.
- Search terms match job titles by default (`title_only: true`). With `--full-text`,
  Adzuna matches words anywhere in the full posting, which we can't see: "junior developer"
  in Orlando returned 28 jobs, only 1 of them junior. Title-only returned just that one.
  Broad terms like "software engineer" still return every seniority level ("Senior",
  "Software Engineer I"); seniority filtering happens locally (milestone 2).
- The remote keywords ("remote", "télétravail") are always matched against the full
  posting, so remote searches can return jobs whose snippet never mentions remote work.
- Adzuna has no remote filter. Remote searches add "remote"/"télétravail" as keywords.
- Some regions are high-volume: one page of remote US results covers well under an hour
  of new postings.
- Filters only see the title and snippet. Experience requirements are usually past the
  snippet's end, so most jobs pass as `experience_unknown`.
- Ambiguous titles ("Systems Engineer", "Integration Engineer", "Platform Infrastructure
  Engineer") don't match a role family and are rejected; add terms to `role_families` to
  keep them. Roman-numeral II ("Software Engineer II") is kept; III and above are rejected.

## Development

```bash
uv run pytest
uv run ruff check .
```

`tests/fixtures/adzuna_search_page.json` holds invented results in Adzuna's response format.

Job listings come from the [Adzuna API](https://www.adzuna.com).
