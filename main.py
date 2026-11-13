import os
import io
import json
import base64
import asyncio
from datetime import datetime
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
    standard_version: str = "BS 8214:2026"
    raw_pdf_path: str


def sb_headers(content_type: str = "application/json") -> dict:
    h = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


async def update_job_status(client: httpx.AsyncClient, job_id: str, payload: dict):
    payload["updated_at"] = datetime.utcnow().isoformat()
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


async def extract_chunk_with_gemini(client: httpx.AsyncClient, start_page: int, chunk_bytes: bytes) -> dict:
    b64_pdf = base64.b64encode(chunk_bytes).decode("utf-8")
    prompt = (
        f"You are a UK Fire Door Inspection data extractor. These PDF pages start at original page {start_page}. "
        "Extract EVERY door record visible in these pages. Do NOT invent data. "
        "Return strict JSON matching: "
        '{"total_doors_seen": <int>, "failed_doors": [{"door_id": "...", "floor": "...", "rating": "FD30/FD60/Unknown", '
        '"status": "Fail/Repair/Replace/No Access", "defect": "exact defects", "source_page": <int>}]} '
        "Exclude doors clearly marked Pass/Compliant from failed_doors, but count them in total_doors_seen."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_PAID_API_KEY}"
    resp = await client.post(
        url,
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
    resp.raise_for_status()
    raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw_text)


async def enrich_with_deepseek(client: httpx.AsyncClient, doors_batch: list, key_idx: int, standard_ver: str) -> list:
    if not doors_batch:
        return []
    prompt = (
        f"You are a UK Fire Door Engineer ({standard_ver}). For each failed door below, provide exact UK remediation details. "
        'Return strict JSON: {"doors": [{"door_id": "...", "floor": "...", "rating": "...", "defect": "...", "source_page": 1, '
        '"remedial_action": "BS 8214 compliant action", "part_needed": "exact UK trade part or No Parts Required", '
        '"qty": <int>, "unit_cost_gbp": <float>, "labour_mins": <int>, "priority": "High (P1) or Medium (P2) or Low (P3)"}]}'
    )
    # Automatic failover across all available DeepSeek keys
    for attempt in range(len(DEEPSEEK_KEYS)):
        api_key = DEEPSEEK_KEYS[(key_idx + attempt) % len(DEEPSEEK_KEYS)]
        try:
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
        except Exception:
            if attempt == len(DEEPSEEK_KEYS) - 1:
                raise
    return []


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

            # 2. Split into 20-page chunks & extract via Paid Gemini Pro
            _, chunks = split_pdf_into_chunks(pdf_bytes, pages_per_chunk=20)
            chunk_results = await asyncio.gather(
                *(extract_chunk_with_gemini(client, start_p, c_bytes) for start_p, c_bytes in chunks)
            )

            total_surveyed = sum(int(r.get("total_doors_seen", 0)) for r in chunk_results)
            seen_ids = set()
            unique_failed = []
            for r in chunk_results:
                for d in r.get("failed_doors", []):
                    did = str(d.get("door_id", "")).strip()
                    if did and did not in seen_ids:
                        seen_ids.add(did)
                        unique_failed.append(d)

            if total_surveyed < len(unique_failed):
                total_surveyed = len(unique_failed)

            # 3. Parallel BS 8214 enrichment across 3 DeepSeek keys
            batches = [unique_failed[i::3] for i in range(3)]
            enriched_lists = await asyncio.gather(
                *(enrich_with_deepseek(client, b, idx, job.standard_version) for idx, b in enumerate(batches))
            )
            final_doors = [door for sublist in enriched_lists for door in sublist]

            # 4. Generate Excel & Pre-Work PDF in /tmp
            b_info = {
                "client_company": job.client_name,
                "building_name": job.building_name,
                "survey_date": datetime.utcnow().strftime("%d %b %Y"),
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
            await update_job_status(client, job.job_id, {
                "status": "failed",
                "error_message": str(exc)[:500]
            })
            await client.post(
                N8N_CALLBACK_URL,
                json={"job_id": job.job_id, "status": "failed", "error": str(exc)[:300]}
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
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    bg.add_task(execute_pipeline, job)
    return {"status": "accepted", "job_id": job.job_id}
