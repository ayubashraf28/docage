-- EC Agent: core schema
-- Stores every Encumbrance Certificate (EC) entry as structured rows, so
-- queries by plot / survey / party are exact (no chunking, nothing lost).
-- Works on Supabase or any Postgres 14+.

create extension if not exists pg_trgm;   -- fuzzy name search
create extension if not exists pgcrypto;  -- gen_random_uuid()

-- One row per uploaded file -------------------------------------------------
create table if not exists documents (
    id               uuid primary key default gen_random_uuid(),
    owner_id         uuid,                       -- Supabase auth user (see 002_supabase_rls.sql)
    file_name        text not null,
    file_sha256      text not null,              -- detects re-uploads of the same file
    storage_path     text,                       -- path in Supabase Storage, if used
    doc_type         text not null default 'encumbrance_certificate',
    statement_no     text,
    application_no   text,
    village          text,
    survey_searched  text,                       -- the survey number the EC was requested for
    period_from      date,
    period_to        date,
    page_count       int,
    declared_total   int,                        -- "411 out of 411" from the EC footer
    parsed_total     int,                        -- registered entries we actually parsed
    parsed_pending   int,
    status           text not null default 'parsed'
                     check (status in ('uploaded','parsing','parsed','count_mismatch','failed')),
    uploaded_at      timestamptz not null default now(),
    unique (owner_id, file_sha256)
);

-- One row per EC transaction ------------------------------------------------
create table if not exists entries (
    id                  bigint generated always as identity primary key,
    document_id         uuid not null references documents(id) on delete cascade,
    sl_no               text not null,           -- as printed, e.g. '117/411' or '1/1'
    is_pending          boolean not null default false,
    page_no             int,                     -- page in the PDF, for verification
    reg_date            date,                    -- registration (or pending) date
    exec_date           date,
    pres_date           date,
    deed_code           text,                    -- IGRS code, e.g. '0101'
    deed_type           text,                    -- e.g. 'Sale Deed'
    deed_category       text,                    -- normalised: sale, mortgage, release, gpa, gift, ...
    market_value        numeric(16,2),
    consideration_value numeric(16,2),
    doc_no              text,                    -- '17045/2026'
    doc_number          int,
    doc_year            int,
    sro                 text,
    village             text,
    colony              text,
    layout_key          text,                    -- normalised village/colony used to group layouts
    survey_text         text,
    plot_text           text,
    extent_text         text,
    extent_value        numeric(14,3),
    extent_unit         text,                    -- sq_yds, guntas, acres, sq_m
    extent_sq_yds       numeric(14,3),           -- converted, for comparison
    boundaries          text,
    link_doc            text,                    -- 'Link Doct' (previous title document)
    pending_reason      text,
    raw_description     text not null,           -- full original cell text, never lose data
    raw_parties         text not null,
    raw_doc_ref         text,
    unique (document_id, sl_no, is_pending)
);

-- Normalised lookups: one entry can cover several plots / survey numbers ----
create table if not exists entry_plots (
    entry_id  bigint not null references entries(id) on delete cascade,
    plot_no   text not null,                     -- '117', '116A'
    plot_part text,                              -- 'P/A', 'WEST/P', 'P-C' (sub-division as written)
    primary key (entry_id, plot_no, plot_part)
);

create table if not exists entry_surveys (
    entry_id     bigint not null references entries(id) on delete cascade,
    survey_no    int not null,                   -- base number, e.g. 33
    sub_division text not null default '',       -- '^C/^A', 'P', ...
    primary key (entry_id, survey_no, sub_division)
);

create table if not exists parties (
    id              bigint generated always as identity primary key,
    entry_id        bigint not null references entries(id) on delete cascade,
    position        int not null,                -- order printed in the EC
    role            text not null,               -- EX, CL, MR, ME, DR, DE, RR, RE, LR, LE ...
    side            text not null check (side in ('executant','claimant')),
    name            text not null,               -- as printed
    name_normalized text not null,               -- upper case, punctuation stripped
    represented_by  text
);

-- Indexes for the queries the app runs -----------------------------------------
create index if not exists entries_document_idx  on entries (document_id);
create index if not exists entries_reg_date_idx  on entries (reg_date);
create index if not exists entries_doc_no_idx    on entries (doc_no);
create index if not exists entries_link_doc_idx  on entries (link_doc);
create index if not exists entries_layout_idx    on entries (layout_key);
create index if not exists entries_category_idx  on entries (deed_category);
create index if not exists entry_plots_plot_idx  on entry_plots (plot_no);
create index if not exists entry_surveys_no_idx  on entry_surveys (survey_no);
create index if not exists parties_entry_idx     on parties (entry_id);
create index if not exists parties_name_trgm_idx on parties using gin (name_normalized gin_trgm_ops);

-- One wide view the report generator can read -----------------------------------
create or replace view entries_full with (security_invoker = true) as
select
    e.*,
    d.statement_no,
    d.file_name,
    (select string_agg(p.name, '; ' order by p.position) from parties p
      where p.entry_id = e.id and p.side = 'executant') as executants,
    (select string_agg(p.name, '; ' order by p.position) from parties p
      where p.entry_id = e.id and p.side = 'claimant')  as claimants,
    (select string_agg(distinct ep.plot_no, ', ') from entry_plots ep where ep.entry_id = e.id) as plots,
    (select string_agg(distinct es.survey_no::text, ', ') from entry_surveys es where es.entry_id = e.id) as surveys
from entries e
join documents d on d.id = e.document_id;

-- Search: every filter is optional; results in date order ---------------------
-- The LLM (or a search form) only fills these parameters; SQL returns exact rows.
create or replace function search_entries(
    p_plot          text    default null,
    p_survey        int     default null,
    p_party         text    default null,   -- fuzzy: 'gunti bhavani', 'IDBI'
    p_layout        text    default null,   -- e.g. 'Sahebguda'
    p_deed_category text    default null,   -- sale, mortgage, release, gpa, ...
    p_from          date    default null,
    p_to            date    default null,
    p_document_id   uuid    default null
)
returns setof entries_full
language sql stable security invoker
as $$
    select f.*
    from entries_full f
    where (p_document_id   is null or f.document_id = p_document_id)
      and (p_plot          is null or exists (select 1 from entry_plots ep
                                              where ep.entry_id = f.id and ep.plot_no = upper(p_plot)))
      and (p_survey        is null or exists (select 1 from entry_surveys es
                                              where es.entry_id = f.id and es.survey_no = p_survey))
      and (p_party         is null or exists (select 1 from parties p
                                              where p.entry_id = f.id
                                                and (p.name_normalized like '%' || upper(p_party) || '%'
                                                     or similarity(p.name_normalized, upper(p_party)) > 0.45)))
      and (p_layout        is null or f.layout_key ilike '%' || p_layout || '%')
      and (p_deed_category is null or f.deed_category = p_deed_category)
      and (p_from          is null or f.reg_date >= p_from)
      and (p_to            is null or f.reg_date <= p_to)
    order by f.layout_key, f.reg_date, f.id;
$$;
