"""
main.py
FastAPI backend for the HubSpot Data Prep tool.

Endpoints:
  POST /api/clean         -> upload a .csv/.xlsx, get back the full cleaned payload in one shot
  POST /api/clean-stream  -> same, but streams Server-Sent Events as batches complete
                              (event: log | batch_done | result | error)
  POST /api/export-csv    -> given cleaned JSON rows, get back a downloadable CSV file

Run locally:
  uvicorn main:app --reload --port 8000
"""

import io
import os
import json
import tempfile
from typing import List, Dict, Any

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from cleaner import (
    read_spreadsheet,
    clean_dataframe_streaming,
    CleaningError,
)

load_dotenv()

app = FastAPI(title="HubSpot Data Prep API")

allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_FILE_SIZE_MB = 10
MAX_PREVIEW_ROWS = 10


@app.get("/api/health")
def health():
    return {"status": "ok"}


def _validate_and_save_upload(contents: bytes, filename: str) -> str:
    if not filename.lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Please upload a .csv or .xlsx file.")
    if len(contents) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=400, detail=f"File is larger than the {MAX_FILE_SIZE_MB}MB limit."
        )
    suffix = os.path.splitext(filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(contents)
        return tmp.name


@app.post("/api/clean")
async def clean_file(file: UploadFile = File(...)):
    contents = await file.read()
    tmp_path = _validate_and_save_upload(contents, file.filename)

    try:
        df = read_spreadsheet(tmp_path, file.filename)
        if df.empty:
            raise HTTPException(status_code=400, detail="The uploaded file has no rows.")

        original_preview = df.head(MAX_PREVIEW_ROWS).fillna("").to_dict(orient="records")

        cleaned_rows: List[Dict[str, Any]] = []
        header_mapping: Dict[str, str] = {}
        stats: Dict[str, Any] = {}
        for event in clean_dataframe_streaming(df):
            if event["type"] == "result":
                cleaned_rows = event["cleaned"]
                header_mapping = event["header_mapping"]
                stats = event["stats"]

        return {
            "filename": file.filename,
            "row_count": len(df),
            "original_preview": original_preview,
            "cleaned": cleaned_rows,
            "header_mapping": header_mapping,
            "stats": stats,
        }
    except CleaningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        os.unlink(tmp_path)


@app.post("/api/clean-stream")
async def clean_file_stream(file: UploadFile = File(...)):
    contents = await file.read()
    tmp_path = _validate_and_save_upload(contents, file.filename)

    def event_stream():
        try:
            df = read_spreadsheet(tmp_path, file.filename)
            if df.empty:
                yield _sse("error", {"detail": "The uploaded file has no rows."})
                return

            original_preview = df.head(MAX_PREVIEW_ROWS).fillna("").to_dict(orient="records")
            yield _sse("meta", {
                "filename": file.filename,
                "row_count": len(df),
                "original_preview": original_preview,
            })

            for event in clean_dataframe_streaming(df):
                if event["type"] == "log":
                    yield _sse("log", {"message": event["message"]})
                elif event["type"] == "batch_done":
                    yield _sse("batch_done", {"batch": event["batch"], "of": event["of"]})
                elif event["type"] == "result":
                    yield _sse("result", {
                        "cleaned": event["cleaned"],
                        "header_mapping": event["header_mapping"],
                        "stats": event["stats"],
                    })
        except CleaningError as exc:
            yield _sse("error", {"detail": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors to the client too
            yield _sse("error", {"detail": f"Unexpected server error: {exc}"})
        finally:
            os.unlink(tmp_path)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/export-csv")
def export_csv(rows: List[Dict[str, Any]] = Body(..., embed=True)):
    if not rows:
        raise HTTPException(status_code=400, detail="No rows provided to export.")

    # Strip internal-only fields before generating the HubSpot import file.
    clean_rows = []
    for row in rows:
        clean_rows.append({k: v for k, v in row.items() if not k.startswith("_") and k != "Mx_Valid"})

    df = pd.DataFrame(clean_rows)
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=hubspot_ready_contacts.csv"},
    )
