# job-matcher

Pulls software job postings (English and French searches, plus companies' own job boards)
for Montreal, Ottawa, Toronto, Florida, Texas, and remote Canada/US roles into SQLite, tags
and filters them against a bilingual profile, and scores the survivors against your CV with
a free, local keyword scorer.

## Costs

| Part | Cost |
|---|---|
| Filtering, tagging, scoring (everything local) | Free |
| Board discovery (Common Crawl index) | Free |
| Company boards (Ashby, Greenhouse APIs) | Free, no key |
| Adzuna, trial key | Free, 25 calls total |
| Adzuna, regular use | API Access plan, £8/month (500 calls/day, 5,000/month) |
| LLM scoring (optional, not built) | A local model via Ollama is free; the Claude API needs prepaid credits |

## Setup

Requires [uv](https://docs.astral.sh/uv/). From the project root:

```bash
uv sync
```

Copy `.env.example` to `.env` in the project root and add your Adzuna `app_id` and `app_key`
(developer.adzuna.com → Dashboard → API Access Details). `.env` is git-ignored.

`config/` holds example settings. To use your own, copy `config/profile.example.yaml` to
`private/profile.yaml` and `config/searches.example.yaml` to `private/searches.yaml`, and
edit those. `private/` is git-ignored, and the app uses a file there when it exists.

## Usage

```bash
uv run jm fetch --plan                          # list searches and request budget, call nothing
uv run jm fetch -r orlando --limit 3            # one region, at most 3 API calls
uv run jm fetch -r remote_ca -t "développeur junior"   # one exact term from the config
uv run jm fetch                                 # every enabled region
uv run jm filter                                # tag and filter every stored job
uv run jm cv                                    # what's extracted from your CV
uv run jm score                                 # score passed jobs against your CV
uv run jm list                                  # passed jobs by score, full-/part-time
uv run jm label                                 # write private/labels.csv for you to label
uv run jm eval                                  # compare your labels with the scorer
uv run jm list --employment part_time           # one employment type only
uv run jm list --rejected                       # ...or were rejected, with reasons
uv run jm list --reason seniority --limit 20    # rejected for one reason
uv run jm show 98                               # one job: text, tags + evidence, verdict
uv run jm stats                                 # what's in the database
```

| Option | Effect |
|---|---|
| `-r, --region` | Only these regions (repeatable). |
| `-t, --term` | Only these exact terms from the searches config (repeatable). |
| `--limit N` | Stop after N API calls in this run. |
| `--title-only / --full-text` | Match terms in job titles (config default) or anywhere in the posting. |
| `--max-pages N` | Pages per search (50 results each). |
| `--all-pages` | Don't stop a search at the first page with nothing new. |
| `--plan` | Show what would run, without calling the API. |

Searches, term lists, and limits live in the searches config. The database is `data/jobs.db`
(override with `JM_DB_PATH`).

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

- **Metro**: `montreal` (including Laval, Longueuil, and other nearby cities), `ottawa`
  (including Gatineau), `toronto` (the GTA); `orlando`, `tampa`, `miami`, or `florida_other`;
  `austin`, `dallas` (Dallas–Fort Worth), `houston`, `san_antonio`, or `texas_other`. Read
  from the source's country › state › county › city data, or its location text.
- **Work mode**: `remote`, `hybrid`, `onsite`, or unknown, read from the title and snippet in
  English and French. Unknown means the snippet doesn't say, not that the job is on-site.
- **Likely duplicates**: same title, company, and city are flagged with `duplicate_of`,
  not merged.

## Filtering (`jm filter`)

The profile config holds the rules. Matching ignores case and accents, works on whole
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
`location` (hybrid or on-site outside the target metros, or anywhere outside the
`countries` setting, `[us, ca]` by default), `salary` (listed salaries only,
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

## Scoring (`jm score`)

Put your CV at `private/cv.pdf` (or `.docx`, `.md`, `.txt`); `private/` is git-ignored.
`jm cv` shows its sections and the skills recognized, with where each appears (Skills,
Projects, Experience). Skills come from `config/skills.yaml`; add any it misses.

Scores are 0-100, rebuilt on every run, and explained line by line in `jm show <id>`:

- **Skills (0-60, neutral 30):** the share of the job's named skills that are in your CV,
  with related skills counting half (React/Vue toward Angular, Java toward Kotlin; see
  `related_skills`). The swing from neutral is scaled by n / (n + 2) for n named skills,
  because snippets often name only one or two: a single named skill moves it at most 10.
- **Level (10-30):** entry-level title 30, 0-1 years 26, entry-level wording 22, 2 years 20,
  unstated 15, stretch title ("II") 10.
- **Adjustments** (`ranking` in the profile config): French required +8 / asset +4,
  a bonus per metro, remote -3 (a much larger applicant pool, so remote roles rank slightly
  below the same role in a target area), clearance (obtainable 0, unclear -5, active -15,
  polygraph -5 more), suspicious -15. Role families are not boosted; related experience from
  your CV (e.g. tutoring for training roles) is shown but not scored.

Bands: 70+ strong, 50-69 possible, under 50 weak. The weights are first guesses, to be
checked against your labels (next section).

## Evaluating the scorer (`jm label`, `jm eval`)

`jm label` writes `private/labels.csv` (git-ignored) with 15 scored jobs: spread across the
score range, at most 2 per company, shuffled, and **without scores**, so they can't anchor
you. It never overwrites an existing file unless you pass `--force`.

Fill in, judging from the same title and snippet the scorer sees:

- `label`: **2** apply, **1** maybe (a stretch, or not enough information), **0** skip
  (words `apply`/`maybe`/`skip` work too)
- `reason`: a few words ("too senior", "no Java", "not Orlando", "looks templated")
- `opened_full_posting`: `yes` if you clicked through to the full posting

`jm eval` compares your labels with the whole pipeline: a job the filters rejected counts as
the pipeline saying "skip" (its own "filtered out" column), so correct rejections stay in the
comparison and a wrongly rejected job you'd apply to shows up as a disagreement. It reports
band agreement, Spearman rank correlation (1 = same order as yours, 0 = unrelated; rejected
jobs rank below every scored one), the mean score for each of your labels (scored jobs only;
it should fall from apply to skip), a label-vs-verdict grid, and every disagreement with your
reason next to the pipeline's explanation. Each disagreement is either a scorer mistake (fix a rule or weight) or a
personal preference the profile doesn't capture yet. The `Scorer` interface (`scoring/base.py`) lets a local
LLM or API scorer replace the keyword scorer later.

## Board discovery (in progress)

`jm discover` finds Greenhouse and Ashby job boards in Common Crawl's URL index, so the
tool can read companies' public board APIs without a hand-kept company list. It's free, but
slow: the index is heavily rate-limited and often times out, so a full run can take hours.
Boards are saved to the database for `jm fetch-boards` to read. Lever's robots.txt
blocks Common Crawl's crawler, so Lever boards can't be discovered this way.

```bash
uv run jm discover --ats ashby --limit 3   # a small first run
uv run jm discover                         # the newest crawl, both systems
```

| Option | Effect |
|---|---|
| `--crawls N` | How many of the newest crawls to read (default 1: one crawl alone can take days of resumed runs, since the index often times out). |
| `--ats NAME` | Only `greenhouse` or `ashby` (repeatable). Default: both. |
| `--limit N` | Stop after N finished queries in this run, across all crawls. |
| `--retry-pause S` | Seconds to wait before retrying failed queries (default 300). |

The index can cut a response short while still answering 200, so discovery uses small
per-character queries, reads each until two reads agree, and splits a prefix whose reads
never settle. Each finished query is saved at once, so a stopped or interrupted run (Ctrl+C)
resumes where it left off. A failed query is recorded and retried later, and 5 failures in a
row stop the run.

At the end, it checks boards known to exist (Ashby `onebrief`, Greenhouse `anthropic`):
`found`, `not checked yet` (the queries that would find it haven't been confirmed), or
`missing`, which means discovery is losing boards and the command exits with an error.

## Company boards (`jm fetch-boards`)

Reads every discovered board through the public Ashby and Greenhouse job-board APIs (free,
no key) and stores the jobs worth keeping, with their **full descriptions**. Then run
`jm filter` and `jm score` as usual.

```bash
uv run jm fetch-boards --limit 20          # a small first run
uv run jm fetch-boards --ats ashby         # one system only
uv run jm fetch-boards                     # every board that's due
```

| Option | Effect |
|---|---|
| `--ats NAME` | Only `greenhouse` or `ashby` (repeatable). Default: both. |
| `--limit N` | Stop after reading N boards. |
| `--refresh-days D` | Read a board again once D days have passed (default 7). |
| `-p, --profile` | The profile whose role families and metros decide what's kept. |

A board lists every job a company has, worldwide, so jobs are kept only with a
software-related title (any role family in the profile) **and** either a target metro or
remote work in a target country (`countries`) or an unstated one. Everything else is counted, not
stored; the full filters still run later in `jm filter`. On Greenhouse, descriptions are
downloaded only for kept jobs: a large board's descriptions can reach 10 MB, its plain job
list under 500 KB.

Locations are free text ("Orlando, FL", "Ontario, CAN", "Remote-Friendly, United States",
or several places in one string, split on `|`, `;`, `/` or a capitalized `OR`), read by
`places.py` into the same country › state › city shape Adzuna uses. Regions wholly outside
the US and Canada ("Europe", "APAC", "LATAM") count as a non-target country; ones that
include them ("North America", "Worldwide") leave the country unstated. A job with several
places is filed under the most relevant: a target metro first, then remote in the US or
Canada. The place is read again from the stored text on every `jm filter`, so parser fixes
reach jobs fetched earlier. Ashby states each job's work mode; Greenhouse
only says "remote" in its location text (its "Location Type" field is defined by each
company and proved unreliable), so otherwise the mode is read from the description.

Boards are read never-fetched first, then stalest first, with a pause before every request.
Each board's result is saved as it finishes, so a stopped run resumes. A board that no
longer exists (404, common for older crawl entries) is never tried again; one that failed
is tried on the next run, and 5 failures in a row stop the run.

## Limitations

- Adzuna returns only a ~500-character description snippet ending in "…". Requirements
  and work mode stated later in a posting are invisible here. Company boards don't have
  this limit.
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
- The CV reader treats any short line containing a heading word ("Skills", "Projects",
  "Tools", "Experience", ...) as a section heading, so an unusual line can be filed under
  the wrong section. That only changes the "found in" labels in `jm cv`, not scores.
- Keyword scoring only sees words. It can't tell that a project shows a skill unless the
  skill is named, and short snippets keep most scores near the middle (no job reached 70 on
  the first 35 scored).

## Development

```bash
uv run pytest
uv run ruff check .
```

`tests/fixtures/adzuna_search_page.json` holds invented results in Adzuna's response format.

Job listings come from the [Adzuna API](https://www.adzuna.com).

## License

[MIT](LICENSE)
