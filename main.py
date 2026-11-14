import os
import io
import json
import hmac
import base64
import asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel
import httpx
from pypdf import PdfReader, PdfWriter
from generators import generate_excel_pack, generate_prework_pdf

app = FastAPI(title="UK Fire Door Zero-Touch Engine")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]
GEMINI_PAID_API_KEY = os.environ["GEMINI_PAID_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
DEEPSEEK_KEYS = [k for k in [
    os.getenv("DEEPSEEK_KEY_1"),
    os.getenv("DEEPSEEK_KEY_2"),
    os.getenv("DEEPSEEK_KEY_3"),
] if k]
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
    standard_version: str = "BS 8214:2016"
    raw_pdf_path: str


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


async def extract_chunk_with_gemini(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    start_page: int,
    chunk_bytes: bytes
) -> dict:
    b64_pdf = base64.b64encode(chunk_bytes).decode("utf-8")
    prompt = (
        f"You are a UK Fire Door Inspection data extractor. These PDF pages start at original page {start_page}. "
        "Extract EVERY door record visible in these pages. Do NOT invent data. "
        "Return strict JSON matching: "
        '{"total_doors_seen": <int>, "failed_doors": [{"door_id": "...", "floor": "...", "rating": "FD30/FD60/Unknown", '
        '"status": "Fail/Repair/Replace/No Access", "defect": "exact defects", "source_page": <int>}]} '
        "Exclude doors clearly marked Pass/Compliant from failed_doors, but count them in total_doors_seen."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    backoff_waits = [5, 15, 30]

    async with sem:
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
                        "contents": [{
                            "parts": [
                                {"text": prompt},
                                {"inline_data": {"mime_type": "application/pdf", "data": b64_pdf}}
                            ]
                        }],
                        "generationConfig": {
                            "temperature": 0.0,
                            "response_mime_type": "application/json"
                        }
                    },
                    timeout=180.0
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    resp.raise_for_status()
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


def normalise_priority(val) -> str:
    s = str(val or "").strip().lower()
    if "high" in s or "p1" in s:
        return "High (P1)"
    if "low" in s or "p3" in s:
        return "Low (P3)"
    if "medium" in s or "p2" in s:
        return "Medium (P2)"
    return "Medium (P2)"


def safe_int(val, default: int) -> int:
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def safe_float(val, default: float) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def normalise_door_record(orig: dict, enriched: dict | None = None) -> dict:
    src = enriched if isinstance(enriched, dict) else {}
    door_id = str(src.get("door_id") or orig.get("door_id") or "Unknown").strip() or "Unknown"
    floor = str(src.get("floor") or orig.get("floor") or "Unknown").strip() or "Unknown"
    rating = str(src.get("rating") or orig.get("rating") or "Unknown").strip() or "Unknown"
    defect = str(src.get("defect") or orig.get("defect") or "Unspecified defect").strip() or "Unspecified defect"
    source_page = safe_int(src.get("source_page", orig.get("source_page", 1)), 1)

    if enriched is None:
        return {
            "row_id": orig["row_id"],
            "door_id": door_id,
            "floor": floor,
            "rating": rating,
            "defect": defect,
            "source_page": source_page,
            "remedial_action": "MANUAL REVIEW REQUIRED",
            "part_needed": "No Parts Required",
            "qty": 0,
            "unit_cost_gbp": 0.0,
            "labour_mins": 30,
            "priority": "Medium (P2)"
        }

    remedial_action = str(src.get("remedial_action") or "MANUAL REVIEW REQUIRED").strip() or "MANUAL REVIEW REQUIRED"
    part_needed = str(src.get("part_needed") or "No Parts Required").strip() or "No Parts Required"
    qty = safe_int(src.get("qty", 0), 0)
    unit_cost_gbp = safe_float(src.get("unit_cost_gbp", 0.0), 0.0)
    labour_mins = safe_int(src.get("labour_mins", 30), 30)
    priority = normalise_priority(src.get("priority"))

    return {
        "row_id": orig["row_id"],
        "door_id": door_id,
        "floor": floor,
        "rating": rating,
        "defect": defect,
        "source_page": source_page,
        "remedial_action": remedial_action,
        "part_needed": part_needed,
        "qty": qty,
        "unit_cost_gbp": unit_cost_gbp,
        "labour_mins": labour_mins,
        "priority": priority
    }


async def call_deepseek_once(client: httpx.AsyncClient, doors_batch: list, api_key: str, standard_ver: str) -> list:
    prompt = (
        f"You are a UK Fire Door Engineer ({standard_ver}). For each failed door below, provide exact UK remediation details. "
        "You MUST echo back the exact integer 'row_id' for every input door. "
        'Return strict JSON: {"doors": [{"row_id": <int>, "door_id": "...", "floor": "...", "rating": "...", "defect": "...", "source_page": 1, '
        '"remedial_action": "BS 8214 compliant action", "part_needed": "exact UK trade part or No Parts Required", '
        '"qty": <int>, "unit_cost_gbp": <float>, "labour_mins": <int>, "priority": "High (P1) or Medium (P2) or Low (P3)"}]}'
    )
    resp = await client.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-chat",
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(doors_batch)}
            ]
        },
        timeout=120.0
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content).get("doors", [])


async def enrich_with_deepseek(client: httpx.AsyncClient, doors_batch: list, key_idx: int, standard_ver: str) -> list:
    if not doors_batch:
        return []
    if not DEEPSEEK_KEYS:
        return [normalise_door_record(d, None) for d in doors_batch]

    returned_by_id = {}
    used_key_offset = 0

    for attempt in range(len(DEEPSEEK_KEYS)):
        used_key_offset = (key_idx + attempt) % len(DEEPSEEK_KEYS)
        api_key = DEEPSEEK_KEYS[used_key_offset]
        try:
            doors_out = await call_deepseek_once(client, doors_batch, api_key, standard_ver)
            for item in doors_out:
                if isinstance(item, dict) and "row_id" in item:
                    rid = safe_int(item.get("row_id"), -1)
                    if rid >= 0:
                        returned_by_id[rid] = item
            break
        except Exception:
            continue

    missing_doors = [d for d in doors_batch if d["row_id"] not in returned_by_id]
    if missing_doors and DEEPSEEK_KEYS:
        next_key = DEEPSEEK_KEYS[(used_key_offset + 1) % len(DEEPSEEK_KEYS)]
        try:
            retry_out = await call_deepseek_once(client, missing_doors, next_key, standard_ver)
            for item in retry_out:
                if isinstance(item, dict) and "row_id" in item:
                    rid = safe_int(item.get("row_id"), -1)
                    if rid >= 0:
                        returned_by_id[rid] = item
        except Exception:
            pass

    results = []
    for d in doors_batch:
        rid = d["row_id"]
        if rid in returned_by_id:
            results.append(normalise_door_record(d, returned_by_id[rid]))
        else:
            results.append(normalise_door_record(d, None))
    return results


async def execute_pipeline(job: ProcessJobRequest):
    async with httpx.AsyncClient() as client:
        try:
            await update_job_status(client, job.job_id, {"status": "processing"})

            # 1. Download raw PDF from Supabase Storage
            dl_resp = await client.get(
                f"{SUPABASE_URL}/storage/v1/object/raw-surveys/{job.raw_pdf_path}",
                headers=sb_headers(content_type=""),
                timeout=60.0
            )
            dl_resp.raise_for_status()
            pdf_bytes = dl_resp.content

            # 2. Split into 20-page chunks & extract via Paid Gemini Pro with Semaphore(3)
            _, chunks = split_pdf_into_chunks(pdf_bytes, pages_per_chunk=20)
            sem = asyncio.Semaphore(3)
            chunk_results = await asyncio.gather(
                *(extract_chunk_with_gemini(client, sem, start_p, c_bytes) for start_p, c_bytes in chunks)
            )

            total_surveyed = sum(safe_int(r.get("total_doors_seen", 0), 0) for r in chunk_results)
            seen_keys = set()
            unique_failed = []
            for r in chunk_results:
                for d in r.get("failed_doors", []):
                    if not isinstance(d, dict):
                        continue
                    did = str(d.get("door_id", "")).strip()
                    flr = str(d.get("floor", "")).strip()
                    dedupe_key = f"{did.lower()}|{flr.lower()}"
                    if did and dedupe_key not in seen_keys:
                        seen_keys.add(dedupe_key)
                        unique_failed.append(d)

            if total_surveyed < len(unique_failed):
                total_surveyed = len(unique_failed)

            if total_surveyed == 0:
                raise ValueError("ZeroDoorsError: No fire door inspection records were found in the uploaded PDF.")

            for idx, d in enumerate(unique_failed):
                d["row_id"] = idx

            # 3. Parallel BS 8214 enrichment across 3 DeepSeek keys
            batches = [unique_failed[i::3] for i in range(3)]
            enriched_lists = await asyncio.gather(
                *(enrich_with_deepseek(client, b, idx, job.standard_version) for idx, b in enumerate(batches))
            )
            all_enriched = [door for sublist in enriched_lists for door in sublist]
            enriched_by_row_id = {d["row_id"]: d for d in all_enriched}

            final_doors = []
            for orig in unique_failed:
                rid = orig["row_id"]
                door_obj = enriched_by_row_id.get(rid) or normalise_door_record(orig, None)
                final_doors.append(door_obj)

            # 4. Generate Excel & Pre-Work PDF in /tmp
            b_info = {
                "client_company": job.client_name,
                "building_name": job.building_name,
                "survey_date": datetime.now(timezone.utc).strftime("%d %b %Y"),
                "standard_version": job.standard_version,
                "total_doors_surveyed": total_surveyed,
                "hourly_rate": job.hourly_rate,
                "material_markup": job.material_markup,
            }
            xlsx_local = f"/tmp/{job.job_id}.xlsx"
            pdf_local = f"/tmp/{job.job_id}.pdf"
            generate_excel_pack(b_info, final_doors, xlsx_local)
            generate_prework_pdf(b_info, final_doors, pdf_local)

            # 5. Upload deliverables to Supabase Storage
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
                "extracted_doors_json": final_doors
            })

            # 6. Notify n8n Workflow B (Optional Delivery Switch)
            await client.post(
                N8N_CALLBACK_URL,
                json={
                    "job_id": job.job_id,
                    "status": "completed",
                    "gmail_thread_id": job.gmail_thread_id,
                    "sender_email": job.sender_email,
                    "client_name": job.client_name,
                    "building_name": job.building_name,
                    "total_surveyed": total_surveyed,
                    "failed_count": len(final_doors),
                    "xlsx_path": xlsx_remote,
                    "pdf_path": pdf_remote
                }
            )

        except Exception as exc:
            clean_err = safe_error_summary(exc)
            await update_job_status(client, job.job_id, {
                "status": "failed",
                "error_message": clean_err[:500]
            })
            await client.post(
                N8N_CALLBACK_URL,
                json={
                    "job_id": job.job_id,
                    "status": "failed",
                    "gmail_thread_id": job.gmail_thread_id,
                    "client_name": job.client_name,
                    "building_name": job.building_name,
                    "error": clean_err[:300]
                }
            )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/process-job", status_code=202)
async def start_job(
    job: ProcessJobRequest,
    bg: BackgroundTasks,
    x_webhook_secret: str = Header(None)
):
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid secret")
    bg.add_task(execute_pipeline, job)
    return {"status": "accepted", "job_id": job.job_id}
