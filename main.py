"""
EC Agent backend (FastAPI)

  uvicorn main:app --reload      -> http://localhost:8000

Endpoints (all need a Supabase login token, except /api/config):
  POST /api/upload     upload an EC PDF -> parsed and stored
  GET  /api/documents  list your uploaded ECs
  GET  /api/search     find entries by plot / survey / party / deed type / dates
  GET  /api/report     same filters, returns the Word report
"""
import os, re, tempfile, datetime, time, json, base64, threading
from collections import OrderedDict
from pathlib import Path

import httpx, psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from load_ec import load as load_pdf
from ec_tool import write_docx

DATABASE_URL = os.environ.get("DATABASE_URL")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
DEV_USER = os.environ.get("AUTH_DEV_USER")          # local testing only: skips login
if DEV_USER and (os.environ.get("RENDER") or os.environ.get("PRODUCTION")):
    raise RuntimeError("AUTH_DEV_USER must never be set on a deployed server (it disables login)")
MAX_UPLOAD_MB = 50
HERE = Path(__file__).parent

app = FastAPI(title="EC Agent")


# ---------------------------------------------------------------- auth
_token_cache: dict[str, tuple[str, float]] = {}   # token -> (user id, expiry); avoids a trip to Supabase per request
_cache_lock = threading.Lock()


def _token_exp(token: str) -> float:
    """Expiry from the JWT payload (only used to bound the cache; Supabase verifies the token)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return time.time() + 60


def current_user(authorization: str | None = Header(default=None)) -> str:
    """Returns the Supabase user id for the bearer token in the request."""
    if DEV_USER:
        return DEV_USER
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please log in")
    token = authorization.split(" ", 1)[1]
    now = time.time()
    with _cache_lock:
        hit = _token_cache.get(token)
    if hit and hit[1] > now:
        return hit[0]
    r = httpx.get(f"{SUPABASE_URL}/auth/v1/user",
                  headers={"apikey": SUPABASE_KEY, "Authorization": authorization}, timeout=10)
    if r.status_code != 200:
        raise HTTPException(401, "Session expired, please log in again")
    uid = r.json()["id"]
    with _cache_lock:
        if len(_token_cache) > 500:
            _token_cache.clear()
        _token_cache[token] = (uid, min(_token_exp(token), now + 300))
    return uid


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def db():
    """A pooled connection (reused, so no new TLS handshake to the database on every request).
    Use as `with db() as conn:` - commits on success, rolls back on error."""
    global _pool
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not set")
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=True,
                                       kwargs={"row_factory": dict_row, "prepare_threshold": None},
                                       check=ConnectionPool.check_connection)
    return _pool.connection()


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config():
    return {"supabase_url": SUPABASE_URL, "supabase_key": SUPABASE_KEY, "dev": bool(DEV_USER)}


# ---------------------------------------------------------------- upload
@app.post("/api/upload")
def upload(file: UploadFile = File(...), user: str = Depends(current_user)):
    # Plain "def": FastAPI runs it in a worker thread, so a 30s parse doesn't block other requests
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        safe_name = re.sub(r"[^\w.\- ]", "_", Path(file.filename).name)
        path = Path(tmp) / safe_name
        size = 0
        with open(path, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB")
                out.write(chunk)
        try:
            doc_id, meta, status = load_pdf(str(path), DATABASE_URL, owner=user)
        except Exception as e:
            raise HTTPException(422, f"Could not read this PDF as an encumbrance statement: {e}")
    return {
        "document_id": str(doc_id), "status": status, "file_name": safe_name,
        "statement_no": meta["statement_no"], "village": meta["village"], "survey": meta["survey"],
        "period": meta["period"], "pages": meta["pages"],
        "entries": meta["parsed_registered"], "pending": meta["parsed_pending"],
        "declared_total": meta["declared_total"],
    }


@app.get("/api/documents")
def documents(user: str = Depends(current_user)):
    with db() as conn:
        return conn.execute("""
            select id, file_name, statement_no, village, survey_searched, period_from, period_to,
                   page_count, parsed_total, parsed_pending, declared_total, status, uploaded_at
            from documents where owner_id = %s order by uploaded_at desc""", (user,)).fetchall()


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, user: str = Depends(current_user)):
    with db() as conn:
        n = conn.execute("delete from documents where id = %s and owner_id = %s", (doc_id, user)).rowcount
    if not n:
        raise HTTPException(404, "Document not found")
    return {"deleted": doc_id}


# ---------------------------------------------------------------- search
class Filters:
    def __init__(self, plot: str | None = None, survey: int | None = None, party: str | None = None,
                 layout: str | None = None, deed_category: str | None = None,
                 date_from: datetime.date | None = None, date_to: datetime.date | None = None,
                 document_id: str | None = None):
        self.plot = (plot or "").strip() or None
        self.survey = survey
        self.party = (party or "").strip() or None
        self.layout = (layout or "").strip() or None
        self.deed_category = deed_category or None
        self.date_from, self.date_to = date_from, date_to
        self.document_id = document_id or None

    def empty(self):
        return not any([self.plot, self.survey, self.party, self.layout, self.deed_category])

    def label(self):
        parts = [self.plot and f"Plot {self.plot}", self.survey and f"Survey {self.survey}",
                 self.party and f"Party '{self.party}'", self.layout and f"Layout '{self.layout}'",
                 self.deed_category and self.deed_category.title() + " deeds",
                 self.date_from and f"from {self.date_from:%d-%m-%Y}", self.date_to and f"to {self.date_to:%d-%m-%Y}"]
        return ", ".join(p for p in parts if p)


def run_search(conn, f: Filters, user: str):
    # The backend connects as the database owner (RLS bypassed), so filter by owner explicitly.
    rows = conn.execute("""
        select s.* from search_entries(%s, %s, %s, %s, %s, %s, %s, %s) s
        join documents d on d.id = s.document_id
        where d.owner_id = %s""",
        (f.plot, f.survey, f.party, f.layout, f.deed_category, f.date_from, f.date_to, f.document_id, user)).fetchall()
    if rows:
        parties = conn.execute("""select entry_id, role, name, represented_by from parties
                                  where entry_id = any(%s) order by entry_id, position""",
                               ([r["id"] for r in rows],)).fetchall()
        by_entry = {}
        for p in parties:
            by_entry.setdefault(p["entry_id"], []).append(p)
        for r in rows:
            r["parties"] = by_entry.get(r["id"], [])
    return rows


def fmt_date(d):
    return d.strftime("%d-%m-%Y") if d else ""


@app.get("/api/search")
def search(f: Filters = Depends(), user: str = Depends(current_user)):
    if f.empty():
        raise HTTPException(400, "Enter a plot, survey number, party or deed type")
    with db() as conn:
        rows = run_search(conn, f, user)
    return {"query": f.label(), "count": len(rows), "results": [{
        "id": r["id"], "sl_no": r["sl_no"], "page": r["page_no"], "pending": r["is_pending"],
        "reg_date": fmt_date(r["reg_date"]), "deed": r["deed_type"], "category": r["deed_category"],
        "doc_no": r["doc_no"], "sro": r["sro"], "layout": r["layout_key"],
        "plot": r["plot_text"], "extent": r["extent_text"],
        "executants": r["executants"], "claimants": r["claimants"],
        "market_value": r["market_value"], "consideration": r["consideration_value"],
        "link_doc": r["link_doc"], "statement_no": r["statement_no"],
    } for r in rows]}


@app.get("/api/report")
def report(f: Filters = Depends(), user: str = Depends(current_user)):
    if f.empty():
        raise HTTPException(400, "Enter a plot, survey number, party or deed type")
    with db() as conn:
        rows = run_search(conn, f, user)
        if not rows:
            raise HTTPException(404, f"No entries found for {f.label()}")
        doc_ids = list({r["document_id"] for r in rows})
        docs = conn.execute("""select * from documents where id = any(%s)""", (doc_ids,)).fetchall()
        doc_nos = [x["doc_no"] for x in conn.execute(
            "select doc_no from entries where document_id = any(%s)", (doc_ids,)).fetchall()]

    # Convert DB rows to the shape the report writer expects
    groups = OrderedDict()
    for r in rows:
        groups.setdefault(r["layout_key"] or "Unknown layout", []).append({
            "sl": r["sl_no"], "page": r["page_no"], "pending": r["is_pending"],
            "reg_date": fmt_date(r["reg_date"]), "exec_date": fmt_date(r["exec_date"]),
            "pres_date": fmt_date(r["pres_date"]), "deed": r["deed_type"],
            "mkt_value": r["market_value"], "cons_value": r["consideration_value"],
            "doc_no": r["doc_no"], "sro": r["sro"], "plot_txt": r["plot_text"] or "",
            "extent": r["extent_text"] or "", "survey_txt": r["survey_text"] or "",
            "link_doc": r["link_doc"] or "", "pending_reason": r["pending_reason"] or "",
            "parties": [{"role": p["role"], "name": p["name"] + (f" represented by {p['represented_by']}"
                                                                 if p["represented_by"] else "")}
                        for p in r["parties"]],
        })
    groups = OrderedDict(sorted(groups.items(), key=lambda kv: -len(kv[1])))
    if len(docs) == 1:
        d = docs[0]
        source = (f"Source: EC Statement No. {d['statement_no']}, Village {d['village']}, Survey No. "
                  f"{d['survey_searched']}, period {fmt_date(d['period_from'])} to {fmt_date(d['period_to'])} "
                  f"({d['page_count']} pages).")
    else:
        source = "Source: " + "; ".join(f"EC Statement No. {d['statement_no']} ({d['village']})" for d in docs) + "."
    data = {"meta": {"source_line": source}, "entries": [{"doc_no": n} for n in doc_nos]}

    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    write_docx(data, groups, f.label(), tmp.name)
    name = "EC_Report_" + re.sub(r"\W+", "_", f.label()).strip("_")[:80] + ".docx"
    return FileResponse(tmp.name, filename=name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        background=BackgroundTask(os.unlink, tmp.name))
