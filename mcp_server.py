"""
mcp_server.py
Single MCP server exposing all tools needed for the SJ Piano Academy
payment-tracking workflow:

  1. search_interac_emails   – search Gmail for Interac e-Transfer emails
  2. find_student_by_parent  – look up student in pianostudents collection
  3. check_invoice_exists    – check if invoice already exists for this month
  4. create_invoice          – insert a new invoice into the invoices collection
  5. send_thank_you_email    – email a PDF receipt to the student

Run with:
    python mcp_server.py
"""

import os
import json
import base64
import itertools
import smtplib
import time
import re
import email as email_lib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

# ── Google / Gmail ─────────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── MongoDB ─────────────────────────────────────────────────────────────────
from pymongo import MongoClient, ReturnDocument

# ── MCP ─────────────────────────────────────────────────────────────────────
from mcp.server.fastmcp import FastMCP

# ── Receipt PDF ──────────────────────────────────────────────────────────────
from receipt_generator import generate_receipt

# ════════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════════

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "")

SMTP_USER = os.getenv("GMAIL_SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("GMAIL_SMTP_APP_PASSWORD", "")
BCC_EMAIL = os.getenv("BCC_EMAIL", "")

# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _get_mongo_db():
    """Return the MongoDB database object."""
    client = MongoClient(MONGO_URI)
    return client[MONGO_DB_NAME]


def _extract_reply_to(msg_payload: dict) -> Optional[str]:
    """Extract Reply-To header from a Gmail message payload."""
    headers = msg_payload.get("headers", [])
    for h in headers:
        if h["name"].lower() == "reply-to":
            # May look like "Name <email@example.com>" or just "email@example.com"
            val = h["value"]
            match = re.search(r"[\w.\-+]+@[\w.\-]+", val)
            return match.group(0) if match else val
    return None


def _extract_date_received(msg_payload: dict) -> Optional[datetime]:
    """Extract the Date header and return as UTC datetime."""
    headers = msg_payload.get("headers", [])
    for h in headers:
        if h["name"].lower() == "date":
            from email.utils import parsedate_to_datetime
            try:
                return parsedate_to_datetime(h["value"]).astimezone(timezone.utc)
            except Exception:
                pass
    return None


def _parse_amount(subject: str) -> Optional[float]:
    """Extract dollar amount from subject like 'received $200.00 from'."""
    match = re.search(r"\$(\d+(?:\.\d+)?)", subject)
    return float(match.group(1)) if match else None


def _parse_parent_name(subject: str) -> Optional[str]:
    """Extract parent name from Interac subject line."""
    # Pattern: "received $X.XX from <Name> and it has been"
    match = re.search(r"received \$[\d.]+\s+from\s+(.+?)\s+and\s+it\s+has\s+been", subject, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _now() -> datetime:
    """Current UTC time, overridable via REMINDER_DATE (e.g. '2026-07-31') for backfilled runs."""
    override = os.getenv("REMINDER_DATE")
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
# MCP Server
# ════════════════════════════════════════════════════════════════════════════

mcp = FastMCP("SJPiano Payment Tracker")


@mcp.tool()
def search_interac_emails() -> str:
    """
    Search Gmail from the 1st of the current month to now for Interac e-Transfer
    emails that match the expected subject pattern.

    Returns a JSON list of matched emails, each containing:
      - message_id
      - subject
      - reply_to
      - date_received  (ISO format UTC)
      - parent_name    (extracted from subject)
      - amount         (float, extracted from subject)
    """
    try:
        service = _get_gmail_service()

        # Build date range: 1st of current month 00:00 UTC → now
        now = _now()
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Gmail query uses epoch seconds for after:/before:
        after_epoch = int(start_of_month.timestamp())

        query = (
            f'subject:"Interac e-Transfer" '
            f'subject:"automatically deposited" '
            f'after:{after_epoch}'
        )

        result = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=50,
        ).execute()

        messages = result.get("messages", [])
        if not messages:
            return json.dumps({"status": "no_emails", "emails": []})

        matched = []
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me",
                id=msg_ref["id"],
                format="metadata",
                metadataHeaders=["Subject", "Reply-To", "Date"],
            ).execute()

            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            subject = headers.get("subject", "")

            # Must contain "automatically deposited"
            if "automatically deposited" not in subject.lower():
                continue

            reply_to = _extract_reply_to(payload) or headers.get("reply-to", "")
            date_received = _extract_date_received(payload)
            parent_name = _parse_parent_name(subject)
            amount = _parse_amount(subject)

            matched.append({
                "message_id": msg_ref["id"],
                "subject": subject,
                "reply_to": reply_to,
                "date_received": date_received.isoformat() if date_received else None,
                "parent_name": parent_name,
                "amount": amount,
            })

        return json.dumps({"status": "ok", "emails": matched})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        end_of_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end_of_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return start_of_month, end_of_month


def _has_invoice_this_month(db, student_email: str, student_name: str) -> bool:
    """
    An invoice already exists for this SPECIFIC student (matched by both
    name AND email) this month. Siblings frequently share the same parent
    email, so matching on email alone would mark an unpaid sibling as
    already invoiced just because their brother/sister was invoiced.
    """
    start_of_month, end_of_month = _month_bounds(_now())
    invoice = db.invoices.find_one({
        "students.email": {"$regex": f"^{re.escape(student_email)}$", "$options": "i"},
        "students.name": {"$regex": f"^{re.escape(student_name)}$", "$options": "i"},
        "feepaiddate": {
            "$gte": start_of_month,
            "$lt": end_of_month,
        },
    })
    return invoice is not None


def _amount_cents(student: dict) -> Optional[int]:
    try:
        return round(float(student.get("amount", "")) * 100)
    except (TypeError, ValueError):
        return None


@mcp.tool()
def find_student_by_parent(parent_name: str, reply_to_email: str, amount: float) -> str:
    """
    Resolve an Interac payment to the student(s) in pianostudents it covers,
    matching on ParentName (case-insensitive) and email.

    Handles two cases:
      1. SINGLE CHILD — the paid amount exactly equals one child's own
         "amount" field. Returns that one student.
      2. COMBINED PAYMENT / SIBLINGS — a parent with multiple children can
         pay for more than one in a single e-transfer (e.g. $240 covering
         two children whose individual fee is $120 each), or send separate
         transfers of the same amount for each child. This looks at all of
         that parent's children (matched by ParentName + email) who do NOT
         already have an invoice this month, and finds the smallest group
         of them whose individual "amount" fields sum exactly to the paid
         amount. This also naturally handles the sibling-disambiguation
         case (two kids with the same individual fee, paid separately) by
         excluding whichever kid was already invoiced earlier in the run.

    Returns JSON:
      { "status": "ok", "students": [<one or more student docs>] }
      { "status": "not_found", "message": ... }
      { "status": "already_invoiced", "message": ... }
    """
    try:
        db = _get_mongo_db()
        paid_cents = round(amount * 100)

        all_candidates = list(db.pianostudents.find({
            "ParentName": {"$regex": f"^{re.escape(parent_name)}$", "$options": "i"},
            "email": {"$regex": f"^{re.escape(reply_to_email)}$", "$options": "i"},
        }))

        if not all_candidates:
            return json.dumps({
                "status": "not_found",
                "message": (
                    f"No student found for parent='{parent_name}', "
                    f"email='{reply_to_email}'"
                ),
            })

        uninvoiced = [
            s for s in all_candidates
            if not _has_invoice_this_month(db, reply_to_email, s.get("studentname", ""))
        ]

        if not uninvoiced:
            return json.dumps({
                "status": "already_invoiced",
                "message": (
                    f"All {len(all_candidates)} matching child(ren) for "
                    f"parent='{parent_name}', email='{reply_to_email}' already have "
                    f"invoices this month."
                ),
            })

        # Try smallest groups first: a lone exact match wins before any
        # multi-child combination is considered.
        match = None
        for size in range(1, len(uninvoiced) + 1):
            for combo in itertools.combinations(uninvoiced, size):
                combo_cents = [_amount_cents(s) for s in combo]
                if None in combo_cents:
                    continue
                if sum(combo_cents) == paid_cents:
                    match = combo
                    break
            if match:
                break

        if not match:
            return json.dumps({
                "status": "not_found",
                "message": (
                    f"No unpaid child or combination of children for "
                    f"parent='{parent_name}', email='{reply_to_email}' sums to "
                    f"${amount:.2f}."
                ),
            })

        students = []
        for s in match:
            s = dict(s)
            s["_id"] = str(s["_id"])
            students.append(s)

        return json.dumps({"status": "ok", "students": students})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def check_invoice_exists(student_email: str, student_name: str) -> str:
    """
    Check whether an invoice already exists in the invoices collection
    for the given student (matched by BOTH email AND name) in the CURRENT
    calendar month.

    Always pass student_name — siblings often share the same parent email,
    and matching on email alone would incorrectly report an unpaid sibling
    as already invoiced.

    Returns JSON: { "exists": true/false, "invoice": <doc or null> }
    """
    try:
        db = _get_mongo_db()

        start_of_month, end_of_month = _month_bounds(_now())

        invoice = db.invoices.find_one({
            "students.email": {"$regex": f"^{re.escape(student_email)}$", "$options": "i"},
            "students.name": {"$regex": f"^{re.escape(student_name)}$", "$options": "i"},
            "feepaiddate": {
                "$gte": start_of_month,
                "$lt": end_of_month,
            },
        })

        if invoice:
            invoice["_id"] = str(invoice["_id"])
            # Convert datetime fields to ISO strings for JSON
            if isinstance(invoice.get("feepaiddate"), datetime):
                invoice["feepaiddate"] = invoice["feepaiddate"].isoformat()
            return json.dumps({"exists": True, "invoice": invoice})

        return json.dumps({"exists": False, "invoice": None})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def create_invoice(
    student_name: str,
    student_email: str,
    amount: float,
    fee_paid_date_iso: str,
) -> str:
    """
    Insert a new invoice into the invoices collection.

    Args:
        student_name:       e.g. "Yanish"
        student_email:      e.g. "test@gmail.com"
        amount:             e.g. 200.0
        fee_paid_date_iso:  ISO 8601 string of when payment was received,
                            e.g. "2026-02-15T14:30:00+00:00"

    Returns JSON with the new invoice number.
    """
    try:
        db = _get_mongo_db()

        invoice_number = str(int(time.time() * 1000))  # millisecond timestamp
        fee_paid_date = datetime.fromisoformat(fee_paid_date_iso).astimezone(timezone.utc)

        doc = {
            "invoicenumber": invoice_number,
            "students": {
                "name": student_name,
                "address": "",
                "email": student_email,
                "phone": "",
            },
            "totalamount": float(amount),
            "tax": 0,
            "feepaiddate": fee_paid_date,
            "paymentstatus": "Paid",
            "items": [],
            "dateissued": int(time.time() * 1000),
            "__v": 0,
        }

        result = db.invoices.insert_one(doc)
        return json.dumps({
            "status": "ok",
            "invoice_number": invoice_number,
            "inserted_id": str(result.inserted_id),
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def send_thank_you_email(
    student_name: str,
    student_email: str,
    amount: float,
    invoice_number: str,
    fee_paid_date_iso: str,
) -> str:
    """
    Generate a PDF receipt and email it to the student.
    If TEST_EMAIL env var is set, all emails are redirected there instead
    (useful for testing locally without emailing real families).

    Args:
        student_name:       e.g. "Yanish"
        student_email:      e.g. "test@gmail.com"
        amount:             e.g. 200.0
        invoice_number:     e.g. "1764355491540"
        fee_paid_date_iso:  ISO 8601 date string of when payment was received
    """
    try:
        fee_paid_date = datetime.fromisoformat(fee_paid_date_iso).astimezone(timezone.utc)
        month_year = fee_paid_date.strftime("%b %Y")   # e.g. "Feb 2026"

        test_email = os.getenv("TEST_EMAIL", "")
        recipient = test_email if test_email else student_email

        # ── Generate PDF receipt ──────────────────────────────────
        pdf_bytes = generate_receipt(
            receipt_number=invoice_number,
            paid_on=fee_paid_date,
            student_name=student_name,
            student_email=student_email,
            amount=amount,
        )

        # ── Build email ───────────────────────────────────────────
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = recipient
        msg["Subject"] = f"Receipt for lesson payment {month_year} | SJ Piano Academy"
        if not test_email:
            msg["Bcc"] = BCC_EMAIL
        body = (
            "We have attached a digital copy of your receipt for your convenience."
        )
        msg.attach(MIMEText(body, "plain"))

        # Attach PDF
        attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
        attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"Receipt_{invoice_number}.pdf",
        )
        msg.attach(attachment)

        # ── Send via Gmail SMTP ───────────────────────────────────
        recipients = [recipient] if test_email else [student_email, BCC_EMAIL]
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(SMTP_USER, SMTP_APP_PASSWORD)
            smtp.sendmail(SMTP_USER, recipients, msg.as_string())

        return json.dumps({
            "status": "ok",
            "message": f"Thank you email sent to {recipient}"
                       + (f" (TEST MODE — original: {student_email})" if test_email else ""),
            "receipt_number": invoice_number,
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# ════════════════════════════════════════════════════════════════════════════
# Reminder tools
# ════════════════════════════════════════════════════════════════════════════

def _is_manual_billing(student: dict) -> bool:
    """
    True if this student pays outside the automated e-Transfer flow (e.g.
    cash) and should be excluded from automated reminders/tracking.

    The pianostudents collection has TWO inconsistent conventions for this
    flag across records: capital "Manual" (boolean True/False) and
    lowercase "manual" (string "true"/"false"). Check both spellings and
    treat True or the string "true" (any case) as manual billing, rather
    than trusting one exact key name + type — a single-convention Mongo
    query silently misses the other convention's records.
    """
    for key in ("Manual", "manual"):
        value = student.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() == "true":
            return True
    return False


@mcp.tool()
def get_active_students() -> str:
    """
    Return all students from the pianostudents collection where:
      - Status == "Active" (case-insensitive)
      - NOT flagged for manual/cash billing (see _is_manual_billing —
        checks both "Manual" and "manual" fields, boolean or string)

    Each record includes: _id (str), StudentName, ParentName, email, amount.
    Returns JSON: { "status": "ok", "students": [...] }
    """
    try:
        db = _get_mongo_db()
        cursor = db.pianostudents.find({
            "Status": {"$regex": "^active$", "$options": "i"},
        })
        students = []
        for s in cursor:
            if _is_manual_billing(s):
                continue
            s["_id"] = str(s["_id"])
            students.append(s)
        return json.dumps({"status": "ok", "students": students})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


TEST_EMAIL = os.getenv("TEST_EMAIL", "")


def _reserve_reminder_slot(db, email_key: str, date_key: str) -> bool:
    """
    Atomically claims the "reminder sent" slot for this email on this date.

    NOTE: each MCP tool call from the agent runs in its OWN fresh subprocess
    (langchain-mcp-adapters creates a new stdio session per call), so an
    in-memory set does NOT persist across calls within the same agent run —
    it resets to empty every time. MongoDB is the only state genuinely
    shared across those subprocesses, so the "already sent today" flag is
    stored there instead, with an atomic find-and-update so two near-
    simultaneous calls for the same family can't both win the race.

    Returns True if this call just claimed the slot (proceed to send),
    False if another call already claimed it today (skip — would duplicate).
    """
    existing = db.reminderlog.find_one_and_update(
        {"email": email_key, "date": date_key},
        {"$setOnInsert": {
            "email": email_key,
            "date": date_key,
            "sent_at": datetime.now(timezone.utc),
        }},
        upsert=True,
        return_document=ReturnDocument.BEFORE,  # None if this call just inserted it
    )
    return existing is None


def _release_reminder_slot(db, email_key: str, date_key: str) -> None:
    """Undo a reservation if the actual send subsequently failed."""
    db.reminderlog.delete_one({"email": email_key, "date": date_key})


@mcp.tool()
def send_reminder_email(student_email: str) -> str:
    """
    Send a polite fee-reminder email to a student/parent.
    If TEST_EMAIL env var is set, all emails are redirected there instead.

    A parent with multiple children shares one email address. This tool
    tracks (in MongoDB) whether a reminder was already sent to this email
    TODAY, so if you call it once per unpaid student, siblings after the
    first will get a "skipped" result and no duplicate reminder is sent.

    Args:
        student_email:  parent/student email from MongoDB
    """
    email_key = student_email.strip().lower()
    date_key = _now().strftime("%Y-%m-%d")
    db = _get_mongo_db()

    if not _reserve_reminder_slot(db, email_key, date_key):
        return json.dumps({
            "status": "skipped",
            "message": (
                f"Reminder already sent to {student_email} earlier today "
                f"(likely a sibling of this student) — not sending a duplicate."
            ),
        })

    try:
        now = _now()
        month_year = now.strftime("%b %Y")   # e.g. "Apr 2026"

        recipient = TEST_EMAIL if TEST_EMAIL else student_email

        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = recipient
        msg["Subject"] = f"Friendly Reminder: {month_year} Lesson Fee | SJ Piano Academy"
        if not TEST_EMAIL:
            msg["Bcc"] = BCC_EMAIL

        body = (
            f"Dear Parents,\n\n"
            f"I hope this message finds you well.\n"
            f"This is a friendly reminder that the piano lesson fee for {month_year} is due. "
            f"Please note the fee cycle is first week of every month. "
            f"Please let me know if I've missed anything.\n\n"
            f"Thank you."
        )
        msg.attach(MIMEText(body, "plain"))

        recipients = [recipient] if TEST_EMAIL else [student_email, BCC_EMAIL]
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(SMTP_USER, SMTP_APP_PASSWORD)
            smtp.sendmail(SMTP_USER, recipients, msg.as_string())

        return json.dumps({
            "status": "ok",
            "message": f"Reminder email sent to {recipient}"
                       + (f" (TEST MODE — original: {student_email})" if TEST_EMAIL else ""),
        })

    except Exception as e:
        # The send failed, so give up the reservation — otherwise this
        # family would be silently skipped for the rest of the day even
        # though they never actually got a reminder.
        _release_reminder_slot(db, email_key, date_key)
        return json.dumps({"status": "error", "message": str(e)})


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🎹 SJ Piano MCP Server starting...")
    mcp.run(transport="stdio")
