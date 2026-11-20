# EC Agent

Parses Encumbrance Certificate (EC) PDFs from the Telangana IGRS portal, stores every entry as structured data in Postgres (Supabase), and answers questions like "all transactions for Plot 117" or "everything involving IDBI Bank" with exact results and a Word report.

## Files

| File | What it does |
|---|---|
| `db/001_schema.sql` | Tables, indexes, `entries_full` view and `search_entries()` function |
| `db/002_supabase_rls.sql` | Supabase only: each user sees only their own documents |
| `db/003_grants_and_hardening.sql` | Supabase only: API grants and security advisor fixes |
| `ec_tool.py` | PDF parser + Word report generator (also works standalone) |
| `load_ec.py` | Parses a PDF and loads it into the database (also a CLI) |
| `main.py` | FastAPI backend: upload, documents, search, Word report |
| `static/index.html` | Web page: login, upload, search, download |

## Data model

```
documents 1──* entries 1──* entry_plots    (plot_no, plot_part)
                       1──* entry_surveys  (survey_no, sub_division)
                       1──* parties        (role, side, name, represented_by)
```

One EC entry can cover several plots and survey numbers, so those live in their own tables. That makes "Plot 117" an exact lookup instead of a text search. The original cell text is kept in `raw_*` columns so nothing from the PDF is ever lost, and `page_no` + `sl_no` let every answer point back to the source.

## Setup (Supabase)

1. Create a Supabase project.
2. In the SQL editor, run `db/001_schema.sql`, `db/002_supabase_rls.sql`, then `db/003_grants_and_hardening.sql`.
3. Copy the connection string from Project Settings → Database.
4. Load an EC:

```bash
pip install pdfplumber "psycopg[binary]" python-docx
export DATABASE_URL="postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres"
python load_ec.py EC_FOR_SY.NO.33.pdf --owner <your-auth-user-id>
```

Loading the same file again replaces it, so you can improve the parser and reload safely.

## Run the website locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
# edit .env: paste the Session pooler connection string (Supabase -> Connect)
uvicorn main:app --reload
```

Open http://localhost:8000 and log in with a user created in Supabase (Authentication -> Users -> Add user, tick "Auto confirm").

API docs are generated automatically at http://localhost:8000/docs.

## Deploy (Render, free plan, Singapore)

1. Push this folder to a private GitHub repo (`.env` is git-ignored, never commit it).
2. Render: **New > Blueprint**, pick the repo. `render.yaml` sets up the service in Singapore.
3. Paste your Session pooler connection string when asked for `DATABASE_URL`.
4. Supabase > Authentication > URL Configuration: set **Site URL** to your Render URL and add it under **Redirect URLs** (needed for "Forgot password?" emails).

Free plan note: the service sleeps after 15 minutes without visits, so the first visit after that takes about a minute to wake up. The paid Starter plan keeps it awake.

Never set `AUTH_DEV_USER` on a deployed server; the app refuses to start if you do.

## Querying

Every filter is optional:

```sql
select * from search_entries(p_plot => '117');
select * from search_entries(p_party => 'gunti bhavani');          -- fuzzy name match
select * from search_entries(p_survey => 26, p_deed_category => 'sale', p_from => '2026-01-01');
select * from search_entries(p_plot => '117', p_layout => 'Sahebguda');
```

From the app, call it with `supabase.rpc('search_entries', { p_plot: '117' })`.

`deed_category` values: sale, mortgage, release, gpa, gift, settlement, rectification, revocation, lease, supplemental, agreement, development, partition, cancellation, other.

## Tested on

EC Statement No. 240589688 (Mangalpalle, Sy. No. 33, 326 pages): 411/411 registered entries + 1 pending parsed and loaded in about 25 seconds.

## Roadmap

- [x] Parser + Word report
- [x] Database schema + loader
- [x] FastAPI backend: upload → parse → load, search, Word report
- [x] Web page: login, upload, search, download
- [ ] Keep original PDFs in Supabase Storage
- [ ] Question → `search_entries` parameters via LLM tool calling
- [x] Deploy config (Render, Singapore)
- [ ] OCR for scanned ECs; pgvector for free-text documents (sale deeds, orders)
