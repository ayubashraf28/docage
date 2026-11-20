-- Supabase only: each signed-in user sees only their own documents and entries.
-- Run after 001_schema.sql. The loader uses the database connection string
-- (service role), which bypasses RLS, so pass --owner <auth user id> when loading.

alter table documents      enable row level security;
alter table entries        enable row level security;
alter table entry_plots    enable row level security;
alter table entry_surveys  enable row level security;
alter table parties        enable row level security;

alter table documents alter column owner_id set default auth.uid();

create policy "own documents" on documents
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

create policy "own entries" on entries
    for all using (exists (select 1 from documents d where d.id = document_id and d.owner_id = auth.uid()));

create policy "own plots" on entry_plots
    for all using (exists (select 1 from entries e join documents d on d.id = e.document_id
                           where e.id = entry_id and d.owner_id = auth.uid()));

create policy "own surveys" on entry_surveys
    for all using (exists (select 1 from entries e join documents d on d.id = e.document_id
                           where e.id = entry_id and d.owner_id = auth.uid()));

create policy "own parties" on parties
    for all using (exists (select 1 from entries e join documents d on d.id = e.document_id
                           where e.id = entry_id and d.owner_id = auth.uid()));
