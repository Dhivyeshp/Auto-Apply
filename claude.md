# AutoApply: Tailored Resume + Job Application Agent

## Goal
For each job posting: pull the JD, generate a tailored 1-page resume from a master bullet bank, then autofill and submit the application after human approval. Uses Claude Code (Pro plan, headless mode) for all LLM work. No separate API billing.

## Core Rules (non-negotiable)
- Never invent skills, tools, metrics, titles, or dates. Only select and rephrase real bullets.
- Resume must be exactly 1 page.
- Human approves every submission. No blind auto-submit.
- Log every application with the exact resume version sent.
- Respect site ToS. No LinkedIn Easy Apply automation.

## Discovery (Phase 1: SimplifyJobs repos)
Source: SimplifyJobs + Pitt CSC GitHub repos, updated by the community plus hourly Simplify scrapes.
- `SimplifyJobs/Summer2027-Internships` (main README = Summer 2027)
- Same repo's Off-Season 2027 README (fall/winter/spring co-ops)

**Don't parse the README.** Pull the raw JSON instead:
`https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json`
(verify path + branch on first run, it's moved before)

**Daily job (`discover/simplify.py`):**
1. Fetch `listings.json`
2. Drop: inactive/hidden, closed, advanced-degree-only, non-US (unless remote)
3. Filter by `config.yaml`:
   - categories: SWE, DS/AI/ML, hardware (toggle PM, quant)
   - terms: Summer 2027, Spring 2027, Fall 2027
   - title include: intern, co-op; exclude: PhD, MBA, senior
   - locations: any / prefer TX, remote
   - sponsorship flags, citizenship-required flags
4. Dedupe by listing id + URL against `jobs` table
5. New matches → `jobs` table, status `queued`
6. Optional: ping (email/Discord) with daily new count

**Notes**
- Inspect the actual schema first (`jq '.[0]'`), field names may differ from what's assumed here
- Store raw listing JSON per job so nothing's lost if the schema changes
- Many SWE roles close in days, so run discovery at least daily, ideally every few hours via cron / GitHub Actions
- Detect ATS from apply URL domain (greenhouse.io, lever.co, ashbyhq.com, myworkdayjobs.com) and tag it

## Pipeline
1. **Ingest** - URL or job list → scrape JD text, company, role, location, ATS type
2. **Extract** - Claude pulls: must-have skills, nice-to-haves, keywords, role category (SWE / ML / FDE / data / PM-adjacent)
3. **Select** - score every bullet in `bullets.json` against JD, pick top ~12-15 across sections
4. **Rewrite** - light rephrase to mirror JD language. Keep facts identical
5. **Render** - fill `template.tex` (Jinja) → compile PDF
6. **Fit check** - if >1 page, drop lowest-scored bullet and re-render. Loop until 1 page
7. **ATS check** - re-parse PDF to text, compute keyword coverage %, flag if under threshold
8. **Apply** - Playwright fills form, uploads PDF, drafts answers to custom questions
9. **Review gate** - show: resume diff vs base, coverage score, form screenshot, drafted answers. Approve / edit / skip
10. **Submit + log** - submit, save confirmation screenshot, write row to DB

## Repo Structure
```
autoapply/
├── CLAUDE.md              # this spec, so Claude Code has context
├── data/
│   ├── profile.json       # contact, edu, links, work auth, EEO, start date
│   ├── bullets.json       # master bullet bank
│   └── answers.json       # reusable answers (why us, sponsorship, relocation...)
├── templates/
│   └── resume.tex         # Jinja-templated LaTeX skeleton
├── prompts/
│   ├── extract.md
│   ├── select.md
│   └── rewrite.md
├── config.yaml            # discovery filters (terms, categories, keywords, locations)
├── src/
│   ├── discover/
│   │   └── simplify.py    # pull SimplifyJobs listings.json, filter, queue
│   ├── ingest.py          # scrape JD, detect ATS
│   ├── tailor.py          # calls `claude -p` for extract/select/rewrite
│   ├── render.py          # Jinja → LaTeX → PDF, 1-page loop
│   ├── ats_check.py       # PDF → text, keyword coverage
│   ├── apply/
│   │   ├── greenhouse.py
│   │   ├── lever.py
│   │   └── ashby.py
│   ├── review.py          # CLI or tiny web UI approval gate
│   └── tracker.py         # SQLite logging
├── output/
│   └── {company}_{role}_{date}/
│       ├── resume.pdf
│       ├── jd.txt
│       ├── tailoring.json
│       └── confirmation.png
└── main.py
```

## bullets.json Schema
```json
{
  "experience": [
    {
      "id": "ibm",
      "org": "IBM",
      "title": "Software Development Engineer Intern",
      "team": "watsonx.ai Data Refinery",
      "location": "Austin, TX",
      "dates": "Summer 2026",
      "bullets": [
        {
          "id": "ibm-1",
          "text": "Built ...",
          "tags": ["backend", "python", "data pipelines"],
          "tech": ["Python", "Kubernetes"],
          "has_metric": true,
          "priority": 1
        }
      ]
    }
  ],
  "projects": [],
  "leadership": [],
  "skills": {
    "languages": [],
    "frameworks": [],
    "cloud_tools": []
  }
}
```
- Write 2-3 variants per bullet if useful (e.g. backend-flavored vs ML-flavored). Selection picks one.
- `priority` 1 = always include if any relevance.

## Claude Code Calls
Headless, JSON out:
```bash
claude -p "$(cat prompts/extract.md)" --output-format json < jd.txt
```
- Each step returns strict JSON. Validate with pydantic, retry once on bad output.
- `rewrite` prompt gets original bullet + JD keywords. Output must be checked: any new number, tool, or proper noun not in the original → reject and keep original.

## Apply Layer
- Phase 1 ATS support: Greenhouse, Lever, Ashby (clean forms)
- Phase 2 maybe: Workday (account per company, multi-page, flaky)
- Field mapping: label text → profile.json key via rules first, Claude fallback for weird fields
- Custom questions: draft from `answers.json` + JD, always shown in review
- On CAPTCHA: pause, hand to user

## Tracker (SQLite)
| col | type |
|---|---|
| id | int |
| company | text |
| role | text |
| url | text |
| ats | text |
| date_applied | date |
| resume_path | text |
| coverage_pct | real |
| status | text (drafted / submitted / skipped / interview / rejected / offer) |
| notes | text |

## Build Order
1. Tracker (SQLite `jobs` table) + `discover/simplify.py` + `config.yaml`. Run it, eyeball matches
2. `profile.json` + `bullets.json` from current resume
3. `resume.tex` template + `render.py` (1-page loop working)
4. `tailor.py` with Claude Code headless calls
5. `ats_check.py`
6. Review CLI: show diff + score, approve to save PDF
7. Greenhouse apply bot
8. Lever + Ashby
9. Batch mode: everything `queued` flows through tailor → review → apply
10. Schedule discovery (cron or GitHub Actions)

## Stack
Python 3.11+, Claude Code (headless), Jinja2, LaTeX (tectonic or pdflatex), pdfplumber, Playwright, pydantic, SQLite, rich (CLI review)

## Later Ideas
- Cover letter generation (same no-invent rules)
- Discovery phase 2: poll Greenhouse/Lever/Ashby public board APIs for a custom `companies.json`
- Discovery phase 3: parse job alert emails from Gmail
- Simple web dashboard for the tracker
- Referral finder: flag companies where you have contacts
