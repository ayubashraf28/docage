#!/usr/bin/env python3
"""
Load an Encumbrance Certificate PDF into Postgres / Supabase.

  export DATABASE_URL="postgresql://postgres:<password>@db.<project>.supabase.co:5432/postgres"
  python load_ec.py EC.pdf [--owner <supabase-user-uuid>] [--storage-path ecs/EC.pdf]

Re-loading the same file replaces its previous rows (matched by SHA-256),
so you can improve the parser and reload safely.
"""
import argparse, hashlib, os, re, sys, datetime
import psycopg
from ec_tool import parse_pdf, layout_key, survey_bases, EXEC_ROLES

SQ_YDS_PER = {"sq_yds": 1, "guntas": 121, "acres": 4840, "sq_m": 1.19599}


# ---------------------------------------------------------------- normalisers
def to_date(s):
    try:
        return datetime.datetime.strptime(s, "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return None


def deed_category(deed):
    d = deed.upper()
    rules = [("RELEASE", "release"), ("RECONVEYANCE", "release"), ("RECTIFICATION", "rectification"),
             ("CANCELLATION", "cancellation"), ("REVOCATION", "revocation"), ("GPA", "gpa"),
             ("POWER OF ATTORNEY", "gpa"), ("DEPOSIT OF TITLE", "mortgage"), ("DOTD", "mortgage"),
             ("MORTGAGE", "mortgage"), ("GIFT", "gift"), ("SETTLEMENT", "settlement"),
             ("PARTITION", "partition"), ("LEASE", "lease"), ("DEVELOPMENT", "development"),
             ("AGREEMENT OF SALE", "agreement"), ("SUPPLEMENTAL", "supplemental"), ("SALE", "sale")]
    for key, cat in rules:
        if key in d:
            return cat
    return "other"


def split_plots(plot_txt):
    """'1/ WEST/P 117/P-C' -> [('1','WEST/P'), ('117','P-C')]; '116 117' -> [('116',''), ('117','')]"""
    out = []
    t = re.sub(r"/\s+", "/", plot_txt or "").replace(",", " ")
    for tok in t.split():
        m = re.match(r"(\d+[A-Z]?)(?:/(.*))?$", tok.upper())
        if m:
            out.append((m.group(1), (m.group(2) or "").strip("/")))
        elif out and re.match(r"[A-Z]", tok.upper()):     # e.g. 'P/A' written as a separate token
            no, part = out[-1]
            out[-1] = (no, (part + "/" + tok.upper()).strip("/"))
    return list(dict.fromkeys(out))


def split_surveys(survey_txt):
    """'22/ ^B/1/1 33^C/^A 33/P' -> [(22,'^B/1/1'), (33,'^C/^A'), (33,'P')]"""
    out = []
    t = re.sub(r"/\s+", "/", survey_txt or "").replace(",", " ")
    for tok in t.split():
        m = re.match(r"(\d+)(.*)$", tok)
        if m:
            out.append((int(m.group(1)), m.group(2).strip("/")))
    return list(dict.fromkeys(out))


def parse_extent(txt):
    m = re.match(r"([\d.]+)\s*(.*)", (txt or "").strip())
    if not m:
        return None, None, None
    try:
        val = float(m.group(1).rstrip("."))
    except ValueError:
        return None, None, None
    u = m.group(2).upper().replace(" ", "")
    unit = ("guntas" if "GUNTA" in u else "acres" if u.startswith("AC") else
            "sq_m" if "MTR" in u or "SQ.M" in u else "sq_yds" if "YD" in u else None)
    return val, unit, round(val * SQ_YDS_PER[unit], 3) if unit else None


def split_rep(name):
    m = re.split(r"\s+(?:REPRESENTED BY|REP\.? BY)\s*:?-?\s*", name, maxsplit=1, flags=re.I)
    return m[0].strip(), (m[1].strip() if len(m) > 1 else None)


def norm_name(s):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", s.upper())).strip()


def village_colony(vw):
    v = re.sub(r"\s*W-\s*B:.*$", "", vw or "")
    parts = [p.strip() for p in v.split("/") if p.strip()]
    return (parts[0] if parts else None), (parts[1] if len(parts) > 1 else None)


# ---------------------------------------------------------------- loading
def load(pdf_path, dsn, owner=None, storage_path=None):
    with open(pdf_path, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    data = parse_pdf(pdf_path)
    m = data["meta"]
    period = re.findall(r"\d\d-\d\d-\d{4}", m.get("period") or "")
    status = "parsed" if m["declared_total"] in (None, m["parsed_registered"]) else "count_mismatch"

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # replace a previous load of the same file
        cur.execute("delete from documents where file_sha256 = %s and owner_id is not distinct from %s", (sha, owner))
        cur.execute("""
            insert into documents (owner_id, file_name, file_sha256, storage_path, statement_no, application_no,
                                   village, survey_searched, period_from, period_to, page_count,
                                   declared_total, parsed_total, parsed_pending, status)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (owner, os.path.basename(pdf_path), sha, storage_path, m["statement_no"], m["application_no"],
             m["village"], m["survey"], to_date(period[0]) if period else None,
             to_date(period[1]) if len(period) > 1 else None, m["pages"],
             m["declared_total"], m["parsed_registered"], m["parsed_pending"], status))
        doc_id = cur.fetchone()[0]

        # Batched inserts: a handful of round trips instead of ~4 per entry,
        # which matters when the database is far away (UK -> Singapore ~ 170 ms each).
        entry_rows = []
        for e in data["entries"]:
            village, colony = village_colony(e["village_ward"])
            ev, eu, esq = parse_extent(e["extent"])
            dn = re.match(r"(\d+)/(\d{4})$", e["doc_no"] or "")
            entry_rows.append(
                (doc_id, e["sl"], e["pending"], e["page"], to_date(e["reg_date"]), to_date(e["exec_date"]),
                 to_date(e["pres_date"]), e["deed_code"], e["deed"], deed_category(e["deed"]),
                 e["mkt_value"], e["cons_value"], e["doc_no"],
                 int(dn.group(1)) if dn else None, int(dn.group(2)) if dn else None, e["sro"],
                 village, colony, layout_key(e), e["survey_txt"], e["plot_txt"], e["extent"],
                 ev, eu, esq, e["boundaries"], e["link_doc"] or None,
                 (e["pending_reason"] or "").strip() or None, e["description"], e["parties_raw"], e["doc_raw"]))
        cur.executemany("""
            insert into entries (document_id, sl_no, is_pending, page_no, reg_date, exec_date, pres_date,
                deed_code, deed_type, deed_category, market_value, consideration_value,
                doc_no, doc_number, doc_year, sro, village, colony, layout_key,
                survey_text, plot_text, extent_text, extent_value, extent_unit, extent_sq_yds,
                boundaries, link_doc, pending_reason, raw_description, raw_parties, raw_doc_ref)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            returning id""", entry_rows, returning=True)
        ids = []
        while True:  # one result set per inserted row, in order
            ids.append(cur.fetchone()[0])
            if not cur.nextset():
                break
        assert len(ids) == len(data["entries"]), "entry id count mismatch"

        plot_rows, survey_rows, party_rows = [], [], []
        for eid, e in zip(ids, data["entries"]):
            plot_rows += [(eid, no, part) for no, part in split_plots(e["plot_txt"])]
            survey_rows += [(eid, no, sub) for no, sub in split_surveys(e["survey_txt"])]
            for pos, p in enumerate(e["parties"], 1):
                name, rep = split_rep(p["name"])
                party_rows.append((eid, pos, p["role"], "executant" if p["role"] in EXEC_ROLES else "claimant",
                                   name, norm_name(name), rep))
        cur.executemany("insert into entry_plots (entry_id, plot_no, plot_part) values (%s,%s,%s) on conflict do nothing", plot_rows)
        cur.executemany("insert into entry_surveys (entry_id, survey_no, sub_division) values (%s,%s,%s) on conflict do nothing", survey_rows)
        cur.executemany("""insert into parties (entry_id, position, role, side, name, name_normalized, represented_by)
                           values (%s,%s,%s,%s,%s,%s,%s)""", party_rows)
        conn.commit()
    return doc_id, m, status


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="Postgres connection string (or set DATABASE_URL)")
    ap.add_argument("--owner", help="Supabase auth user id that owns this document")
    ap.add_argument("--storage-path")
    a = ap.parse_args()
    if not a.dsn:
        sys.exit("Set DATABASE_URL or pass --dsn")
    doc_id, m, status = load(a.pdf, a.dsn, a.owner, a.storage_path)
    print(f"Loaded {m['parsed_registered']} registered + {m['parsed_pending']} pending entries "
          f"(EC declares {m['declared_total']}), status={status}, document_id={doc_id}")


if __name__ == "__main__":
    main()
