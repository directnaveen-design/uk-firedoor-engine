import os
import io
import re
import json
import hmac
import math
import base64
import asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel
import httpx
from pypdf import PdfReader, PdfWriter
from catalog import PARTS_CATALOG, catalog_prompt_summary
from generators import generate_excel_pack, generate_prework_pdf

app = FastAPI(title="UK Fire Door Zero-Touch Engine")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]
GEMINI_PAID_API_KEY = os.getenv("GEMINI_PAID_API_KEY", "").strip()
_extra_gemini_csv = [k.strip() for k in os.getenv("GEMINI_BACKUP_API_KEYS", "").split(",") if k.strip()]
_extra_gemini_numbered = [
    os.getenv(f"GEMINI_BACKUP_API_KEY_{i}", "").strip()
    for i in range(1, 11)
]
GEMINI_API_KEYS = list(dict.fromkeys(
    k for k in ([GEMINI_PAID_API_KEY] + _extra_gemini_numbered + _extra_gemini_csv) if k
))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
_default_nvidia_models = [
    "mistralai/mistral-large-4-1t-a52b",
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "google/gemma-4-31b-it",
    "deepseek-ai/deepseek-v4.1-flash",
    "moonshotai/kimi-k3.1-pro",
]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

NVIDIA_MODELS = [m.strip() for m in os.getenv("NVIDIA_MODELS", "").split(",") if m.strip()] or _default_nvidia_models

N8N_CALLBACK_URL = os.environ["N8N_CALLBACK_URL"]
WEBHOOK_SECRET = os.environ["INTERNAL_WEBHOOK_SECRET"]


class ProcessJobRequest(BaseModel):
    job_id: str
    gmail_thread_id: str = ""
    sender_email: str = ""
    client_name: str = "UK Fire Contractor"
    building_name: str = "Commercial Site"
    hourly_rate: float = 55.0
    material_markup: float = 0.15
    site_fee: float = 0.0
    vat_rate: float = 0.20
    standard_version: str = "BS 8214:2016"
    raw_pdf_path: str
    email_text: str = ""


async def send_tg(client: httpx.AsyncClient, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:3800]},
            timeout=10.0,
        )
    except Exception:
        pass


def sb_headers(content_type: str = "application/json") -> dict:
    h = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def safe_error_summary(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__}: HTTP {exc.response.status_code}"
    if isinstance(exc, ValueError) and str(exc).startswith("ZeroDoorsError:"):
        return str(exc)
    return f"{type(exc).__name__}: {str(exc)[:200]}"


async def update_job_status(client: httpx.AsyncClient, job_id: str, payload: dict):
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.patch(
        f"{SUPABASE_URL}/rest/v1/compliance_jobs?job_id=eq.{job_id}",
        headers=sb_headers(),
        json=payload
    )


def split_pdf_into_chunks(pdf_bytes: bytes, pages_per_chunk: int = 20) -> tuple[int, list[tuple[int, bytes]]]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    chunks = []
    for start in range(0, total_pages, pages_per_chunk):
        writer = PdfWriter()
        for p in range(start, min(start + pages_per_chunk, total_pages)):
            writer.add_page(reader.pages[p])
        buf = io.BytesIO()
        writer.write(buf)
        chunks.append((start + 1, buf.getvalue()))
    return total_pages, chunks


def parse_llm_json(raw_text: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text or "", flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(cleaned[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM output is not a JSON object")
    return parsed


def extract_text_from_chunk(chunk_bytes: bytes, start_page: int) -> str:
    reader = PdfReader(io.BytesIO(chunk_bytes))
    pages_txt = []
    for idx, page in enumerate(reader.pages):
        txt = (page.extract_text() or "").strip()
        if txt:
            pages_txt.append(f"--- PAGE {start_page + idx} ---\n{txt}")
    return "\n\n".join(pages_txt)


async def call_nvidia_json(client: httpx.AsyncClient, prompt_text: str, timeout: float = 180.0) -> tuple[dict, str]:
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY is not configured")
    last_exc = None
    for model_id in NVIDIA_MODELS:
        for attempt in range(2):
            try:
                resp = await client.post(
                    NVIDIA_BASE_URL,
                    headers={
                        "Authorization": f"Bearer {NVIDIA_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_id,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a strict JSON extraction API. Return ONLY a single valid JSON object matching the requested schema. No markdown fences, no commentary.",
                            },
                            {"role": "user", "content": prompt_text},
                        ],
                        "temperature": 0.0,
                        "max_tokens": 16384,
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    raise RuntimeError(f"Empty choices from {model_id}")
                raw_text = (choices[0].get("message") or {}).get("content") or ""
                if not raw_text.strip():
                    raise RuntimeError(f"Empty content from {model_id}")
                return parse_llm_json(raw_text), f"NVIDIA:{model_id}"
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError, json.JSONDecodeError, KeyError, IndexError, RuntimeError, ValueError) as exc:
                last_exc = exc
                if attempt == 0 and (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code in (429, 500, 502, 503, 504)
                ):
                    await asyncio.sleep(4)
                    continue
                break
    raise last_exc or RuntimeError("All NVIDIA models failed")


async def call_gemini_json(client: httpx.AsyncClient, parts: list, timeout: float = 180.0) -> tuple[dict, str]:
    if not GEMINI_API_KEYS:
        raise RuntimeError("No GEMINI API keys configured")
    models = list(dict.fromkeys([GEMINI_MODEL, "gemini-3.5-flash-lite", "gemini-3.8-flash"]))
    last_exc = None
    for key_idx, api_key in enumerate(GEMINI_API_KEYS, 1):
        for model_id in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
            for attempt in range(2):
                try:
                    resp = await client.post(
                        url,
                        headers={
                            "x-goog-api-key": api_key,
                            "Content-Type": "application/json",
                        },
                        json={
                            "contents": [{"parts": parts}],
                            "generationConfig": {
                                "temperature": 0.0,
                                "responseMimeType": "application/json",
                            },
                        },
                        timeout=timeout,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    candidates = data.get("candidates") or []
                    if not candidates:
                        raise RuntimeError("Empty Gemini candidates")
                    finish_reason = candidates[0].get("finishReason")
                    if finish_reason != "STOP":
                        raise RuntimeError(f"Unexpected finishReason: {finish_reason}")
                    raw_text = candidates[0]["content"]["parts"][0]["text"]
                    return parse_llm_json(raw_text), f"Gemini:{model_id}(key#{key_idx})"
                except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError, json.JSONDecodeError, KeyError, IndexError, RuntimeError, ValueError) as exc:
                    last_exc = exc
                    if attempt == 0 and (
                        not isinstance(exc, httpx.HTTPStatusError)
                        or exc.response.status_code in (429, 500, 502, 503, 504)
                    ):
                        await asyncio.sleep(5)
                        continue
                    break
    raise last_exc or RuntimeError("All Gemini fallback keys/models failed")


async def extract_commercial_from_email(client: httpx.AsyncClient, job: ProcessJobRequest) -> dict:
    defaults = {
        "hourly_rate": job.hourly_rate,
        "material_markup": job.material_markup,
        "setup_fee_gbp": job.site_fee,
        "vat_rate": job.vat_rate,
    }
    if not job.email_text.strip():
        return defaults

    prompt = (
        "Extract commercial pricing parameters from this UK contractor email. Return null for any value not explicitly stated.\n"
        "Return strict JSON: "
        '{"hourly_rate": <float or null>, "material_markup": <float decimal e.g. 0.125 for 12.5% or null>, '
        '"setup_fee_gbp": <float or null>, "vat_rate": <float decimal e.g. 0.20 for 20% or null>}\n\n'
        f"EMAIL TEXT:\n{job.email_text[:4000]}"
    )
    try:
        if NVIDIA_API_KEY:
            try:
                parsed, _ = await call_nvidia_json(client, prompt, timeout=60.0)
            except Exception:
                parsed, _ = await call_gemini_json(client, [{"text": prompt}], timeout=60.0)
        else:
            parsed, _ = await call_gemini_json(client, [{"text": prompt}], timeout=60.0)
        return {
            "hourly_rate": safe_float(parsed.get("hourly_rate"), job.hourly_rate) if parsed.get("hourly_rate") is not None else job.hourly_rate,
            "material_markup": safe_float(parsed.get("material_markup"), job.material_markup) if parsed.get("material_markup") is not None else job.material_markup,
            "setup_fee_gbp": safe_float(parsed.get("setup_fee_gbp"), job.site_fee) if parsed.get("setup_fee_gbp") is not None else job.site_fee,
            "vat_rate": safe_float(parsed.get("vat_rate"), job.vat_rate) if parsed.get("vat_rate") is not None else job.vat_rate,
        }
    except Exception:
        return defaults


async def extract_chunk_with_gemini(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    start_page: int,
    chunk_bytes: bytes,
    is_first_chunk: bool
) -> tuple[dict, str]:
    catalog_text = catalog_prompt_summary()
    job_level_rule = (
        "Also extract job-level metadata from the header/cover: "
        '"end_client_name" (the Client organization named in the PDF header), "contractor_company", "site_name" (strip leading dashes/spaces), '
        '"site_address", "survey_date", "survey_standard" (exact BS standard printed in header e.g. "BS 8214:2016" or "BS 8214:2026" or null), '
        '"total_doors_declared" (integer or null).'
        if is_first_chunk else
        'Set job-level metadata fields to null.'
    )

    prompt = (
        f"You are a UK Fire Door Inspection data extractor. These PDF pages start at original page {start_page}. "
        "Extract ALL doors visible in these pages. Never invent data and NEVER alter noun types (e.g. never change 'signs' into 'locks').\n"
        "RULES:\n"
        "1. door_id: keep exact printed ID (e.g. 'CCH-02-05 to CCH-02-07', 'CCH-03-10/A & B'). Do NOT include '(x3 Doors)' or '(Double Door)' inside door_id if avoidable.\n"
        "2. rating: keep exact printed fire rating including trailing S (e.g. FD30, FD30S, FD60, FD60S, FD90, FD90S, FD120, FD120S). Never drop 'S'.\n"
        "3. status: strictly one of 'pass', 'fail', 'no_access', or 'excluded' (use 'excluded' for ON HOLD, ASBESTOS, AIB, or M&E / Fire Alarm ONLY doors).\n"
        "4. priority_text: survey's own priority words (e.g. 'CRITICAL - P1', 'HIGH - P1', 'MEDIUM - P2', 'REPLACEMENT'), else null.\n"
        "5. est_time_minutes: integer TOTAL minutes for the row as printed (e.g. '2 hrs 15 mins' = 135, '4 hrs 30 mins' = 270, '1 hr 45 mins' = 105, '45 mins per door (2 hrs 15 mins total)' = 135), else null.\n"
        "6. client_supplied: true ONLY if the *entire* row has zero contractor-supplied parts. If a door has a client-supplied closer PLUS contractor-supplied drop seal and hinge pads, set client_supplied=false and put ONLY the contractor-supplied items in 'materials' (do NOT include the client-supplied closer in 'materials').\n"
        "7. materials (CRITICAL — MULTI-PART LIST): return a JSON array of ALL contractor-supplied materials required for this row, using exact codes from the CATALOGUE below. Do NOT drop secondary items like closers, hinge pads, drop seals, or signs!\n"
        "   - EXACT DIMENSION MATCH: 915mm drop-down seal MUST map to DROP_SEAL_915 (NEVER DROP_SEAL_838). 838mm MUST map to DROP_SEAL_838. 1000mm MUST map to DROP_SEAL_1000.\n"
        "   - INTUMESCENT STRIP TRADE LENGTHS: Prefer INTUM_STRIP_15X4_2100 or INTUM_STRIP_20X4_2100 (2.1m lengths). Standard single door (5.2m) = 3 lengths per door (so 3 doors = 9 lengths total).\n"
        "   - FULL DOORSET / BER RULE: If required_works specifies BEYOND ECONOMIC REPAIR / full certified doorset replacement (e.g. FD60S single doorset), include ONLY the matching doorset code (DOORSET_FD60S_SGL, DOORSET_FD30S_SGL, or DOORSET_FD60S_DBL, qty 1) — do NOT list loose hinges, closer, seals, or signs separately.\n"
        "   - If a required contractor-supplied item has NO matching code in the catalogue, use part_code 'UNMAPPED' with its qty.\n"
        f"CATALOGUE:\n{catalog_text}\n\n"
        f"{job_level_rule}\n"
        "Return strict JSON:\n"
        '{"end_client_name": "...", "contractor_company": "...", "site_name": "...", "site_address": "...", '
        '"survey_date": "...", "survey_standard": "... or null", "total_doors_declared": <int or null>, '
        '"doors": [{"door_id": "...", "floor": "...", "rating": "...", "status": "pass|fail|no_access|excluded", '
        '"findings": "...", "required_works": "...", "priority_text": "... or null", '
        '"est_time_minutes": <int or null>, "client_supplied": false, '
        '"materials": [{"part_code": "...", "qty": <int>}], "page": <int>}]}'
    )

    chunk_text = extract_text_from_chunk(chunk_bytes, start_page)
    async with sem:
        if NVIDIA_API_KEY and len(chunk_text) >= 40:
            try:
                return await call_nvidia_json(
                    client,
                    f"{prompt}\n\nPDF SURVEY TEXT:\n{chunk_text}",
                    timeout=180.0,
                )
            except Exception:
                if not GEMINI_API_KEYS:
                    raise

        if GEMINI_API_KEYS:
            b64_pdf = base64.b64encode(chunk_bytes).decode("utf-8")
            return await call_gemini_json(
                client,
                [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "application/pdf", "data": b64_pdf}},
                ],
                timeout=180.0,
            )

        raise ValueError("ScannedPDFError: No selectable text found in PDF and no vision fallback key configured.")


def safe_int(val, default: int | None = 0) -> int | None:
    if val is None:
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def norm_key(val) -> str:
    return " ".join(str(val or "").lower().split())


def split_grouped_door_id(raw_id: str) -> list[str]:
    clean = str(raw_id or "").strip()
    if not clean:
        return ["Unknown"]

    # Strip trailing parenthetical annotations like "(x3 Doors)", "(Double Door)", "(Pair)"
    clean = re.sub(r"\s*\([^)]*\)\s*$", "", clean).strip()
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        return ["Unknown"]

    # Pattern 1: "CCH-02-05 to CCH-02-07" or "FD-01-04 to 06"
    m_range = re.match(r"^(.*?)(\d+)\s+to\s+(?:[A-Za-z0-9\-]*?[-/])?(\d+)$", clean, flags=re.IGNORECASE)
    if m_range:
        prefix, start_s, end_s = m_range.group(1), m_range.group(2), m_range.group(3)
        start_n, end_n = int(start_s), int(end_s)
        width = len(start_s)
        if 0 < (end_n - start_n) <= 30:
            return [f"{prefix}{str(i).zfill(width)}" for i in range(start_n, end_n + 1)]

    # Pattern 2: "CCH-03-10/A & B" or "FD-22-010/A-B"
    m_slash_pair = re.match(r"^(.*?/)([A-Za-z])\s*(?:&|-|and)\s*([A-Za-z])$", clean, flags=re.IGNORECASE)
    if m_slash_pair:
        base_p, c1, c2 = m_slash_pair.group(1), m_slash_pair.group(2).upper(), m_slash_pair.group(3).upper()
        return [f"{base_p}{c1}", f"{base_p}{c2}"]

    # Pattern 3: Comma or ampersand separated e.g. "FD-12-005A, 005B & 006" or "FD-02-08 & #09"
    if "&" in clean or "," in clean:
        parts = [p.strip().lstrip("#").strip() for p in re.split(r"[&,]", clean) if p.strip()]
        if len(parts) > 1:
            first = parts[0]
            m_slash = re.match(r"^(.*?/)([A-Za-z0-9]+)$", first)
            m_prefix = re.match(r"^([A-Za-z]+-\d+-)(\d+[A-Za-z]?)$", first) or re.match(r"^(.*?)(\d+[A-Za-z]?)$", first)
            expanded = [first]
            for p in parts[1:]:
                if m_slash and len(p) <= 2 and "/" not in p:
                    expanded.append(f"{m_slash.group(1)}{p}")
                elif m_prefix and not re.match(r"^[A-Za-z]{2,}-", p):
                    expanded.append(f"{m_prefix.group(1)}{p}")
                else:
                    expanded.append(p)
            return expanded

    return [clean.replace("#", "").strip()]


def map_priority_deterministic(priority_text, findings: str = "", req_works: str = "") -> tuple[str, str]:
    combined = f"{priority_text or ''} {findings} {req_works}".strip().lower()
    if any(k in combined for k in ("critical", "p0", "immediate", "beyond economic repair", "doorset replacement", "full replacement")):
        return "Critical (P0)", ""
    if any(k in combined for k in ("high", "urgent", "p1", "replacement")):
        return "High (P1)", ""
    if any(k in combined for k in ("medium", "standard", "p2")):
        return "Medium (P2)", ""
    if any(k in combined for k in ("low", "advisory", "p3")):
        return "Low (P3)", ""
    return "Medium (P2)", "priority not stated"


def is_excluded_door(status: str, findings: str, req_works: str) -> bool:
    combined = f"{status} {findings} {req_works}".lower()
    if status == "excluded":
        return True
    if any(k in combined for k in ("on hold", "asbestos", "aib", "m&e only", "referred to m&e", "excluded from active", "ipc hold", "barrier nursing", "isolation room", "decommissioned")):
        return True
    return False


def distribute_qty(total_qty: int, count: int) -> list[int]:
    if count <= 1:
        return [total_qty]
    if total_qty >= count:
        base_q = total_qty // count
        rem_q = total_qty % count
        return [base_q + (1 if idx < rem_q else 0) for idx in range(count)]
    # Shared single item on a pair (e.g. 1 astragal seal across Leaf A & Leaf B) -> assign to first leaf only
    return [total_qty] + [0] * (count - 1)


def normalize_raw_materials(d: dict, client_supplied: bool) -> list[dict]:
    if client_supplied:
        return []
    raw_mats = d.get("materials")
    items = []
    if isinstance(raw_mats, list) and len(raw_mats) > 0:
        for m in raw_mats:
            if not isinstance(m, dict):
                continue
            code = str(m.get("part_code") or "").strip().upper()
            qty = safe_int(m.get("qty"), 1)
            if not code or code == "NO_PARTS_REQ" or (qty is not None and qty <= 0):
                continue
            items.append({"part_code": code, "qty": qty or 1})
    else:
        # Backwards compatibility if LLM returns single part_code / qty
        code = str(d.get("part_code") or "").strip().upper()
        qty = safe_int(d.get("qty"), 1) or 1
        if code and code != "NO_PARTS_REQ":
            items.append({"part_code": code, "qty": qty})
    return items


def process_extracted_doors(raw_doors: list) -> tuple[list, list, list, list]:
    seen_keys = set()
    passed_doors = []
    no_access_doors = []
    excluded_doors = []
    failed_doors = []

    for d in raw_doors:
        if not isinstance(d, dict):
            continue
        raw_id = str(d.get("door_id") or "Unknown").strip()
        split_ids = split_grouped_door_id(raw_id)
        count = len(split_ids)

        est_total = safe_int(d.get("est_time_minutes"), None)
        if est_total is not None and est_total > 0:
            base_mins = est_total // count
            rem_mins = est_total % count
            mins_per_door = [base_mins + (1 if idx < rem_mins else 0) for idx in range(count)]
        else:
            mins_per_door = [None] * count

        status = str(d.get("status") or "fail").strip().lower()
        floor = str(d.get("floor") or "Unknown").strip() or "Unknown"
        rating = str(d.get("rating") or "Unknown").strip() or "Unknown"
        findings = str(d.get("findings") or "").strip() or "Unspecified finding"
        req_works = str(d.get("required_works") or "").strip() or findings
        prio, prio_note = map_priority_deterministic(d.get("priority_text"), findings, req_works)
        client_supplied = bool(d.get("client_supplied", False))
        page_num = safe_int(d.get("page"), 1) or 1

        excluded_flag = is_excluded_door(status, findings, req_works)
        raw_materials = normalize_raw_materials(d, client_supplied)

        # Pre-distribute each material's qty across split_ids
        per_door_mats: list[list[dict]] = [[] for _ in range(count)]
        for rm in raw_materials:
            q_dist = distribute_qty(rm["qty"], count)
            code = rm["part_code"]
            for idx in range(count):
                if q_dist[idx] <= 0:
                    continue
                if code in PARTS_CATALOG:
                    cat_item = PARTS_CATALOG[code]
                    per_door_mats[idx].append({
                        "part_code": code,
                        "part_needed": cat_item["description"],
                        "unit": cat_item["unit"],
                        "qty": q_dist[idx],
                        "unit_cost_gbp": float(cat_item["unit_price_gbp"]),
                        "line_total_gbp": round(q_dist[idx] * float(cat_item["unit_price_gbp"]), 2),
                        "is_unmapped": False,
                    })
                else:
                    per_door_mats[idx].append({
                        "part_code": "UNMAPPED",
                        "part_needed": f"UNMAPPED ({code})" if code and code != "UNMAPPED" else "UNMAPPED",
                        "unit": "Item",
                        "qty": q_dist[idx],
                        "unit_cost_gbp": 0.0,
                        "line_total_gbp": 0.0,
                        "is_unmapped": True,
                    })

        for idx, single_id in enumerate(split_ids):
            dedupe_key = f"{norm_key(single_id)}|{norm_key(floor)}|{norm_key(findings)}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            if "pass" in status and not excluded_flag:
                passed_doors.append({"door_id": single_id, "floor": floor, "rating": rating, "page": page_num})
                continue
            if ("no" in status and "access" in status) or "quarantine" in findings.lower():
                no_access_doors.append({"door_id": single_id, "floor": floor, "rating": rating, "findings": findings, "page": page_num})
                continue
            if excluded_flag:
                excluded_doors.append({
                    "door_id": single_id,
                    "floor": floor,
                    "rating": rating,
                    "reason": f"{findings} — {req_works}".strip(" —"),
                    "page": page_num
                })
                continue

            mats = per_door_mats[idx]
            door_has_unmapped = any(m["is_unmapped"] for m in mats)
            total_parts_gbp = round(sum(m["line_total_gbp"] for m in mats), 2)

            if client_supplied or not mats:
                primary_code = "NO_PARTS_REQ"
                primary_desc = "Client-Supplied (Labour Only)" if client_supplied else "No Additional Parts (Shared Leaf / Labour Only)"
                primary_qty = 0
                primary_unit_cost = 0.0
                materials_summary = primary_desc
            else:
                primary = mats[0]
                primary_code = primary["part_code"]
                primary_desc = primary["part_needed"]
                primary_qty = primary["qty"]
                primary_unit_cost = primary["unit_cost_gbp"]
                materials_summary = "; ".join(f"{m['qty']}x {m['part_needed']}" for m in mats)

            est_m = mins_per_door[idx]
            remedial_display = f"{req_works} ({prio_note})" if prio_note else req_works

            failed_doors.append({
                "door_id": single_id,
                "floor": floor,
                "rating": rating,
                "defect": findings,
                "remedial_action": remedial_display,
                "part_code": primary_code,
                "part_needed": primary_desc,
                "qty": primary_qty,
                "unit_cost_gbp": primary_unit_cost,
                "materials": mats,
                "materials_summary": materials_summary,
                "total_parts_gbp": total_parts_gbp,
                "labour_mins": est_m if est_m is not None else 0,
                "has_est_time": est_m is not None and est_m > 0,
                "is_unmapped": door_has_unmapped,
                "priority": prio,
                "source_page": page_num,
            })

    return failed_doors, passed_doors, no_access_doors, excluded_doors


def resolve_standard_label(survey_std: str | None, fallback_std: str) -> str:
    s = str(survey_std or "").strip()
    if s:
        if "8214:2016" in s:
            return "BS 8214:2016 (Superseded by BS 8214:2026)"
        return s
    return fallback_std or "BS 8214:2026"


async def execute_pipeline(job: ProcessJobRequest):
    stage = "INIT"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            stage = "SUPABASE_STATUS_PROCESSING"
            await update_job_status(client, job.job_id, {"status": "processing"})

            stage = "DOWNLOAD_PDF"
            dl_resp = await client.get(
                f"{SUPABASE_URL}/storage/v1/object/raw-surveys/{job.raw_pdf_path}",
                headers=sb_headers(content_type=""),
                timeout=60.0
            )
            dl_resp.raise_for_status()
            pdf_bytes = dl_resp.content

            stage = "PARSE_EMAIL_RATES"
            commercial = await extract_commercial_from_email(client, job)
            total_pages, chunks = split_pdf_into_chunks(pdf_bytes, pages_per_chunk=20)

            await send_tg(
                client,
                f"🚀 Job Started: {job.job_id}\n"
                f"From: {job.sender_email or 'Unknown'}\n"
                f"Site: {job.building_name}\n"
                f"PDF: {total_pages} page(s) ({len(chunks)} chunk)\n"
                f"Rates: £{commercial['hourly_rate']:.2f}/hr | Markup {commercial['material_markup']*100:.1f}% | Fee £{commercial['setup_fee_gbp']:.2f} | VAT {commercial['vat_rate']*100:.0f}%"
            )

            stage = "AI_EXTRACTION"
            sem = asyncio.Semaphore(3)
            chunk_outputs = await asyncio.gather(
                *(
                    extract_chunk_with_gemini(client, sem, start_p, c_bytes, idx == 0)
                    for idx, (start_p, c_bytes) in enumerate(chunks)
                )
            )
            chunk_results = [r for r, _ in chunk_outputs]
            models_used = sorted(set(m for _, m in chunk_outputs))

            first_chunk = chunk_results[0] if chunk_results else {}
            end_client_name = str(first_chunk.get("end_client_name") or "").strip() or job.client_name
            contractor_company = str(first_chunk.get("contractor_company") or "").strip() or job.client_name
            site_name = str(first_chunk.get("site_name") or job.building_name or "Commercial Site").lstrip("–-— ").strip()
            total_doors_declared = safe_int(first_chunk.get("total_doors_declared"), None)
            effective_standard = resolve_standard_label(first_chunk.get("survey_standard"), job.standard_version)

            all_raw_doors = []
            for r in chunk_results:
                all_raw_doors.extend(r.get("doors") or [])

            failed_doors, passed_doors, no_access_doors, excluded_doors = process_extracted_doors(all_raw_doors)
            total_surveyed = len(failed_doors) + len(passed_doors) + len(no_access_doors) + len(excluded_doors)

            if total_surveyed == 0:
                raise ValueError("ZeroDoorsError: No fire door inspection records were found in the uploaded PDF.")

            reasons = []
            unmapped_door_ids = [d["door_id"] for d in failed_doors if d["is_unmapped"]]
            if failed_doors:
                missing_time_count = sum(1 for d in failed_doors if not d["has_est_time"])
                if (missing_time_count / len(failed_doors)) > 0.30:
                    reasons.append(f"{missing_time_count}/{len(failed_doors)} failed doors missing est_time_minutes (>30%).")
                if unmapped_door_ids:
                    reasons.append(f"{len(unmapped_door_ids)} door(s) have UNMAPPED parts (£0.00): {', '.join(unmapped_door_ids[:10])}")
            if total_doors_declared is not None and total_surveyed != total_doors_declared:
                reasons.append(f"Extracted {total_surveyed} doors vs {total_doors_declared} declared in survey.")
            needs_review = len(reasons) > 0

            total_labour_hrs = round(sum(d["labour_mins"] for d in failed_doors) / 60.0, 2)
            total_base_mat = round(sum(d["total_parts_gbp"] for d in failed_doors), 2)

            b_info = {
                "contractor_company": contractor_company,
                "client_company": end_client_name,
                "building_name": site_name,
                "survey_date": datetime.now(timezone.utc).strftime("%d %b %Y"),
                "standard_version": effective_standard,
                "total_doors_surveyed": total_surveyed,
                "passed_count": len(passed_doors),
                "no_access_count": len(no_access_doors),
                "excluded_count": len(excluded_doors),
                "no_access_ids": [d["door_id"] for d in no_access_doors],
                "excluded_doors": excluded_doors,
                "unmapped_door_ids": unmapped_door_ids,
                "hourly_rate": commercial["hourly_rate"],
                "material_markup": commercial["material_markup"],
                "setup_fee_gbp": commercial["setup_fee_gbp"],
                "site_fee": commercial["setup_fee_gbp"],
                "vat_rate": commercial["vat_rate"],
            }

            stage = "GENERATE_EXCEL_PDF"
            xlsx_local = f"/tmp/{job.job_id}.xlsx"
            pdf_local = f"/tmp/{job.job_id}.pdf"
            generate_excel_pack(b_info, failed_doors, xlsx_local)
            generate_prework_pdf(b_info, failed_doors, pdf_local)

            stage = "UPLOAD_DELIVERABLES"
            xlsx_remote = f"{job.job_id}/UK_FireDoor_Compliance_Pack.xlsx"
            pdf_remote = f"{job.job_id}/UK_FireDoor_PreWork_Report.pdf"

            with open(xlsx_local, "rb") as f:
                up_x = await client.post(
                    f"{SUPABASE_URL}/storage/v1/object/deliverables/{xlsx_remote}",
                    headers=sb_headers("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                    content=f.read()
                )
                up_x.raise_for_status()

            with open(pdf_local, "rb") as f:
                up_p = await client.post(
                    f"{SUPABASE_URL}/storage/v1/object/deliverables/{pdf_remote}",
                    headers=sb_headers("application/pdf"),
                    content=f.read()
                )
                up_p.raise_for_status()

            stage = "UPDATE_DB_COMPLETED"
            await update_job_status(client, job.job_id, {
                "status": "completed",
                "xlsx_url": xlsx_remote,
                "pdf_url": pdf_remote,
                "extracted_doors_json": failed_doors
            })

            review_line = f"⚠️ REVIEW NEEDED: {'; '.join(reasons)}" if needs_review else "✅ Clean QA (0 unmapped)"
            await send_tg(
                client,
                f"📄 Files Ready ({job.job_id})\n"
                f"Site: {site_name} ({end_client_name})\n"
                f"Model: {', '.join(models_used)}\n"
                f"Doors: {total_surveyed} total | {len(failed_doors)} fail | {len(passed_doors)} pass | {len(no_access_doors)} no-access | {len(excluded_doors)} hold\n"
                f"Totals: {total_labour_hrs} hrs | Base Materials £{total_base_mat:.2f}\n"
                f"{review_line}\n"
                f"Calling Apps Script..."
            )

            stage = "APPS_SCRIPT_CALLBACK"
            cb_resp = await client.post(
                N8N_CALLBACK_URL,
                json={
                    "job_id": job.job_id,
                    "status": "completed",
                    "gmail_thread_id": job.gmail_thread_id,
                    "sender_email": job.sender_email,
                    "client_name": end_client_name,
                    "building_name": site_name,
                    "standard_version": effective_standard,
                    "total_surveyed": total_surveyed,
                    "failed_count": len(failed_doors),
                    "passed_count": len(passed_doors),
                    "no_access_count": len(no_access_doors),
                    "excluded_count": len(excluded_doors),
                    "unmapped_door_ids": unmapped_door_ids,
                    "needs_review": needs_review,
                    "reasons": reasons,
                    "xlsx_path": xlsx_remote,
                    "pdf_path": pdf_remote
                },
                timeout=60.0
            )
            cb_preview = (cb_resp.text or "").strip()[:300]
            if cb_resp.status_code >= 400 or any(k in cb_preview for k in ("Exception", "TypeError", "ReferenceError", "<!DOCTYPE html", "<html")):
                await send_tg(client, f"❌ Apps Script Callback Error ({cb_resp.status_code}) for {job.job_id}:\n{cb_preview}")
            else:
                await send_tg(client, f"📤 Callback OK ({cb_resp.status_code}) for {job.job_id}:\n{cb_preview}")

        except Exception as exc:
            err_msg = safe_error_summary(exc)
            await send_tg(
                client,
                f"🚨 Railway Failed [{stage}]\n"
                f"Job: {job.job_id}\n"
                f"From: {job.sender_email or 'Unknown'}\n"
                f"Site: {job.building_name}\n"
                f"Error: {err_msg[:400]}"
            )
            try:
                await update_job_status(client, job.job_id, {
                    "status": "failed",
                    "error_message": f"[{stage}] {err_msg}"[:500]
                })
            except Exception:
                pass
            try:
                await client.post(
                    N8N_CALLBACK_URL,
                    json={
                        "job_id": job.job_id,
                        "status": "failed",
                        "gmail_thread_id": job.gmail_thread_id,
                        "sender_email": job.sender_email,
                        "error": f"[{stage}] {err_msg}"[:300]
                    },
                    timeout=30.0
                )
            except Exception:
                pass


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/process-job", status_code=202)
async def start_job(
    job: ProcessJobRequest,
    bg: BackgroundTasks,
    x_webhook_secret: str = Header(None)
):
    if not hmac.compare_digest(x_webhook_secret or "", WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")
    bg.add_task(execute_pipeline, job)
    return {"status": "accepted", "job_id": job.job_id}
