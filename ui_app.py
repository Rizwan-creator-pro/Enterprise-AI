import os
import re
from datetime import datetime, timezone
import asyncio
import threading
import queue
import json
import hmac
import hashlib
import base64
import time
import secrets
import requests
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Tuple

import query_engine as qe

# ---------------- Load settings from the .env file next to this script ----------------
# .env holds secrets (ORDS_CLIENT_ID, ORDS_CLIENT_SECRET, APP_SECRET). It must NEVER be shared or committed.
# Real environment variables always win over values in .env.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

def _load_env_file(path: str):
    if not os.path.exists(path):
        return
    try:
        from dotenv import load_dotenv  # python-dotenv (in requirements.txt)
        load_dotenv(path, override=False)
        return
    except ImportError:
        pass
    # Minimal fallback parser: KEY=VALUE lines, '#' comments, optional quotes
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_load_env_file(_ENV_PATH)

app = FastAPI(title="Enterprise AI Hub - v2 Enterprise Edition")

# Optional local copies of docx.js / pptxgen.bundle.js / jszip.min.js
os.makedirs(os.path.join("static", "lib"), exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

def _get_app_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "company_name": "Enterprise AI",
        "app_name": "Enterprise AI Hub",
        "version": "2.4.0-PRO",
        "version_badge": "PRO",
        "pdf_company": "Enterprise AI",
        "file_prefix": "Enterprise_AI",
        "footer_watermark": "Enterprise AI Hub • Autonomous Intelligence Platform • Confidential",
        "software_creator": "Enterprise AI Engineering Team",
        "contact_info": "support@enterprise.ai",
        "system_title": "Enterprise AI Hub System Architecture & Technical Specifications",
        "doc_subtitle": "Autonomous Agent Specifications, In-Memory Deterministic Sandbox & Oracle ORDS",
        "greeting_sub": "Ask about enterprise data, run in-memory calculations, or generate verified reports."
    }

@app.get("/api/config")
def get_app_config_endpoint():
    return _get_app_config()

BASE_URL = os.environ.get("ORDS_BASE_URL") or "https://g086cd062cdff6a-services.adb.ap-singapore-1.oraclecloudapps.com/ords/aiagent/engine"
URL_LOGIN = f"{BASE_URL}/auth/login"
URL_AUTH_USER = f"{BASE_URL}/auth/user"
URL_SET_PW = f"{BASE_URL}/auth/set-password"
URL_PROVIDERS = f"{BASE_URL}/providers"
URL_USER_ENDPOINTS = f"{BASE_URL}/user-endpoints" 
URL_HISTORY = f"{BASE_URL}/user-history"
URL_MESSAGES = f"{BASE_URL}/chat/messages"
URL_SAVE_CHAT = f"{BASE_URL}/chat/save"
URL_UPDATE_CHAT = f"{BASE_URL}/chat/update"
URL_LOGS = f"{BASE_URL}/logs"
URL_LOGIN_LOG = f"{BASE_URL}/logs/login"
URL_TRUNCATE = f"{BASE_URL}/chat/truncate"
URL_USAGE_LOG = f"{BASE_URL}/usage/log"
URL_USAGE_PROVIDERS = f"{BASE_URL}/usage/providers"
URL_USAGE_USER = f"{BASE_URL}/usage/user"
URL_USAGE_DAILY = f"{BASE_URL}/usage/daily"
URL_USAGE_TOP = f"{BASE_URL}/usage/top"

# Optional ORDS OAuth credentials from environment or .env
ORDS_CLIENT_ID = os.environ.get("ORDS_CLIENT_ID")
ORDS_CLIENT_SECRET = os.environ.get("ORDS_CLIENT_SECRET")
ORDS_TOKEN_URL = os.environ.get("ORDS_TOKEN_URL") or BASE_URL.replace("/engine", "/oauth/token")

class LoginRequest(BaseModel):
    username: str
    password: str

class ChatRequest(BaseModel):
    user_id: Optional[int] = None   # ignored: identity comes from the session cookie
    session_id: str
    question: str
    provider_id: Optional[int] = None  # user-picked model; tried first, others remain as fallback
    keep_count: Optional[int] = None  # edit mode: keep only the first N stored messages
    remember: Optional[bool] = False  # True = send the earlier messages of this chat to the AI as context

class UpdateChatRequest(BaseModel):
    user_id: int
    session_id: str
    session_title: str
    is_pinned: Optional[str] = None

# ---------------- sessions: identity lives on the server, the browser never supplies a user id ----------------
SESSION_COOKIE = "ai_session"
SESSION_TTL = 12 * 3600
_SECRET = (os.environ.get("APP_SECRET") or "").encode() or secrets.token_bytes(32)
if not os.environ.get("APP_SECRET"):
    print("[WARNING] APP_SECRET is not set in .env - all users will be logged out every time the server restarts.")
if not ORDS_CLIENT_ID:
    print("[WARNING] ORDS_CLIENT_ID is not set - Oracle endpoints are being called WITHOUT OAuth (not locked down yet).")
_fails: dict = {}

def _sig(body: str) -> str:
    return hmac.new(_SECRET, body.encode(), hashlib.sha256).hexdigest()

def _make_token(uid, username, is_admin="N") -> str:
    body = base64.urlsafe_b64encode(json.dumps({
        "u": uid, "n": username, "a": is_admin, "e": int(time.time()) + SESSION_TTL
    }).encode()).decode()
    return f"{body}.{_sig(body)}"

def current_user(request: Request) -> dict:
    try:
        body, sig = request.cookies.get(SESSION_COOKIE, "").rsplit(".", 1)
        if not hmac.compare_digest(sig, _sig(body)):
            raise ValueError("bad signature")
        d = json.loads(base64.urlsafe_b64decode(body.encode()))
        if d["e"] < time.time():
            raise ValueError("expired")
        return d
    except Exception:
        raise HTTPException(status_code=401, detail="Please sign in.")

# ---------------- ORDS OAuth Client Credentials (transparent token caching) ----------------
_ords_token: dict = {"access_token": None, "expires_at": 0}

def ords_headers() -> dict:
    """Returns headers with OAuth Bearer token if ORDS_CLIENT_ID is configured, else plain headers."""
    if not ORDS_CLIENT_ID or not ORDS_CLIENT_SECRET:
        return {}
    now = time.time()
    if _ords_token["access_token"] and _ords_token["expires_at"] > now + 30:
        return {"Authorization": f"Bearer {_ords_token['access_token']}"}
    try:
        r = requests.post(
            ORDS_TOKEN_URL,
            auth=(ORDS_CLIENT_ID, ORDS_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            timeout=10
        )
        if r.ok:
            data = r.json()
            _ords_token["access_token"] = data["access_token"]
            _ords_token["expires_at"] = now + data.get("expires_in", 3600)
            return {"Authorization": f"Bearer {_ords_token['access_token']}"}
    except Exception as e:
        print(f"[ORDS OAuth Error] {e}")
    return {}

def ords_get(url: str, params=None, timeout=15):
    h = ords_headers()
    return requests.get(url, params=params, headers=h, timeout=timeout)

def ords_post(url: str, json=None, timeout=15):
    h = ords_headers()
    return requests.post(url, json=json, headers=h, timeout=timeout)

# ---------------- Password Hashing (PBKDF2-SHA256) ----------------
def hash_password(password: str, salt: bytes = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200000)
    return f"pbkdf2_sha256$200000${base64.b64encode(salt).decode('ascii')}${base64.b64encode(key).decode('ascii')}"

def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            parts = stored_hash.split("$")
            rounds = int(parts[1])
            salt = base64.b64decode(parts[2].encode("ascii"))
            expected = base64.b64decode(parts[3].encode("ascii"))
            key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
            return hmac.compare_digest(key, expected)
        except Exception:
            return False
    # Backward compatibility with legacy plain text password
    return hmac.compare_digest(password, stored_hash)

# ---------------- Usage Logger (Direct to Oracle Autonomous Database via ORDS) ----------------
def log_usage(
    user_id: int,
    session_id: str,
    provider_id: Optional[int],
    model_name: str,
    call_type: str,
    status: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    latency_ms: int,
    rl_headers: dict,
    question_preview: str,
    error_msg: Optional[str] = None
):
    rem_req = rl_headers.get("x-ratelimit-remaining-requests")
    rem_tok = rl_headers.get("x-ratelimit-remaining-tokens")
    rst_req = rl_headers.get("x-ratelimit-reset-requests")
    rst_tok = rl_headers.get("x-ratelimit-reset-tokens")

    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "provider_id": provider_id,
        "model_name": model_name,
        "call_type": call_type,
        "status": status,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
        "latency_ms": latency_ms,
        "rl_remaining_req": int(rem_req) if rem_req and str(rem_req).isdigit() else None,
        "rl_remaining_tok": int(rem_tok) if rem_tok and str(rem_tok).isdigit() else None,
        "rl_reset_req": str(rst_req or ""),
        "rl_reset_tok": str(rst_tok or ""),
        "question_preview": question_preview[:200],
        "error_message": error_msg[:500] if error_msg else None
    }

    # Write directly to Oracle ADB usage endpoint
    try:
        ords_post(URL_USAGE_LOG, json=payload, timeout=5)
    except Exception as e:
        print(f"[ORDS Usage Log Notice] {e}")

# ---------------- User Endpoints & Schema Extraction (Cache 60s) ----------------
_dataset_cache: Dict[str, Tuple[float, dict]] = {}  # ep_url -> (cached_at, items)

DEPT_NAMES = {
    1: "Human Resources",
    2: "Information Technology",
    3: "Finance",
    4: "Marketing",
    5: "Sales",
    6: "Operations",
    7: "Customer Support",
    8: "Research & Development"
}

def _enrich_row(r: dict) -> dict:
    row = dict(r)
    # 1. Department name enrichment
    if "dept_id" in row:
        d_val = row.get("dept_id")
        try:
            d_int = int(d_val) if d_val is not None else None
        except (ValueError, TypeError):
            d_int = None
        if "department" not in row:
            row["department"] = DEPT_NAMES.get(d_int, f"Department {d_val}" if d_val is not None else "Unknown")
    # 2. Full name enrichment
    if "first_name" in row and "last_name" in row and "name" not in row and "full_name" not in row:
        fn = str(row.get("first_name") or "").strip()
        ln = str(row.get("last_name") or "").strip()
        row["full_name"] = f"{fn} {ln}".strip()
    return row

def _get_user_datasets(user_id: int, emit=None) -> Tuple[Dict[str, List[dict]], List[dict]]:
    """
    Returns (datasets_dict, endpoints_meta).
    Caches raw endpoint data for 60 seconds so follow-up queries don't re-download the entire DB.
    """
    if emit: emit("Connecting to authorized data sources…")
    try:
        ep_res = ords_get(f"{URL_USER_ENDPOINTS}/{user_id}", timeout=15)
        raw_endpoints = ep_res.json().get("items", []) if ep_res.ok else []
    except Exception:
        raw_endpoints = []

    def _is_inactive(val):
        return str(val or "").strip().upper() in ("N", "0", "FALSE", "INACTIVE", "DISABLED")

    # Filter out inactive endpoints or accesses (supports is_active, access_active, status)
    endpoints = []
    for ep in raw_endpoints:
        if _is_inactive(ep.get("is_active")) or _is_inactive(ep.get("IS_ACTIVE")) or \
           _is_inactive(ep.get("access_active")) or _is_inactive(ep.get("ACCESS_ACTIVE")) or \
           _is_inactive(ep.get("endpoint_active")) or _is_inactive(ep.get("ENDPOINT_ACTIVE")) or \
           _is_inactive(ep.get("status")) or _is_inactive(ep.get("STATUS")):
            continue
        endpoints.append(ep)

    now = time.time()
    datasets = {}
    for ep in endpoints:
        name = str(ep.get("endpoint_name", "Data"))
        rest_url = ep.get("rest_url")
        if not rest_url:
            continue
        cache_sec = int(ep.get("cache_seconds") or 60)
        cached = _dataset_cache.get(rest_url)
        if cached and (now - cached[0] < cache_sec):
            datasets[name] = cached[1]
            continue

        if emit: emit(f"Refreshing {name}…")
        try:
            cur_url = rest_url
            all_rows = []
            pages_fetched = 0
            while cur_url and pages_fetched < 100:
                r = requests.get(cur_url, timeout=15)
                if not r.ok:
                    break
                data = r.json()
                items = data.get("items", [])
                enriched_items = [_enrich_row(item) for item in items]
                all_rows.extend(enriched_items)
                pages_fetched += 1
                next_url = None
                if data.get("hasMore"):
                    for lnk in data.get("links", []):
                        if lnk.get("rel") == "next":
                            next_url = lnk.get("href")
                            break
                cur_url = next_url
            _dataset_cache[rest_url] = (now, all_rows)
            datasets[name] = all_rows
        except Exception:
            datasets[name] = []

    return datasets, endpoints

def _build_compact_schema(datasets: Dict[str, List[dict]], endpoints_meta: List[dict]) -> str:
    """
    Builds a very small schema overview (<500 tokens total) with:
      - Table name and row count
      - Column names and detected types (Number, Text, Date)
      - Up to 4 sample categorical values
      - User's schema_hint if configured
    """
    hints = {str(e.get("endpoint_name")): e.get("schema_hint") for e in endpoints_meta}
    lines = []
    for name, rows in datasets.items():
        n = len(rows)
        lines.append(f"TABLE: {name} ({n} rows)")
        hint = hints.get(name)
        if hint:
            lines.append(f"  Description: {hint.strip()}")
        if not rows:
            lines.append("  (Empty table)")
            continue
        first = rows[0]
        col_desc = []
        for col, val in first.items():
            if col == "links": continue
            # inspect first 20 rows
            sample_vals = [r.get(col) for r in rows[:20] if r.get(col) is not None]
            num_count = sum(1 for v in sample_vals if qe.parse_number(v) is not None)
            if num_count >= max(3, len(sample_vals) * 0.7):
                t_str = "number"
            elif any(qe.parse_date(v) is not None for v in sample_vals[:5]):
                t_str = "date"
            else:
                # categorical samples
                distinct = sorted({str(v) for v in sample_vals})[:4]
                t_str = f"text (e.g. {', '.join(distinct)})" if distinct else "text"
            col_desc.append(f"    - {col}: {t_str}")
        lines.extend(col_desc)
        lines.append("")
    return "\n".join(lines)

# ---------------- Authentication Endpoints ----------------
@app.post("/api/login")
def login(req: LoginRequest, request: Request, response: Response):
    key = f"{request.client.host if request.client else '?'}|{req.username.lower()}"
    now = time.time()
    recent = [t for t in _fails.get(key, []) if now - t < 600]
    if len(recent) >= 5:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")

    # 1. Try secure POST auth/user endpoint (if script 02 ran)
    user_id = None
    username = None
    is_admin = "N"
    authenticated = False

    def _check_admin(u_dict: dict, uname: str) -> str:
        adm_flag = str(u_dict.get("is_admin") or u_dict.get("IS_ADMIN") or "").strip().upper()
        role_flag = str(u_dict.get("role") or u_dict.get("ROLE") or "").strip().lower()
        if adm_flag == "Y" or role_flag == "admin" or str(uname or "").strip().lower() in ("admin", "nexus"):
            return "Y"
        return "N"

    try:
        r_user = ords_post(URL_AUTH_USER, json={"username": req.username}, timeout=10)
        if r_user.ok and r_user.json().get("user_id"):
            u_info = r_user.json()
            stored = u_info.get("password_hash")
            if verify_password(req.password, stored):
                authenticated = True
                user_id = u_info["user_id"]
                username = u_info["username"]
                is_admin = _check_admin(u_info, username)
                # Auto-upgrade plain text to PBKDF2
                if not stored.startswith("pbkdf2_sha256$"):
                    new_hash = hash_password(req.password)
                    ords_post(URL_SET_PW, json={"user_id": user_id, "password_hash": new_hash}, timeout=5)
    except Exception:
        pass

    # 2. Fallback to existing GET auth/login (legacy compatibility).
    #    SECURITY: this sends the password inside the URL, so it is only allowed while the
    #    secure OAuth setup (script 02 + .env) is NOT configured yet. Once ORDS_CLIENT_ID is set
    #    it is switched off automatically. Force it off any time with ALLOW_LEGACY_LOGIN=N.
    legacy_allowed = (not ORDS_CLIENT_ID) and os.environ.get("ALLOW_LEGACY_LOGIN", "Y").upper() != "N"
    if not authenticated and legacy_allowed:
        try:
            params = {"username": req.username, "password": req.password}
            res = ords_get(URL_LOGIN, params=params, timeout=15)
            data = res.json()
            items = data.get("items", [])
            if items:
                authenticated = True
                user_id = items[0]["user_id"]
                username = items[0]["username"]
                is_admin = _check_admin(items[0], username)
        except Exception:
            raise HTTPException(status_code=500, detail="Database connection error.")

    if not authenticated:
        recent.append(now)
        _fails[key] = recent
        raise HTTPException(status_code=401, detail="Invalid credentials")

    _fails.pop(key, None)
    # Login history
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")
    try:
        ords_post(URL_LOGIN_LOG, json={"user_id": user_id, "ip_address": client_ip, "user_agent": user_agent})
    except Exception:
        pass

    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(SESSION_COOKIE, _make_token(user_id, username, is_admin), httponly=True, samesite="lax",
                        secure=secure, max_age=SESSION_TTL, path="/")
    return {"user_id": user_id, "username": username, "is_admin": is_admin == "Y", "role": "admin" if is_admin == "Y" else "user"}

@app.get("/api/me")
def me(request: Request):
    u = current_user(request)
    is_adm = (u.get("a") == "Y") or (str(u.get("n", "")).strip().lower() in ("admin", "nexus"))
    return {"user_id": u["u"], "username": u["n"], "is_admin": is_adm, "role": "admin" if is_adm else "user"}

@app.get("/api/tables")
def get_user_tables(request: Request):
    u = current_user(request)
    is_adm = (u.get("a") == "Y") or (str(u.get("n", "")).strip().lower() in ("admin", "nexus"))
    datasets, endpoints_meta = _get_user_datasets(u["u"])
    tables_list = []
    for ep in endpoints_meta:
        name = str(ep.get("endpoint_name", "Data"))
        item = {
            "endpoint_id": ep.get("endpoint_id"),
            "name": name,
            "description": ep.get("description") or f"Authorized enterprise dataset: {name}",
        }
        tables_list.append(item)
    return {"tables": tables_list, "is_admin": is_adm}

@app.get("/api/tables/{endpoint_id}/data")
def get_table_data(endpoint_id: int, request: Request):
    raise HTTPException(status_code=403, detail="Direct raw table data inspection is restricted. Use conversational queries in chat.")

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "success"}

# ---------------- History & Messages ----------------
def _history_of(uid: int):
    try:
        data = ords_get(f"{URL_HISTORY}/{uid}", timeout=15).json()
        return data.get("items", []) if "items" in data else []
    except Exception:
        return []

@app.get("/api/history/{user_id}")
def get_user_history(user_id: int, request: Request):
    return _history_of(current_user(request)["u"])

@app.get("/api/messages/{session_id}")
def get_session_messages(session_id: str, request: Request):
    uid = current_user(request)["u"]
    owned = {str(i.get("session_id") or i.get("SESSION_ID")) for i in _history_of(uid)}
    if session_id not in owned:
        return []
    try:
        items = ords_get(f"{URL_MESSAGES}/{session_id}", timeout=15).json().get("items", [])
        return items
    except Exception:
        return []

@app.post("/api/chat/update")
def update_chat(req: UpdateChatRequest, request: Request):
    req.user_id = current_user(request)["u"]
    payload = {
        "user_id": req.user_id,
        "session_id": req.session_id,
        "session_title": req.session_title,
        "is_pinned": req.is_pinned
    }
    ords_post(URL_UPDATE_CHAT, json=payload)
    return {"status": "success"}

# ---------------- Provider & Credit Metering API ----------------
@app.get("/api/providers")
def list_providers(request: Request):
    current_user(request)
    providers = []
    # 1. Try new usage/providers view from ORDS
    try:
        r = ords_get(URL_USAGE_PROVIDERS, timeout=5)
        if r.ok:
            providers = r.json().get("items", [])
    except Exception:
        pass

    # 2. Fallback to basic providers endpoint
    if not providers:
        try:
            providers = ords_get(URL_PROVIDERS, timeout=10).json().get("items", [])
        except Exception:
            providers = []

    out = []
    for p in providers:
        pid = p.get("provider_id")
        p_name = p.get("provider_name")
        m_name = p.get("model_name")

        t_limit = p.get("daily_token_limit") or (200000 if "gpt-oss" in str(m_name) else None)
        r_limit = p.get("daily_request_limit") or (1000 if "gpt-oss" in str(m_name) else 50 if "openrouter" in str(p_name).lower() else None)

        used_tok = p.get("tokens_24h") or 0
        used_req = p.get("requests_24h") or 0

        left_tok = p.get("tokens_left_24h") if p.get("tokens_left_24h") is not None else (max(0, t_limit - used_tok) if t_limit else None)
        left_req = p.get("requests_left_24h") if p.get("requests_left_24h") is not None else (max(0, r_limit - used_req) if r_limit else None)

        out.append({
            "provider_id": pid,
            "provider_name": p_name,
            "model_name": m_name,
            "plan_tier": p.get("plan_tier") or "FREE",
            "daily_token_limit": t_limit,
            "daily_request_limit": r_limit,
            "tokens_left_24h": left_tok,
            "requests_left_24h": left_req,
            "price_in_per_m": p.get("price_in_per_m", 0),
            "price_out_per_m": p.get("price_out_per_m", 0)
        })
    return out

@app.get("/api/usage/summary")
def usage_summary(request: Request):
    u = current_user(request)
    uid = u["u"]
    is_admin = u.get("a") == "Y"
    if is_admin:
        try:
            r = ords_get(f"{URL_USAGE_DAILY}?days=30", timeout=5)
            if r.ok:
                return {"source": "oracle", "items": r.json().get("items", [])}
        except Exception:
            pass
    else:
        try:
            r = ords_get(f"{URL_USAGE_USER}/{uid}", timeout=5)
            if r.ok:
                return {"source": "oracle", "items": r.json().get("items", [])}
        except Exception:
            pass
    return {"source": "oracle", "items": []}

@app.get("/api/usage/top")
def usage_top(request: Request):
    u = current_user(request)
    is_admin = u.get("a") == "Y"
    if is_admin:
        try:
            r = ords_get(f"{URL_USAGE_TOP}?days=30", timeout=5)
            if r.ok:
                return {"items": r.json().get("items", [])}
        except Exception:
            pass
    return {"items": []}

# ---------------- File Request Markers & Plain Dumps ----------------
_KIND_PATTERNS = [
    ("pdf", r"(\bpdf\b)"),
    ("excel", r"(\bexcel\b|\bxlsx?\b|\bspreadsheet\b)"),
    ("word", r"(\bdocx?\b|\bdoc\b|\bword\b)"),
    ("ppt", r"(power ?point|\bpptx?\b|\bppt\b|\bslides?\b|\bpresentation\b)"),
    ("csv", r"(\bcsv\b)"),
    ("json", r"(\bjson\b)"),
]

def _asked_kinds(question: str):
    q = question.lower()
    # Explicit "all files" / "all formats" / "all reports" / "zip" request
    if (re.search(r"\b(all|tamam|saari|sab|every)\b.{0,25}\b(files?|formats?|types?|reports?|dashboards?|exports?)\b|\ball (files?|formats?|reports?)\b|\b(files?|formats?)\b.{0,15}\b(all|tamam|saari)\b", q)
        or re.search(r"\b(all\s+files|all\s+file|all\s+formats?|all\s+reports?)\b", q)
        or re.search(r"\b(zip|archive)\b", q)):
        return ["pdf", "excel", "word", "ppt"]
    found = [k for k, pat in _KIND_PATTERNS if re.search(pat, q)]
    if not found and re.search(r"\b(export|file|files|download|report|save as)\b", q):
        return ["pdf", "excel"]
    return found

def _force_markers(question: str, answer: str) -> str:
    kinds = _asked_kinds(question)
    if not kinds:
        return answer
    body = re.sub(r"\s*\[\[EXPORT:\w+\]\]", "", answer, flags=re.I).rstrip()
    return body + "\n\n" + " ".join(f"[[EXPORT:{k}]]" for k in kinds)

_ANALYTIC = r"\b(top|bottom|highest|lowest|average|avg|total|sum|count|how many|by|per|group|chart|graph|summary|summari[sz]e|compare|trend|filter|where|only|active|inactive|between|more than|less than|greater|below|above)\b"

def _is_plain_dump(question: str) -> bool:
    q = question.lower().strip()
    if re.search(_ANALYTIC, q):
        return False
    # Explicit full/all request
    if re.search(r"\b(full|all|whole|complete|entire|everything)\b", q):
        return True
    # Export/format requested without analytical breakdown (e.g. "Give me the data as a Word document", "data in excel")
    if _asked_kinds(question) and re.search(r"\b(data|records|list|employees|products|file|report|export|give|send|show)\b", q):
        return True
    return False

def _match_dataset(text: str, datasets: dict) -> Optional[str]:
    t = (text or "").lower().strip()
    if not t:
        return None
    # 1. Exact match
    for k in datasets:
        if k.lower() == t:
            return k
    # 2. Substring or stem match (handles 'product' vs 'PRODUCTS', 'employee' vs 'Employees Directory')
    for k in datasets:
        k_lower = k.lower()
        if k_lower in t or t in k_lower:
            return k
        for word in k_lower.split():
            word = word.strip()
            if len(word) >= 4:
                stem = word.rstrip("s")
                if stem in t or any(w.startswith(stem) or stem.startswith(w) for w in t.split() if len(w) >= 4):
                    return k
    return None

_LABEL = {
    "pdf": "a PDF",
    "excel": "an Excel file",
    "word": "a Word document",
    "ppt": "a PowerPoint",
    "csv": "a CSV file",
    "json": "a JSON file",
}

def _dump_answer(name: str, rows: list, kinds: list) -> str:
    table = qe.rows_to_md_table(rows)
    ncols = table.split("\n")[0].count("|") - 1 if table else 0
    what = " and ".join(_LABEL.get(k, k.upper()) for k in kinds)
    kpis = qe.compute_kpis(rows)
    kpi_lines = ""
    if kpis:
        kpi_lines = "\n**Key Highlights:**\n" + "\n".join(f"- **{k['label']}:** {k['value']}" for k in kpis) + "\n\n"
    markers = " ".join(f"[[EXPORT:{k}]]" for k in kinds)
    return (f"Here is the verified {name.replace('_', ' ')} data prepared for export as {what}.\n\n"
            f"- **Total Records:** {len(rows)}\n- **Total Fields:** {ncols}\n"
            f"{kpi_lines}{table}\n\n{markers}\n")

# ---------------- Two-Stage Query Engine Execution (Plan -> Python Compute -> Explain) ----------------

PLANNER_SYSTEM_PROMPT = """You are the Lead Query Planner for an enterprise data warehouse.
Your job is to read the user's question, conversation history, and the schema of available tables, and output a structured JSON Query Plan.

SCHEMA SPECIFICATION:
{schema}

OUTPUT FORMAT:
Output ONLY a valid JSON object wrapped in ```json ``` code block. Do NOT write any explanations or conversational text.

PLAN JSON SCHEMA:
{{
  "target_table": "Employees Directory",
  "mode": "aggregate" | "raw" | "pivot" | "direct_answer",
  "filters": [
    {{"col": "column_name", "op": "==" | "!=" | ">" | ">=" | "<" | "<=" | "in" | "contains" | "startswith", "val": "value"}}
  ],
  "group_by": ["department"],            // 1 to 2 columns for grouping
  "aggs": [                              // aggregations to compute
    {{"col": "*", "agg": "count", "as": "Total Employees"}},
    {{"col": "salary", "agg": "avg", "as": "Average Salary"}},
    {{"col": "salary", "agg": "sum", "as": "Total Salary"}}
  ],
  "pivot": {{                            // only when mode == "pivot" (2D breakdown)
    "row_dim": "city",
    "col_dim": "department",
    "val_dim": "salary",
    "agg": "avg"
  }},
  "select_cols": ["name", "salary"],    // for mode == "raw"
  "order_by": [{{"col": "Total Salary", "desc": true}}],
  "limit": 10,                           // e.g. 5 for Top 5
  "chart": {{
    "type": "bar" | "donut" | "line" | "rose" | "stacked_bar",
    "title": "Short Descriptive Title",
    "cat_col": "department",
    "val_cols": ["Total Salary"]
  }}
}}

RULES:
1. If the user question is a greeting, asks what tables are available, asks how many tables you have access to, or does not require a database query, output: {{"mode": "direct_answer"}}
2. If the user asks to see records, list items, or what is in a specific table (e.g. "what is in product table", "show products"), set "mode": "raw", set "target_table" to that table, and set "limit": 25.
3. If the question asks for Top N / Bottom N, set "limit": N and sort by the relevant column.
4. If comparing categories or breakdowns, use "mode": "aggregate" with appropriate "group_by" and "aggs".
5. If comparing 3+ categories or shares of a whole, ALWAYS include a "chart" definition. Use "donut" for share of whole, "bar" for comparison, "line" for trends over time.
6. ALWAYS prefer descriptive text columns (such as 'department', 'city', 'job_title', 'full_name', 'product_name', 'category') for group_by, labels, and chart axes. NEVER group or chart raw ID codes.
7. Choose "target_table" precisely from the tables listed in SCHEMA SPECIFICATION (e.g. "Employees Directory", "PRODUCTS"). Look at the columns of each table to pick the right one for the question.
8. If the user refers to previous context (e.g. "filter them by Karachi", "show that as a chart", "what about their salary"), use the conversation history to understand what was being analyzed.
"""

def _get_synthesizer_prompt() -> str:
    company = _get_app_config().get("company_name", "Enterprise AI")
    return f"""You are an elite corporate executive data analyst for {company}.
You have access to live, authorized enterprise data sources.

YOUR INSTRUCTIONS:
1. EXECUTIVE TAKEAWAY: When answering queries about data, start with 1 to 2 crisp, high-impact executive takeaway sentences summarizing the core finding, top contributor, or primary trend. Always use descriptive names, never raw numeric IDs.
2. GENERAL / CATALOG QUESTIONS: If the user asks what tables exist, how many tables you have access to, or general platform capabilities, provide an articulate, professional summary of your authorized data sources, their row counts, and what analysis can be performed.
3. KEY HIGHLIGHTS & KPIS: Present clear, bold bullet points for key figures (totals, top performers, percentages of total, averages).
4. MATH TRUTH: The Markdown table and numbers provided to you are 100% mathematically verified. You MUST use these exact figures. Never modify or invent numbers.
5. STRATEGIC BUSINESS INSIGHT: Provide 2 to 3 concise bullet points analyzing business implications when relevant.
6. ECHARTS: If a chart JSON is provided in the context, output it wrapped in ```json ``` code block after the table.
7. NO CSV COPY INSTRUCTIONS: Never output raw CSV blocks or write instructions like "Copy the CSV text above into a plain-text file". The user already gets automatic download buttons.
8. NO TUTORIALS OR PRINT INSTRUCTIONS: Never output instructions on how to obtain, print, or export files.
9. NO SLIDE OUTLINES OR META TABLES: Never write presentation outlines, placeholders, or meta-labels.
10. CONVERSATION CONTINUITY: If previous conversation messages are provided, maintain full conversational awareness and seamlessly answer follow-ups.
11. LANGUAGE: Reply in the language the user asked in (English, Urdu script, or Roman Urdu).
12. Never mention Python, SQL, the planner, or backend steps."""

SYNTHESIZER_SYSTEM_PROMPT = _get_synthesizer_prompt()

def _call_llm(provider: dict, messages: list, max_tokens: int = 1500, temperature: float = 0.0) -> Tuple[Optional[str], dict, dict]:
    """Calls Groq / OpenRouter. Returns (content, usage_dict, rate_limit_headers)."""
    headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
    payload = {
        "model": provider["model_name"],
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens
    }
    t0 = time.time()
    res = requests.post(provider["llm_api_url"], headers=headers, json=payload, timeout=60)
    latency_ms = int((time.time() - t0) * 1000)
    res.raise_for_status()
    data = res.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    rl_headers = {k.lower(): v for k, v in res.headers.items() if k.lower().startswith("x-ratelimit-")}
    usage["latency_ms"] = latency_ms
    return content, usage, rl_headers

def _run_chat(req: ChatRequest, cancelled: threading.Event, emit):
    t_start = time.time()
    emit("Loading authorized company data…")
    datasets, endpoints_meta = _get_user_datasets(req.user_id, emit)

    if not datasets or all(len(rows) == 0 for rows in datasets.values()):
        return {
            "answer": "No authorized data sources were found for your user account. Please check user permissions in Oracle APEX.",
            "provider_used": "System",
            "meta_stats": {"tokens": 0, "ms": int((time.time() - t_start) * 1000), "cost": 0}
        }

    # Load recent conversation history if 'remember' is enabled
    history_msgs = []
    if req.remember:
        try:
            h_data = ords_get(f"{URL_MESSAGES}/{req.session_id}", timeout=5).json()
            items = h_data.get("items", []) if isinstance(h_data, dict) else []
            # Keep up to last 8 messages (4 user-AI turns) for conversational continuity
            for m in items[-8:]:
                role = "user" if (m.get("sender_role") or "").lower() == "user" else "assistant"
                content = str(m.get("content") or "").strip()
                if not content:
                    continue
                # Clean file export tags from previous assistant responses to save prompt space
                if role == "assistant":
                    content = re.sub(r"\[\[EXPORT:\w+\]\]", "", content).strip()
                    if len(content) > 500:
                        content = content[:500] + "..."
                history_msgs.append({"role": role, "content": content})
        except Exception:
            history_msgs = []

    # Fast path: instant file dump skips LLM entirely (0 tokens, 0ms latency)
    asked = _asked_kinds(req.question)
    if asked and _is_plain_dump(req.question):
        emit("Preparing your file export directly…")
        # Match best dataset
        best_name = _match_dataset(req.question, datasets) or list(datasets.keys())[0]
        rows = datasets.get(best_name, [])
        answer = _dump_answer(best_name, rows, asked)
        answer = _force_markers(req.question, answer)
        _save_and_record(req, answer, t_start)
        return {
            "answer": answer,
            "provider_used": "Direct Export Engine (0 tokens)",
            "kpis": qe.compute_kpis(rows),
            "meta_stats": {"tokens": 0, "ms": int((time.time() - t_start) * 1000), "cost": 0}
        }

    # Load providers
    try:
        providers = ords_get(URL_PROVIDERS, timeout=10).json().get("items", [])
    except Exception:
        providers = []
    if not providers:
        raise HTTPException(status_code=500, detail="No active AI providers configured in Oracle APEX.")

    if req.provider_id is not None:
        providers.sort(key=lambda x: str(x.get("provider_id")) != str(req.provider_id))

    # STAGE 1: Small Compact Schema (~400 tokens)
    schema_overview = _build_compact_schema(datasets, endpoints_meta)

    # STAGE 2: Call Planner LLM
    emit("Understanding question and planning query…")
    plan = None
    provider_used = None
    total_tokens_used = 0
    total_cost = 0.0

    planner_messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT.replace("{schema}", schema_overview)}
    ]
    planner_messages.extend(history_msgs)
    planner_messages.append({"role": "user", "content": req.question})

    for provider in providers:
        if cancelled.is_set(): return None
        try:
            p_content, p_usage, p_rl = _call_llm(
                provider,
                messages=planner_messages,
                max_tokens=600,
                temperature=0.0
            )
            provider_used = provider["provider_name"]
            p_tok = p_usage.get("total_tokens", 0)
            total_tokens_used += p_tok
            log_usage(
                user_id=req.user_id, session_id=req.session_id, provider_id=provider.get("provider_id"),
                model_name=provider.get("model_name"), call_type="PLAN", status="OK",
                prompt_tokens=p_usage.get("prompt_tokens", 0), completion_tokens=p_usage.get("completion_tokens", 0),
                total_tokens=p_tok, latency_ms=p_usage.get("latency_ms", 0), rl_headers=p_rl,
                question_preview=req.question
            )
            # Extract JSON plan
            m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", p_content)
            raw_json = m.group(1) if m else p_content.strip()
            plan = json.loads(raw_json)
            break
        except Exception as e:
            emit(f"{provider['provider_name']} planner failed — trying fallback…")
            continue

    # STAGE 3: Execute Plan in Python (Exact numbers, 0 tokens)
    exec_result = None
    target_data = []
    if plan and plan.get("mode") != "direct_answer":
        emit("Computing exact numbers with in-memory query engine…")
        target_table = plan.get("target_table")
        matched_name = None
        if target_table and target_table in datasets:
            matched_name = target_table
        else:
            matched_name = _match_dataset(str(target_table or ""), datasets)
            if not matched_name:
                matched_name = _match_dataset(req.question, datasets)
            if not matched_name:
                matched_name = list(datasets.keys())[0]
        target_data = datasets[matched_name]
        try:
            exec_result = qe.execute_plan(target_data, plan)
        except Exception as e:
            print(f"[Query Engine Error] {e}")
            exec_result = None

    # STAGE 4: Synthesis LLM (Writes crisp summary from small result table)
    emit("Synthesizing executive summary & visualization…")
    if exec_result and exec_result.get("table_rows"):
        table_md = exec_result["table_md"]
        kpis_str = ", ".join(f"{k['label']}: {k['value']} ({k.get('sub','')})" for k in exec_result.get("kpis", []))
        chart_json_str = json.dumps(exec_result["chart"], indent=2) if exec_result.get("chart") else ""
        synth_user_prompt = (
            f"User Question: {req.question}\n\n"
            f"Exact Verified Results Table:\n{table_md}\n\n"
            f"Computed Key Metrics:\n{kpis_str}\n\n"
        )
        if chart_json_str:
            synth_user_prompt += f"Verified Chart ECharts Config:\n```json\n{chart_json_str}\n```\n"
    else:
        # Fallback to direct dump or general answer if planner didn't match an aggregation plan
        matched_k = _match_dataset(req.question, datasets)
        if matched_k:
            synth_user_prompt = (
                f"User Question: {req.question}\n\n"
                f"Authorized Schema Overview:\n{schema_overview}\n\n"
                f"Data Preview for {matched_k}:\n{qe.rows_to_md_table(datasets[matched_k][:50])}"
            )
        else:
            previews = []
            for name, rws in datasets.items():
                previews.append(f"### Table: {name} ({len(rws)} records)\n{qe.rows_to_md_table(rws[:15])}")
            all_preview_str = "\n\n".join(previews)
            synth_user_prompt = (
                f"User Question: {req.question}\n\n"
                f"Authorized Schema Overview:\n{schema_overview}\n\n"
                f"Data Previews Across All Available Tables:\n{all_preview_str}"
            )

    answer = None
    synth_messages = [
        {"role": "system", "content": _get_synthesizer_prompt()}
    ]
    synth_messages.extend(history_msgs)
    synth_messages.append({"role": "user", "content": synth_user_prompt})

    for provider in providers:
        if cancelled.is_set(): return None
        try:
            s_content, s_usage, s_rl = _call_llm(
                provider,
                messages=synth_messages,
                max_tokens=2500,
                temperature=0.1
            )
            provider_used = provider["provider_name"]
            s_tok = s_usage.get("total_tokens", 0)
            total_tokens_used += s_tok
            log_usage(
                user_id=req.user_id, session_id=req.session_id, provider_id=provider.get("provider_id"),
                model_name=provider.get("model_name"), call_type="ANSWER", status="OK",
                prompt_tokens=s_usage.get("prompt_tokens", 0), completion_tokens=s_usage.get("completion_tokens", 0),
                total_tokens=s_tok, latency_ms=s_usage.get("latency_ms", 0), rl_headers=s_rl,
                question_preview=req.question
            )
            answer = s_content
            break
        except Exception as e:
            continue

    if not answer:
        # Graceful fallback: return the raw table generated by the query engine
        if exec_result and exec_result.get("table_md"):
            answer = f"**Summary of results** ({exec_result['row_count']} records matching):\n\n{exec_result['table_md']}"
        else:
            raise HTTPException(status_code=500, detail="All AI providers failed. Please retry in a few moments.")
    elif exec_result and exec_result.get("table_md"):
        # If the LLM tried to generate a table and got cut off or missed rows,
        # replace any partial table with the complete mathematically verified table from Python
        if "| ---" in answer or "|\n|" in answer:
            # Strip whatever table or trailing partial table the LLM outputted
            cleaned_summary = re.sub(r"\n\|[^\n]*\n\|[\s\S]*?(?=\n\n|\Z|```)", "", answer).strip()
            # If the LLM removed everything, keep the original intro
            if not cleaned_summary:
                cleaned_summary = answer.split("|")[0].strip()
            # Append the full exact table verified from query engine
            answer = f"{cleaned_summary}\n\n{exec_result['table_md']}"

    answer = _force_markers(req.question, answer)

    if cancelled.is_set(): return None

    # Save chat & update message times
    emit("Saving conversation…")
    _save_and_record(req, answer, t_start, provider_used, total_tokens_used)

    kpis_out = exec_result.get("kpis") if exec_result else []
    if not kpis_out and re.search(r"\b(how many|which|what|list)\b.{0,25}\b(tables?|sources?|endpoints?)\b", req.question.lower()):
        kpis_out = [
            {"label": "Authorized Tables", "value": str(len(datasets)), "sub": ", ".join(datasets.keys())},
            {"label": "Total Records", "value": f"{sum(len(r) for r in datasets.values()):,}", "sub": "across all tables"}
        ]

    return {
        "answer": answer,
        "provider_used": provider_used,
        "kpis": kpis_out,
        "meta_stats": {
            "tokens": total_tokens_used,
            "ms": int((time.time() - t_start) * 1000)
        }
    }

def _save_and_record(req: ChatRequest, answer: str, t_start: float, provider_used: str = None, total_tokens: int = 0):
    if req.keep_count is not None:
        try:
            ords_post(URL_TRUNCATE, json={"user_id": req.user_id, "session_id": req.session_id, "keep_count": req.keep_count}, timeout=5)
        except Exception:
            pass

    title = req.question[:30] + "..." if len(req.question) > 30 else req.question
    try:
        ords_post(URL_SAVE_CHAT, json={
            "session_id": req.session_id, "user_id": req.user_id,
            "session_title": title, "user_msg": req.question, "ai_msg": answer,
            "provider_name": provider_used, "total_tokens": total_tokens,
            "latency_ms": int((time.time() - t_start) * 1000)
        })
    except Exception:
        pass

# ---------------- Chat Task Runner & Background Workers ----------------
_jobs: dict = {}

class StopRequest(BaseModel):
    session_id: str

def _prune_jobs():
    now = time.time()
    for k in [k for k, j in _jobs.items() if not j["running"] and now - j["t0"] > 900]:
        _jobs.pop(k, None)

@app.post("/api/chat")
async def ask_assistant(req: ChatRequest, request: Request):
    req.user_id = current_user(request)["u"]
    prev = _jobs.get(req.session_id)
    if prev and prev["running"] and prev["uid"] == req.user_id:
        prev["cancel"].set()
    cancelled = threading.Event()
    job = {"uid": req.user_id, "cancel": cancelled, "question": req.question, "status": "Thinking…",
           "running": True, "t0": time.time(), "error": None}
    _jobs[req.session_id] = job
    _prune_jobs()
    q: "queue.Queue[dict]" = queue.Queue()

    def emit(m):
        job["status"] = m
        q.put({"status": m})

    def worker():
        try:
            result = _run_chat(req, cancelled, emit)
            q.put({"done": True, **(result or {"cancelled": True})})
        except HTTPException as e:
            job["error"] = str(e.detail)
            q.put({"done": True, "error": e.detail})
        except Exception as e:
            job["error"] = f"Unexpected error: {e}"
            q.put({"done": True, "error": job["error"]})
        finally:
            job["running"] = False

    threading.Thread(target=worker, daemon=True).start()

    async def stream():
        while True:
            try:
                ev = q.get_nowait()
            except queue.Empty:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.12)
                continue
            yield json.dumps(ev) + "\n"
            if ev.get("done"):
                return

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/chat/status/{session_id}")
def chat_status(session_id: str, request: Request):
    uid = current_user(request)["u"]
    j = _jobs.get(session_id)
    if not j or j["uid"] != uid:
        return {"running": False}
    running = j["running"] and not j["cancel"].is_set()
    out = {"running": running, "question": j["question"], "status": j["status"],
           "elapsed": int(time.time() - j["t0"]), "error": None}
    if not running:
        out["error"] = j["error"]
        if not j["running"]:
            _jobs.pop(session_id, None)
    return out

@app.get("/api/chat/running")
def chat_running(request: Request):
    uid = current_user(request)["u"]
    now = time.time()
    return {"items": [{"session_id": sid, "question": j["question"], "status": j["status"], "elapsed": int(now - j["t0"])}
                      for sid, j in list(_jobs.items()) if j["uid"] == uid and j["running"] and not j["cancel"].is_set()]}

@app.post("/api/chat/stop")
def chat_stop(body: StopRequest, request: Request):
    uid = current_user(request)["u"]
    j = _jobs.get(body.session_id)
    if j and j["uid"] == uid:
        j["cancel"].set()
    return {"status": "success"}

# ---------------- Audio Transcription (Whisper) ----------------
@app.post("/api/transcribe")
def transcribe(request: Request, audio: UploadFile = File(...), language: str = Form("")):
    current_user(request)
    try:
        providers = ords_get(URL_PROVIDERS, timeout=10).json().get("items", [])
    except Exception:
        providers = []
    raw = audio.file.read()
    if len(raw) > 24 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Recording is too long.")

    last = "No speech-to-text provider configured."
    for prov in providers:
        url = prov.get("llm_api_url", "")
        model = "whisper-large-v3" if "groq.com" in url else "whisper-1" if "api.openai.com" in url else None
        if not model:
            continue
        data = {"model": model, "response_format": "verbose_json", "temperature": "0"}
        if language in ("ur", "en"):
            data["language"] = language
        try:
            r = requests.post(url.replace("/chat/completions", "/audio/transcriptions"),
                              headers={"Authorization": f"Bearer {prov['api_key']}"},
                              files={"file": (audio.filename or "voice.webm", raw, audio.content_type or "audio/webm")},
                              data=data, timeout=60)
            if r.ok:
                return {"text": (r.json().get("text") or "").strip()}
            last = f"Whisper failed ({r.status_code})"
        except Exception as e:
            last = f"Speech error: {e}"
    raise HTTPException(status_code=502, detail=last)

@app.get("/SUMMARY_DOCUMENTATION.md", response_class=FileResponse)
@app.get("/api/docs/summary", response_class=FileResponse)
def serve_doc_md():
    if os.path.exists("SUMMARY_DOCUMENTATION.md"):
        return FileResponse("SUMMARY_DOCUMENTATION.md", media_type="text/markdown", filename="Enterprise_AI_Architecture_Summary.md")
    raise HTTPException(status_code=404, detail="Documentation not found")

@app.get("/", response_class=FileResponse)
@app.get("/ai", response_class=FileResponse)
@app.get("/ai/", response_class=FileResponse)
def serve_ui():
    return FileResponse("index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ui_app:app", host="0.0.0.0", port=8000, reload=True)