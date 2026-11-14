import os
import io
import re
import json
import hmac
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
GEMINI_PAID_API_KEY = os.environ["GEMINI_PAID_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen")
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
    return type(exc).__name__


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


async def call_gemini_json(client: httpx.AsyncClient, parts: list, timeout: float = 180.0) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    backoff_waits = [5, 15, 30]
    last_exc = None

    for attempt in range(len(backoff_waits) + 1):
        try:
            resp = await client.post(
                url,
                headers={
                    "x-goog-api-key": GEMINI_PAID_API_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "contents": [{"parts": parts}],
                    "generationConfig": {
                        "temperature": 0.0,
                        "responseMimeType": "application/json"
                    }
                },
                timeout=timeout
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
            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                raise ValueError("Gemini output is not a JSON object")
            return parsed
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError, json.JSONDecodeError, KeyError, IndexError, RuntimeError, ValueError) as exc:
            last_exc = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500 and exc.response.status_code != 429:
                raise
            if attempt < len(backoff_waits):
                await asyncio.sleep(backoff_waits[attempt])
            else:
                raise last_exc


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
        parsed = await call_gemini_json(client, [{"text": prompt}], timeout=60.0)
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
) -> dict:
    b64_pdf = base64.b64encode(chunk_bytes).decode("utf-8")
    catalog_text = catalog_prompt_summary()
    job_level_rule = (
        "Also extract job-level metadata from the header/cover: "
        '"end_client_name", "contractor_company", "site_name" (strip leading dashes/spaces), '
        '"site_address", "survey_date", "total_doors_declared" (integer or null).'
        if is_first_chunk else
        'Set job-level metadata fields to null.'
    )

    prompt = (
        f"You are a UK Fire Door Inspection data extractor. These PDF pages start at original page {start_page}. "
        "Extract ALL doors visible in these pages (pass, fail, and no_access). Never invent data.\n"
        "RULES:\n"
        "1. door_id: keep exact printed ID (including grouped ranges like 'FD-01-04 to 06' or 'FD-02-08 & #09').\n"
        "2. rating: keep exact printed rating including trailing S (e.g. FD30, FD30S, FD60, FD60S, FD90, FD120).\n"
        "3. status: strictly one of 'pass', 'fail', or 'no_access'.\n"
        "4. priority_text: survey's own priority words (e.g. 'Critical Fail', 'High Priority'), else null.\n"
        "5. est_time_minutes: integer minutes if stated (e.g. '1 hr 30 mins' = 90, '4 hrs 15 mins' = 255, '2.25 hrs' = 135, '2.5 hrs' = 150), else null.\n"
        "6. client_supplied: true if hardware/parts are client-supplied or on-site, else false.\n"
        "7. part_code & qty: choose the best matching part_code from the catalogue below based on required_works, and integer qty per row. "
        "If client_supplied is true, use 'NO_PARTS_REQ' and qty 0.\n"
        f"CATALOGUE:\n{catalog_text}\n\n"
        f"{job_level_rule}\n"
        "Return strict JSON:\n"
        '{"end_client_name": "...", "contractor_company": "...", "site_name": "...", "site_address": "...", '
        '"survey_date": "...", "total_doors_declared": <int or null>, '
        '"doors": [{"door_id": "...", "floor": "...", "rating": "...", "status": "pass|fail|no_access", '
        '"findings": "...", "required_works": "...", "priority_text": "... or null", '
        '"est_time_minutes": <int or null>, "client_supplied": false, "part_code": "...", "qty": <int>, "page": <int>}]}'
    )
    async with sem:
        return await call_gemini_json(
            client,
            [
                {"text": prompt},
                {"inline_data": {"mime_type": "application/pdf", "data": b64_pdf}}
            ],
            timeout=180.0
        )


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

    # Pattern 1: "FD-01-04 to 06" or "FD-01-04 to FD-01-06"
    m_range = re.match(r"^(.*?)(\d+)\s*(?:to|-)\s*(?:[A-Za-z\-]*?)(\d+)$", clean, flags=re.IGNORECASE)
    if m_range and re.search(r"\bto\b", clean, flags=re.IGNORECASE):
        prefix, start_s, end_s = m_range.group(1), m_range.group(2), m_range.group(3)
        start_n, end_n = int(start_s), int(end_s)
        width = len(start_s)
        if 0 < (end_n - start_n) <= 30:
            return [f"{prefix}{str(i).zfill(width)}" for i in range(start_n, end_n + 1)]

    # Pattern 2: "FD-02-08 & #09" or "FD-02-08 & FD-02-09"
    if "&" in clean or "," in clean:
        parts = [p.strip().lstrip("#").strip() for p in re.split(r"[&,]", clean) if p.strip()]
        if len(parts) > 1:
            first = parts[0]
            m_prefix = re.match(r"^(.*?)(\d+)$", first)
            expanded = [first]
            for p in parts[1:]:
                if p.isdigit() and m_prefix:
                    prefix = m_prefix.group(1)
                    width = len(m_prefix.group(2))
                    expanded.append(f"{prefix}{p.zfill(width)}")
                else:
                    expanded.append(p)
            return expanded

    return [clean.replace("#", "").strip()]


def map_priority_deterministic(priority_text) -> tuple[str, str]:
    if priority_text is None or not str(priority_text).strip():
        return "Medium (P2)", "priority not stated"
    s = str(priority_text).strip().lower()
    if any(k in s for k in ("critical", "high", "urgent", "p1")):
        return "High (P1)", ""
    if any(k in s for k in ("medium", "standard", "p2")):
        return "Medium (P2)", ""
    if any(k in s for k in ("low", "advisory", "p3")):
        return "Low (P3)", ""
    return "Medium (P2)", "priority not stated"


def process_extracted_doors(raw_doors: list) -> tuple[list, list, list]:
    seen_keys = set()
    passed_doors = []
    no_access_doors = []
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

        raw_qty = safe_int(d.get("qty"), 0) or 0
        if count > 1 and raw_qty >= count:
            base_q = raw_qty // count
            rem_q = raw_qty % count
            qty_per_door = [base_q + (1 if idx < rem_q else 0) for idx in range(count)]
        else:
            qty_per_door = [raw_qty] * count

        status = str(d.get("status") or "fail").strip().lower()
        if "pass" in status:
            status = "pass"
        elif "no" in status and "access" in status:
            status = "no_access"
        else:
            status = "fail"

        floor = str(d.get("floor") or "Unknown").strip() or "Unknown"
        rating = str(d.get("rating") or "Unknown").strip() or "Unknown"
        findings = str(d.get("findings") or "").strip() or "Unspecified finding"
        req_works = str(d.get("required_works") or "").strip() or findings
        prio, prio_note = map_priority_deterministic(d.get("priority_text"))
        client_supplied = bool(d.get("client_supplied", False))
        part_code = str(d.get("part_code") or "").strip().upper()
        page_num = safe_int(d.get("page"), 1) or 1

        for idx, single_id in enumerate(split_ids):
            dedupe_key = f"{norm_key(single_id)}|{norm_key(floor)}|{norm_key(findings)}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            if status == "pass":
                passed_doors.append({"door_id": single_id, "floor": floor, "rating": rating, "page": page_num})
                continue
            if status == "no_access":
                no_access_doors.append({"door_id": single_id, "floor": floor, "rating": rating, "findings": findings, "page": page_num})
                continue

            if client_supplied:
                part_desc = "Client-Supplied (Labour Only)"
                qty_val = 0
                unit_cost = 0.0
                is_unmapped = False
            elif part_code in PARTS_CATALOG:
                cat_item = PARTS_CATALOG[part_code]
                part_desc = cat_item["description"]
                qty_val = qty_per_door[idx]
                unit_cost = float(cat_item["unit_price_gbp"])
                is_unmapped = False
            else:
                part_desc = "UNMAPPED"
                qty_val = qty_per_door[idx]
                unit_cost = 0.0
                is_unmapped = True

            est_m = mins_per_door[idx]
            remedial_display = f"{req_works} ({prio_note})" if prio_note else req_works

            failed_doors.append({
                "door_id": single_id,
                "floor": floor,
                "rating": rating,
                "defect": findings,
                "remedial_action": remedial_display,
                "part_code": part_code if not is_unmapped else "UNMAPPED",
                "part_needed": part_desc,
                "qty": qty_val,
                "unit_cost_gbp": unit_cost,
                "labour_mins": est_m if est_m is not None else 0,
                "has_est_time": est_m is not None and est_m > 0,
                "is_unmapped": is_unmapped,
                "priority": prio,
                "source_page": page_num,
            })

    return failed_doors, passed_doors, no_access_doors


async def execute_pipeline(job: ProcessJobRequest):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            await update_job_status(client, job.job_id, {"status": "processing"})

            dl_resp = await client.get(
                f"{SUPABASE_URL}/storage/v1/object/raw-surveys/{job.raw_pdf_path}",
                headers=sb_headers(content_type=""),
                timeout=60.0
            )
            dl_resp.raise_for_status()
            pdf_bytes = dl_resp.content

            commercial = await extract_commercial_from_email(client, job)

            _, chunks = split_pdf_into_chunks(pdf_bytes, pages_per_chunk=20)
            sem = asyncio.Semaphore(3)
            chunk_results = await asyncio.gather(
                *(
                    extract_chunk_with_gemini(client, sem, start_p, c_bytes, idx == 0)
                    for idx, (start_p, c_bytes) in enumerate(chunks)
                )
            )

            first_chunk = chunk_results[0] if chunk_results else {}
            end_client_name = str(first_chunk.get("end_client_name") or "").strip() or job.client_name
            contractor_company = str(first_chunk.get("contractor_company") or "").strip() or job.client_name
            site_name = str(first_chunk.get("site_name") or job.building_name or "Commercial Site").lstrip("–-— ").strip()
            total_doors_declared = safe_int(first_chunk.get("total_doors_declared"), None)

            all_raw_doors = []
            for r in chunk_results:
                all_raw_doors.extend(r.get("doors") or [])

            failed_doors, passed_doors, no_access_doors = process_extracted_doors(all_raw_doors)
            total_surveyed = len(failed_doors) + len(passed_doors) + len(no_access_doors)

            if total_surveyed == 0:
                raise ValueError("ZeroDoorsError: No fire door inspection records were found in the uploaded PDF.")

            # Quality Gate checks
            reasons = []
            if failed_doors:
                missing_time_count = sum(1 for d in failed_doors if not d["has_est_time"])
                unmapped_count = sum(1 for d in failed_doors if d["is_unmapped"])
                if (missing_time_count / len(failed_doors)) > 0.30:
                    reasons.append(f"{missing_time_count}/{len(failed_doors)} failed doors missing est_time_minutes (>30%).")
                if (unmapped_count / len(failed_doors)) > 0.30:
                    reasons.append(f"{unmapped_count}/{len(failed_doors)} failed doors have UNMAPPED parts (>30%).")
            if total_doors_declared is not None and total_surveyed != total_doors_declared:
                reasons.append(f"Extracted {total_surveyed} doors vs {total_doors_declared} declared in survey.")
            needs_review = len(reasons) > 0

            b_info = {
                "contractor_company": contractor_company,
                "client_company": end_client_name,
                "building_name": site_name,
                "survey_date": datetime.now(timezone.utc).strftime("%d %b %Y"),
                "standard_version": job.standard_version,
                "total_doors_surveyed": total_surveyed,
                "passed_count": len(passed_doors),
                "no_access_count": len(no_access_doors),
                "no_access_ids": [d["door_id"] for d in no_access_doors],
                "hourly_rate": commercial["hourly_rate"],
                "material_markup": commercial["material_markup"],
                "site_fee": commercial["setup_fee_gbp"],
                "vat_rate": commercial["vat_rate"],
            }

            xlsx_local = f"/tmp/{job.job_id}.xlsx"
            pdf_local = f"/tmp/{job.job_id}.pdf"
            generate_excel_pack(b_info, failed_doors, xlsx_local)
            generate_prework_pdf(b_info, failed_doors, pdf_local)

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

            await update_job_status(client, job.job_id, {
                "status": "completed",
                "xlsx_url": xlsx_remote,
                "pdf_url": pdf_remote,
                "ex
