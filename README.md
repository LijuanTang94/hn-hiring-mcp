# HN Hiring MCP

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude Desktop, etc.) search the monthly **Hacker News "Who is hiring?"** threads for real job postings — filter by keywords and remote, get direct links.

Data comes from Hacker News' public [Algolia API](https://hn.algolia.com/api) — no scraping, no login, no ToS issues.

## Example

Ask Claude (or call the tool directly):

> "Find remote or US-based AI roles paying $150k+ in this month's HN hiring thread."

→ `search_jobs(roles="ai", location="remote, us", min_salary=150)` returns 29 of 330
postings, each enriched and with a direct apply link:

```jsonc
{
  "thread": "Ask HN: Who is hiring? (May 2026)",
  "matched": 29,
  "total_in_thread": 330,
  "jobs": [
    {
      "headline": "Loophole Labs | Senior Systems Engineer | REMOTE (Americas & Europe) | $150k–195k USD + equity ...",
      "level": ["senior"],
      "location": "REMOTE (Americas & Europe)",
      "stack": ["Go", "Rust", "Kubernetes"],
      "salary_k": { "min": 150, "max": 195 },
      "visa": false,
      "company_type": ["Seed"],
      "apply_url": "https://loopholelabs.io/careers",
      "url": "https://news.ycombinator.com/item?id=47975670"
    }
    // ... 28 more
  ]
}
```

## Tools

| Tool | What it does |
|------|--------------|
| `list_hiring_threads(limit=6)` | List the recent monthly "Who is hiring?" threads (id, title, date). |
| `search_jobs(keywords="", roles="", level="", location="", remote=False, visa=False, min_salary=0, company_type="", verbose=False, thread_id=None, limit=20)` | Search a month's postings. `keywords` (space-separated) must **all** match. `roles` (comma-separated, e.g. `"backend, full-stack"`) is **fuzzy OR** matching with synonyms and hyphen/space-insensitivity; known roles: backend, frontend, fullstack, ml, ai, data, devops, mobile, security, founding. `level` (intern/junior/mid/senior/staff/principal/lead) matches seniority in the headline **or** body, but excludes posts whose **title** is a higher tier (so "Senior SWE … mentors junior devs" won't show for `level="junior"`). `location` is **comma-separated OR**: `"remote, us"` = remote OR US-based; `"us"`/`"usa"` is precise (won't match "join us"); other terms (e.g. `"Berlin"`, `"Europe"`) are substring matches. `remote`/`visa` filter remote / visa-sponsoring roles. `min_salary` ($K) keeps roles whose parsed salary max ≥ that. `company_type` (e.g. `"Series A"`, `"Nonprofit"`, `"Public"`) filters by detected company type. Each result is auto-enriched with **level**, **location**, **stack**, **salary_k**, **visa**, **company_type**, and an **`apply_url`** (the company's apply/careers link, falling back to the HN post). Set `verbose=True` to also get an **`evidence`** dict. |
| `get_posting(job_id)` | Fetch ONE posting in **full** (the search `snippet` is truncated): returns `full_text`, `apply_url`, all `links`, plus location/stack/salary/visa/company_type. `job_id` is the number at the end of a posting's HN url. |
| `analyze_posting(text)` | Analyze any pasted JD: returns **location**, tech **stack**, **salary range ($K)**, **visa** sponsorship, **company_type** — funding stage (Seed / Series A–D+ / YC / Public / Bootstrapped / Profitable) **or** org nature (Nonprofit / Government / Academic / Agency) — and an **`evidence`** dict showing the snippet behind each heuristic so you can verify it. |
| `track_application(job_id, company="", status="applied", notes="")` | Record/update a job application (persisted to `applications.json`). `job_id` is the number at the end of a posting's HN url. |
| `list_applications(status=None)` | List tracked applications, optionally filtered by status. |

## Run locally

```bash
python -m venv .venv
./.venv/bin/pip install mcp httpx
./.venv/bin/python server.py        # starts the MCP server over stdio
```

Quick smoke test (without an MCP client):

```bash
./.venv/bin/python -c "import server; print(server.search_jobs(keywords='ai', remote=True, limit=3))"
```

## Use it in Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
(use **absolute paths**), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "hn-hiring": {
      "command": "/absolute/path/to/hn-hiring-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/hn-hiring-mcp/server.py"]
    }
  }
}
```

Then ask Claude things like:
> "Find remote AI/agent engineering jobs in this month's HN hiring thread."

## Notes on accuracy (heuristics)

Enrichment is **best-effort** parsing of free-text postings, validated against a full
month (330 postings):

- **stack** — reliable (word-boundary matching; `go` won't match `google`).
- **location** — extracted for ~87%; some free-text formats yield `null`, but the
  `location` filter matches the full posting text so filtering stays reliable.
- **salary_k** — only ~28% of postings list pay. Parser ignores the `401(k)` plan and
  non-salary numbers (`100k users`, `50k MRR`, ...).
- **company_type** — funding stage / org nature when stated; absent on many postings.
- **visa** — means the posting **explicitly states** sponsorship; it does *not* imply
  others won't sponsor.

## Why this exists

HN's monthly "Who is hiring?" is one of the best places to find roles at serious
engineering teams — but it's a single thread of **hundreds of free-text comments with
no search and no filters**. Finding the few postings that actually fit you (your stack,
remote, salary, visa, seniority) means scrolling for ages and reading every one.

This server turns that thread into a **structured, queryable dataset** so a job seeker
(or their agent) can pull exactly the roles that match in seconds — with the stack,
salary, location, and apply link already extracted.
