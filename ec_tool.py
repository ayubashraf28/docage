#!/usr/bin/env python3
"""
EC Report Tool
Parses a Telangana/AP "Statement of Encumbrance on Property" PDF (IGRS format)
and generates Word reports of transactions for a plot, survey number or party.

Usage:
  python ec_tool.py parse  EC.pdf                      -> EC.json
  python ec_tool.py report EC.json --plot 117           -> Word report
  python ec_tool.py report EC.json --survey 33
  python ec_tool.py report EC.json --party "GUNTI BHAVANI"
  (combine filters, e.g. --plot 117 --party BHAVANI; add -o out.docx)
"""
import argparse, json, re, sys, datetime
from collections import OrderedDict

EXEC_ROLES = {"EX", "E", "LR", "MR", "DR", "RR", "AR"}
ROLE_NAMES = {"EX": "Executant", "CL": "Claimant", "E": "Executant", "C": "Claimant",
              "MR": "Mortgagor", "ME": "Mortgagee", "DR": "Donor", "DE": "Donee",
              "RR": "Releasor", "RE": "Releasee", "LR": "Lessor", "LE": "Lessee"}


def flat(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------- parsing
def parse_pdf(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:  # closes the file (Windows locks open files)
        return _parse(pdf, path)


def _parse(pdf, path):
    header = flat(pdf.pages[0].extract_text() or "")[:1500]
    meta = {
        "statement_no": (re.search(r"Statement Number:\s*(\d+)", header) or [None, ""])[1],
        "application_no": (re.search(r"Application Number:\s*(\d+)", header) or [None, ""])[1],
        "village": (re.search(r"Village:\s*([^,]+)", header) or [None, ""])[1].strip(),
        "survey": (re.search(r"Survey Number:\s*([^,]+)", header) or [None, ""])[1].strip(),
        "period": (re.search(r"from\s*(\d\d-\d\d-\d{4}\s*to\s*\d\d-\d\d-\d{4})", header) or [None, ""])[1],
        "pages": len(pdf.pages),
        "source_file": path.split("/")[-1],
    }
    rows, pending = [], False
    last_text = ""
    n_pages = len(pdf.pages)
    for pg, page in enumerate(pdf.pages, 1):
        tables = page.extract_tables()
        if pg == n_pages:
            last_text = page.extract_text() or ""
        page.close()  # free the page's parsed objects; keeps memory flat on big PDFs
        for tb in tables:
            for r in tb:
                if not r or all(not c for c in r):
                    continue
                if r[0] and r[0].startswith("Sl"):
                    pending = "Pending" in (r[2] or "")
                    continue
                cells = [c or "" for c in r][:6]
                if r[0] and re.match(r"\d+/\d+", r[0]):
                    rows.append({"pending": pending, "page": pg, "cells": cells})
                elif rows:  # row continued from the previous page
                    rows[-1]["cells"] = [a + ("\n" + b if b else "") for a, b in zip(rows[-1]["cells"], cells)]
    entries = [parse_row(x) for x in rows]
    total = re.search(r"Result:\s*'(\d+) out of (\d+)", flat(last_text))
    meta["declared_total"] = int(total.group(2)) if total else None
    meta["parsed_registered"] = sum(not e["pending"] for e in entries)
    meta["parsed_pending"] = sum(e["pending"] for e in entries)
    return {"meta": meta, "entries": entries}


def nice_deed(d):
    d = d.title()
    for a, b in [(" Of ", " of "), (" In ", " in "), ("Gpa", "GPA"), ("F/O", "f/o"), (" Or ", " or "), ("Dotd", "DOTD"), ("G.O.", "G.O."), ("Ssi", "SSI"), ("Ms ", "MS "), ("Agrl.", "Agrl."), ("U/S", "u/s")]:
        d = d.replace(a, b)
    return d


def parse_row(x):
    sl, desc, dates, nat, parties, doc = x["cells"]
    d = dict(re.findall(r"\((R|E|P|PN)\)\s*(\d\d-\d\d-\d{4})", dates))
    code = nat.split("\n")[0]
    fn = flat(nat)
    mkt = re.search(r"Mkt\.Value:Rs\.\s*([\d.]+)", fn)
    con = re.search(r"Cons\.Value:Rs\.\s*([\d.]+)", fn)
    deed = flat(re.split(r"Mkt\.Value", nat)[0].replace(code, "", 1))
    party_list = re.findall(r"\d+\.\(([A-Z]+)\)\s*(.*?)(?=\s\d+\.\([A-Z]+\)|$)", flat(parties))
    f = flat(desc)
    plot_txt = (re.search(r"PLOT:\s*(.*?)\s*(?:EXTENT|HOUSE|BUILT|Boundar|$)", f) or [None, ""])[1]
    survey_txt = (re.search(r"SURVEY:\s*(.*?)\s*(?:PLOT|EXTENT|HOUSE|FLAT|$)", f) or [None, ""])[1]
    fd = flat(doc)
    dm = re.search(r"(\d+/\d{4})\s*\[", fd)
    sro = re.search(r"of SRO\s*(.*)$", fd)
    return {
        "sl": sl, "page": x["page"], "pending": x["pending"],
        "reg_date": d.get("R") or d.get("PN") or "", "exec_date": d.get("E", ""), "pres_date": d.get("P", ""),
        "deed_code": code, "deed": nice_deed(deed),
        "mkt_value": float(mkt.group(1)) if mkt else None,
        "cons_value": float(con.group(1)) if con else None,
        "parties": [{"role": r, "name": n.strip()} for r, n in party_list],
        "parties_raw": flat(parties),
        "village_ward": (re.search(r"VILL/COL:\s*(.*?)\s*SURVEY", f) or [None, ""])[1],
        "survey_txt": survey_txt.strip(" ,"),
        "plot_txt": plot_txt.strip(),
        "extent": (re.search(r"EXTENT:\s*,?\s*([\d.]+\s*(?:SQ\.?\s*Yds|Guntas|AC|Acres|SQ\.?\s*MTRS)?)", f, re.I) or [None, ""])[1],
        "boundaries": (re.search(r"Boundar\w*:\s*(.*?)(?:Link Doct|$)", f) or [None, ""])[1].strip(),
        "link_doc": (re.search(r"Link Doct:\s*([\d/]+)", f) or [None, ""])[1],
        "doc_no": dm.group(1) if dm else fd,
        "sro": sro.group(1).strip() if sro else "",
        "pending_reason": (re.search(r"\d+/\d{4}\s*\d+\.\s*([A-Z ]+?)\s*\d+/\d{4}", fd) or [None, ""])[1] if x["pending"] else "",
        "description": f, "doc_raw": fd,
    }


# ---------------------------------------------------------------- querying
def survey_bases(e):
    """Base survey numbers only, e.g. '22/ ^B/1/1 33^C/^A 33/P' -> [22, 33]."""
    t = re.sub(r"/\s+", "/", e["survey_txt"].replace(",", " "))
    return sorted({int(m) for m in re.findall(r"(?:^|\s)(\d+)", t)})


def layout_key(e):
    """Group by village/colony, because the same plot number can recur in different layouts.
    Normalises IGRS spelling variants (MANGALPALLY/MANGALPALLE, SHEBGUDA/SAHEBGUDA)."""
    w = e["village_ward"].upper()
    w = re.sub(r"\s*W-\s*B:.*$", "", w)
    w = w.replace("PALLY", "PALLE").replace("SHEBGUDA", "SAHEBGUDA").replace("IBRAHIMPATAN", "IBRAHIMPATAM")
    parts = [p.strip() for p in w.split("/") if p.strip()]
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return " / ".join(parts).title()


def plot_tokens(e):
    return re.findall(r"\b(\d+[A-Z]?)\b", e["plot_txt"])


def matches(e, plot=None, survey=None, party=None):
    if plot and plot.upper() not in plot_tokens(e):
        return False
    if survey and int(re.match(r"\d+", survey).group()) not in survey_bases(e):
        return False
    if party and party.upper() not in e["parties_raw"].upper():
        return False
    return True


def parse_date(s):
    try:
        return datetime.datetime.strptime(s, "%d-%m-%Y")
    except Exception:
        return datetime.datetime.min


def run_query(data, plot=None, survey=None, party=None):
    hits = [e for e in data["entries"] if matches(e, plot, survey, party)]
    groups = OrderedDict()
    for e in sorted(hits, key=lambda e: parse_date(e["reg_date"])):
        groups.setdefault(layout_key(e), []).append(e)
    # largest group first
    return OrderedDict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def parties_cell(e):
    ex = [p["name"] for p in e["parties"] if p["role"] in EXEC_ROLES]
    cl = [p["name"] for p in e["parties"] if p["role"] not in EXEC_ROLES]
    return "; ".join(ex) + "  →  " + "; ".join(cl)


def notes_cell(e, all_docs):
    n = []
    if e["pending"]:
        n.append("PENDING registration" + (f": {e['pending_reason'].strip().title()}" if e["pending_reason"] else ""))
    if "Rectification" in e["deed"] and e["link_doc"]:
        n.append(f"Rectifies / relates to {e['link_doc']}")
    elif e["link_doc"]:
        n.append(f"Link Doct: {e['link_doc']}" + ("" if e["link_doc"] in all_docs else " (outside this EC)"))
    if len(plot_tokens(e)) > 1 or "/" in e["plot_txt"]:
        n.append(f"Plot as described: {e['plot_txt']}")
    return "; ".join(n) or "—"


def rs(v):
    if v is None:
        return "—"
    v = int(v)
    s = str(v)
    if len(s) > 3:  # Indian digit grouping
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = head + "," + tail
    return "Rs. " + s


# ---------------------------------------------------------------- Word output
def write_docx(data, groups, query_label, out):
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    m = data["meta"]
    doc = Document()
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Cm(29.7), Cm(21.0)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Cm(1.5))
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Arial", Pt(9)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")

    def shade(cell, hexcolor):
        tcPr = cell._tc.get_or_add_tcPr()
        s = OxmlElement("w:shd")
        s.set(qn("w:val"), "clear"); s.set(qn("w:color"), "auto"); s.set(qn("w:fill"), hexcolor)
        tcPr.append(s)

    def repeat_header(row):
        trPr = row._tr.get_or_add_trPr()
        h = OxmlElement("w:tblHeader"); h.set(qn("w:val"), "true"); trPr.append(h)

    h = doc.add_paragraph(); r = h.add_run(f"Encumbrance Report: {query_label}"); r.bold = True; r.font.size = Pt(15)
    info = doc.add_paragraph()
    source = m.get("source_line") or (f"Source: EC Statement No. {m['statement_no']}, Village {m['village']}, "
                                      f"Survey No. {m['survey']}, period {m['period']} ({m['pages']} pages).")
    info.add_run(source + " ").font.size = Pt(9)
    total = sum(len(v) for v in groups.values())
    info.add_run(f"{total} matching entr{'y' if total == 1 else 'ies'} found in {len(groups)} property group(s).").font.size = Pt(9)

    all_docs = {e["doc_no"] for e in data["entries"]}
    heads = ["EC Sl. No. (Page)", "Reg. Date", "Exec / Pres Dates", "Nature of Deed", "Doc No. / SRO",
             "Property", "Parties (EX → CL)", "Market Value", "Consideration", "Notes"]
    widths = [1.6, 1.7, 1.9, 2.3, 2.3, 2.8, 6.6, 1.8, 1.9, 3.8]

    for gi, (key, entries) in enumerate(groups.items(), 1):
        p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(10)
        sy = sorted({n for e in entries for n in survey_bases(e)})
        rr = p.add_run(f"Group {gi}: {key}, Survey Nos. {', '.join(map(str, sy))}  ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})"); rr.bold = True; rr.font.size = Pt(11)
        t = doc.add_table(rows=1, cols=len(heads)); t.style = "Table Grid"; t.alignment = WD_TABLE_ALIGNMENT.CENTER
        t.autofit = False
        for i, (c, txt) in enumerate(zip(t.rows[0].cells, heads)):
            c.text = ""; run = c.paragraphs[0].add_run(txt); run.bold = True; run.font.size = Pt(8)
            shade(c, "D9E2F3"); c.width = Cm(widths[i])
        repeat_header(t.rows[0])
        for i, gc in enumerate(t._tbl.tblGrid.findall(qn("w:gridCol"))):
            gc.set(qn("w:w"), str(int(widths[i] * 567)))
        for e in entries:
            prop = f"Plot {e['plot_txt'] or '—'}; {e['extent']}".strip("; ")
            vals = [f"{e['sl']} (p.{e['page']})", e["reg_date"], f"{e['exec_date']} / {e['pres_date']}",
                    e["deed"], f"{e['doc_no']}, SRO {e['sro']}", prop, parties_cell(e),
                    rs(e["mkt_value"]), rs(e["cons_value"]), notes_cell(e, all_docs)]
            cells = t.add_row().cells
            for i, (c, v) in enumerate(zip(cells, vals)):
                c.text = ""; run = c.paragraphs[0].add_run(v); run.font.size = Pt(8); c.width = Cm(widths[i])
                if e["pending"] or "Rectification" in e["deed"] or "Revocation" in e["deed"]:
                    shade(c, "FFF2CC")
    doc.add_paragraph()
    note = doc.add_paragraph()
    nr = note.add_run("Entries are grouped by village/ward and survey numbers, because the same plot number can appear in "
                      "different layouts. Highlighted rows are rectifications, revocations or pending documents. "
                      "Generated automatically from the EC; verify against the original statement (EC Sl. No. and page given) before relying on it.")
    nr.italic = True; nr.font.size = Pt(8); nr.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
    doc.save(out)


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("parse"); p1.add_argument("pdf"); p1.add_argument("-o")
    p2 = sub.add_parser("report"); p2.add_argument("json")
    p2.add_argument("--plot"); p2.add_argument("--survey"); p2.add_argument("--party"); p2.add_argument("-o")
    a = ap.parse_args()
    if a.cmd == "parse":
        data = parse_pdf(a.pdf)
        out = a.o or re.sub(r"\.pdf$", "", a.pdf, flags=re.I) + ".json"
        json.dump(data, open(out, "w"), indent=1)
        m = data["meta"]
        print(f"Parsed {m['parsed_registered']} registered + {m['parsed_pending']} pending entries "
              f"(EC declares {m['declared_total']}) -> {out}")
        if m["declared_total"] and m["declared_total"] != m["parsed_registered"]:
            print("WARNING: count mismatch, check the PDF", file=sys.stderr)
    else:
        if not (a.plot or a.survey or a.party):
            sys.exit("Give at least one of --plot, --survey, --party")
        data = json.load(open(a.json))
        groups = run_query(data, a.plot, a.survey, a.party)
        label = ", ".join(x for x in [a.plot and f"Plot {a.plot}", a.survey and f"Survey {a.survey}", a.party and f"Party '{a.party}'"] if x)
        if not groups:
            sys.exit(f"No entries found for {label}")
        out = a.o or re.sub(r"\W+", "_", label).strip("_") + ".docx"
        write_docx(data, groups, label, out)
        for k, v in groups.items():
            print(f"{len(v):4d}  {k}  Sy. {sorted({n for e in v for n in survey_bases(e)})}")
        print("->", out)


if __name__ == "__main__":
    main()
