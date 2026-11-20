-- Supabase only: run after 001 and 002.
-- Explicit API grants (project created with "auto-grant API access" off)
-- plus fixes for the Supabase security advisor warnings.

-- API access: signed-in users can read their own data (RLS filters rows)
grant usage on schema public to authenticated;
grant select on documents, entries, entry_plots, entry_surveys, parties, entries_full to authenticated;

-- Keep extensions out of the exposed public schema
create schema if not exists extensions;
alter extension pg_trgm set schema extensions;

-- Fixed search_path so the function can't be hijacked, and it can still find pg_trgm
alter function public.search_entries(text, int, text, text, text, date, date, uuid)
  set search_path = public, extensions;

-- Only signed-in users (and the backend) can call search
revoke execute on function public.search_entries(text, int, text, text, text, date, date, uuid) from public, anon;
grant execute on function public.search_entries(text, int, text, text, text, date, date, uuid) to authenticated, service_role;

-- Supabase's auto-RLS helper should not be callable through the API
-- (exists only if "Enable RLS on new tables automatically" was ticked)
do $$ begin
  if exists (select 1 from pg_proc where proname = 'rls_auto_enable') then
    revoke execute on function public.rls_auto_enable() from public, anon, authenticated;
  end if;
end $$;
