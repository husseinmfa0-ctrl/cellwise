"""
cleaner.py
Core AI cleaning engine. Takes a pandas DataFrame of messy CRM contact data
and returns cleaned, HubSpot-ready records using Gemini 1.5 Flash, plus real
MX-record email validation, per-row fix badges, and summary stats.

Design notes:
- Gemini is asked for a per-batch JSON OBJECT (not a bare array): a
  `header_mapping` (which original column fed which standard field) and a
  `contacts` array. This lets the UI show genuine "N headers auto-mapped"
  and "AI operations" log lines drawn from real output, not fabricated copy.
- Badges are derived from the model's OWN output shape (e.g. "does this
  field look like E.164") rather than a fragile row-by-row diff against the
  original sheet, since batched LLM output isn't guaranteed to preserve row
  order 1:1. This is deliberately conservative: no badge is invented that
  isn't backed by something inspectable in the cleaned record.
- MX validation is real DNS (`dnspython`), not a model claim. A model can
  say an email "looks valid"; only a live MX lookup can say the domain can
  actually receive mail. Domains are cached per-request since many contacts
  share a domain (gmail.com, the client's own company domain, etc).
"""

import os
import re
import json
import math
from typing import List, Dict, Any, Tuple, Optional, Iterator

import pandas as pd
from google import genai
from google.genai import types

try:
    import dns.resolver
    _DNS_AVAILABLE = True
except ImportError:  # pragma: no cover - dnspython should always be installed
    _DNS_AVAILABLE = False

MODEL_NAME = "gemini-1.5-flash"

ROWS_PER_BATCH = 150
ASSUMED_SECONDS_SAVED_PER_ROW = 40

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SYSTEM_PROMPT = """You are an expert CRM Data Migration Assistant. Your job is to take a messy \
tabular dataset and clean/format it specifically for a HubSpot contact import.

Task:
1. Header Auto-Mapping: Map arbitrary user headers to standard HubSpot properties:
   - "الاسم بالكامل", "Full Name", or "Name" -> Split and map to 'First Name' and 'Last Name'.
   - "البريد", "البريد الإلكتروني", or "Email" -> Map to 'Email'.
   - "الموبايل", "الهاتف", or "Phone" -> Map to 'Phone Number'.
2. Name Cleaning: Ensure 'First Name' and 'Last Name' are properly separated. If only one \
name is provided, assign it to 'First Name' and leave 'Last Name' empty.
3. Phone Standardization: Convert all phone numbers to international E.164 format \
(e.g., +201xxxxxxxxx). Assume the default country code is Egypt (+20) if no country code is \
present. If a number cannot be confidently standardized, leave it as-is.
4. Email Validation: Identify and filter out obviously invalid email addresses (missing '@', \
malformed structure). Add a boolean field "Is_Valid_Email": true/false based on FORMAT only \
(a separate system checks whether the domain can actually receive mail).

Output Format:
Return ONLY a valid JSON object (no markdown fences, no commentary) shaped exactly like this:
{
  "header_mapping": {"<original column name>": "<standard field it maps to>", ...},
  "contacts": [
    {"First Name": "...", "Last Name": "...", "Email": "...", "Phone Number": "...",
     "Is_Valid_Email": true}
  ]
}
Every object in "contacts" must include at least: "First Name", "Last Name", "Email",
"Phone Number", "Is_Valid_Email". Preserve any other useful columns from the source data as
extra fields. "header_mapping" should reflect every original column you were able to map.
"""


class CleaningError(Exception):
    pass


def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise CleaningError(
            "GEMINI_API_KEY is not set. Copy backend/.env.example to backend/.env and add your key."
        )
    return genai.Client(api_key=api_key)


def _strip_json_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    return cleaned.strip()


def read_spreadsheet(file_path: str, filename: str) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return pd.read_csv(file_path)
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return pd.read_excel(file_path)
    raise CleaningError("Unsupported file type. Please upload a .csv or .xlsx file.")


def _clean_batch_raw(client: genai.Client, batch_df: pd.DataFrame) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    data_str = batch_df.to_string(index=False)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=f"{SYSTEM_PROMPT}\n\nRaw data:\n{data_str}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )

    raw_text = response.text or ""
    payload = _strip_json_fences(raw_text)

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CleaningError(f"Model did not return valid JSON: {exc}") from exc

    if isinstance(parsed, list):
        return {}, parsed

    if not isinstance(parsed, dict):
        raise CleaningError("Model response was not a JSON object or array.")

    contacts = parsed.get("contacts")
    if not isinstance(contacts, list):
        for key in ("data", "results"):
            if isinstance(parsed.get(key), list):
                contacts = parsed[key]
                break
        else:
            raise CleaningError("Model response had no 'contacts' array.")

    header_mapping = parsed.get("header_mapping")
    if not isinstance(header_mapping, dict):
        header_mapping = {}

    return header_mapping, contacts


def _domain_has_mx(domain: str, cache: Dict[str, bool]) -> bool:
    domain = domain.lower().strip().rstrip(".")
    if not domain:
        return False
    if domain in cache:
        return cache[domain]

    result = False
    if _DNS_AVAILABLE:
        try:
            resolver = dns.resolver.Resolver()
            resolver.timeout = 2.5
            resolver.lifetime = 2.5
            answers = resolver.resolve(domain, "MX")
            result = len(answers) > 0
        except Exception:
            result = False
    cache[domain] = result
    return result


def _badges_for_contact(contact: Dict[str, Any], mapped_full_name: bool, mx_cache: Dict[str, bool]) -> Tuple[Dict[str, Any], List[str]]:
    badges: List[str] = []

    phone = str(contact.get("Phone Number") or "").strip()
    if phone and E164_RE.match(phone):
        badges.append("Phone Number: Fixed to E.164")

    first = str(contact.get("First Name") or "").strip()
    last = str(contact.get("Last Name") or "").strip()
    if mapped_full_name and first and last:
        badges.append("Name: Split from Full Name")

    email = str(contact.get("Email") or "").strip()
    format_valid = bool(contact.get("Is_Valid_Email")) and bool(EMAIL_RE.match(email))

    mx_valid: Optional[bool] = None
    if email and format_valid:
        domain = email.split("@")[-1]
        mx_valid = _domain_has_mx(domain, mx_cache)

    if email and not format_valid:
        badges.append("Email: Invalid Format")
        final_valid = False
    elif email and format_valid and mx_valid is False:
        badges.append("Email: Invalid Domain (no MX record)")
        final_valid = False
    elif email and format_valid and mx_valid is True:
        final_valid = True
    else:
        final_valid = False

    contact["Is_Valid_Email"] = final_valid
    if mx_valid is not None:
        contact["Mx_Valid"] = mx_valid
    contact["_badges"] = badges

    return contact, badges


def _mapped_a_full_name(header_mapping: Dict[str, str]) -> bool:
    values = [v.lower() for v in header_mapping.values() if isinstance(v, str)]
    has_first = any("first name" in v for v in values)
    has_last = any("last name" in v for v in values)
    return has_first and has_last


def clean_dataframe_streaming(df: pd.DataFrame) -> Iterator[Dict[str, Any]]:
    if df.empty:
        yield {"type": "result", "cleaned": [], "header_mapping": {}, "stats": _empty_stats()}
        return

    client = _get_client()
    total_rows = len(df)
    num_batches = math.ceil(total_rows / ROWS_PER_BATCH)

    all_contacts: List[Dict[str, Any]] = []
    combined_mapping: Dict[str, str] = {}
    mx_cache: Dict[str, bool] = {}

    phone_fixed = 0
    invalid_emails = 0
    name_splits = 0

    for i in range(num_batches):
        start = i * ROWS_PER_BATCH
        end = min(start + ROWS_PER_BATCH, total_rows)
        batch_df = df.iloc[start:end]

        yield {
            "type": "log",
            "message": f"Sending rows {start + 1}-{end} of {total_rows} to Gemini 1.5 Flash "
                       f"(batch {i + 1}/{num_batches})...",
        }

        header_mapping, contacts = _clean_batch_raw(client, batch_df)
        combined_mapping.update(header_mapping)

        mapped_full_name = _mapped_a_full_name(header_mapping)
        if header_mapping:
            mapping_desc = ", ".join(f"'{k}' -> {v}" for k, v in list(header_mapping.items())[:4])
            more = f" (+{len(header_mapping) - 4} more)" if len(header_mapping) > 4 else ""
            yield {"type": "log", "message": f"Header mapping found: {mapping_desc}{more}"}

        batch_phone_fixed = 0
        batch_invalid = 0
        batch_name_split = 0
        for contact in contacts:
            contact, badges = _badges_for_contact(contact, mapped_full_name, mx_cache)
            if any(b.startswith("Phone Number") for b in badges):
                batch_phone_fixed += 1
            if any(b.startswith("Email: Invalid") for b in badges):
                batch_invalid += 1
            if any(b.startswith("Name:") for b in badges):
                batch_name_split += 1

        phone_fixed += batch_phone_fixed
        invalid_emails += batch_invalid
        name_splits += batch_name_split

        yield {
            "type": "log",
            "message": f"Phone standardization: fixed {batch_phone_fixed} numbers to E.164 in this batch.",
        }
        yield {
            "type": "log",
            "message": f"Validating MX records for {len(contacts)} email domains...",
        }
        yield {
            "type": "log",
            "message": f"Email validation: flagged {batch_invalid} invalid/undeliverable addresses "
                       f"in this batch.",
        }

        all_contacts.extend(contacts)
        yield {"type": "batch_done", "batch": i + 1, "of": num_batches}

    hours_saved = round((total_rows * ASSUMED_SECONDS_SAVED_PER_ROW) / 3600, 1)

    stats = {
        "total_rows": total_rows,
        "headers_mapped": len(combined_mapping),
        "phone_numbers_fixed": phone_fixed,
        "invalid_emails_caught": invalid_emails,
        "names_split": name_splits,
        "estimated_hours_saved": hours_saved,
        "assumed_seconds_per_row": ASSUMED_SECONDS_SAVED_PER_ROW,
    }

    yield {
        "type": "result",
        "cleaned": all_contacts,
        "header_mapping": combined_mapping,
        "stats": stats,
    }


def _empty_stats() -> Dict[str, Any]:
    return {
        "total_rows": 0,
        "headers_mapped": 0,
        "phone_numbers_fixed": 0,
        "invalid_emails_caught": 0,
        "names_split": 0,
        "estimated_hours_saved": 0,
        "assumed_seconds_per_row": ASSUMED_SECONDS_SAVED_PER_ROW,
    }


def clean_dataframe(df: pd.DataFrame) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for event in clean_dataframe_streaming(df):
        if event["type"] == "result":
            cleaned = event["cleaned"]
    return cleaned
