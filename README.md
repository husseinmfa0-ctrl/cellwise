# Backend — Cellwise API

FastAPI service that cleans messy CRM spreadsheets into HubSpot-ready contact
records using Gemini 1.5 Flash, with real DNS-based email validation on top.

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and set GEMINI_API_KEY=your_key
```

## Run

```bash
uvicorn main:app --reload --port 8000
```

Visit http://localhost:8000/docs for interactive API docs.

## Endpoints

- `GET /api/health` — health check.
- `POST /api/clean-stream` — multipart file upload (`file` field, .csv/.xlsx/.xls,
  10MB max). Returns a **Server-Sent Events** stream the frontend's "AI
  Operations Terminal" consumes live:
  - `event: meta` — `{ filename, row_count, original_preview }`
  - `event: log` — `{ message }`, one or more per batch, each describing real
    output from that batch (header mappings found, phones fixed, emails
    flagged) — not scripted flavor text.
  - `event: batch_done` — `{ batch, of }`
  - `event: result` — `{ cleaned, header_mapping, stats }` (final payload)
  - `event: error` — `{ detail }`
- `POST /api/clean` — same cleaning pipeline, but returns the full JSON payload
  in one response instead of streaming (useful for testing / non-browser
  clients). Returns `{ filename, row_count, original_preview, cleaned,
  header_mapping, stats }`.
- `POST /api/export-csv` — body `{ "rows": [...] }` (the `cleaned` array).
  Strips internal fields (`_badges`, `Mx_Valid`) before generating the
  downloadable CSV, so the export only contains real HubSpot columns.

## How cleaning + validation actually works (`cleaner.py`)

1. **Batching** — large files are split into batches of 150 rows before being
   sent to Gemini (`ROWS_PER_BATCH`), to keep responses fast and JSON parsing
   reliable.
2. **Header mapping** — Gemini returns a JSON object per batch:
   `{"header_mapping": {...}, "contacts": [...]}`, not a bare array. This is
   what powers the genuine "N headers auto-mapped" stat and the terminal's
   header-mapping log lines — they're reporting the model's actual output,
   not synthesized copy.
3. **Phone / name badges** — derived from the shape of the model's own output
   (e.g. does this field match E.164?) rather than a row-by-row diff against
   the original sheet, since batched LLM output order isn't guaranteed to
   align 1:1 with input order. No badge is shown that isn't backed by
   something inspectable in the cleaned record.
4. **Email validation is two-stage, and the second stage is real**:
   - Gemini flags obviously malformed emails (format only).
   - This backend then does an **actual DNS MX-record lookup** (`dnspython`)
     against each surviving domain, cached per request. A model can say an
     email "looks valid"; only a live MX lookup can say the domain can
     actually receive mail. `Is_Valid_Email` in the final output reflects
     both checks; `Mx_Valid` is available separately if you want it.
5. **Stats** (`total_rows`, `headers_mapped`, `phone_numbers_fixed`,
   `invalid_emails_caught`, `names_split`, `estimated_hours_saved`) are
   computed from the real per-batch results, not estimated after the fact.
   `estimated_hours_saved` uses a disclosed assumption
   (`ASSUMED_SECONDS_SAVED_PER_ROW`, default 40s/row) — tune it in
   `cleaner.py` if it doesn't match your team's real manual-cleanup pace.

Swap `MODEL_NAME` in `cleaner.py` if you want to test a different Gemini
model.

## Known limits

- No auth / multi-tenant support yet — this is a single-user API (see
  `DEPLOYMENT.md` in the repo root for how it's meant to be deployed today).
- No persistent storage — files are processed in-memory/temp and discarded
  after the response completes.
- No retry/backoff on Gemini rate limits yet.
- MX lookups have a 2.5s timeout per unique domain; a batch with many unique
  slow-to-resolve domains will take a little longer than one with a handful
  of common domains (gmail.com, outlook.com, etc. resolve near-instantly and
  are cached after the first hit).
