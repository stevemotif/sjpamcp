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
from pymongo import MongoClient

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
        now = datetime.now(timezone.utc)
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


@mcp.tool()
def find_student_by_parent(parent_name: str, reply_to_email: str, amount: float) -> str:
    """
    Look up a student in the pianostudents collection by matching:
      - ParentName  == parent_name  (case-insensitive)
      - email       == reply_to_email
      - amount      == str(int(amount))  e.g. "200"

    Returns the student document as JSON, or an error message.
    """
    try:
        db = _get_mongo_db()
        # Amount stored as string like "200"
        amount_str = str(int(amount))

        student = db.pianostudents.find_one({
            "ParentName": {"$regex": f"^{re.escape(parent_name)}$", "$options": "i"},
            "email": {"$regex": f"^{re.escape(reply_to_email)}$", "$options": "i"},
            "amount": amount_str,
        })

        if not student:
            return json.dumps({
                "status": "not_found",
                "message": (
                    f"No active student found for parent='{parent_name}', "
                    f"email='{reply_to_email}', amount='{amount_str}'"
                ),
            })

        # Convert ObjectId to string for JSON serialisation
        student["_id"] = str(student["_id"])
        return json.dumps({"status": "ok", "student": student})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def check_invoice_exists(student_email: str) -> str:
    """
    Check whether an invoice already exists in the invoices collection
    for the given student email in the CURRENT calendar month.

    Returns JSON: { "exists": true/false, "invoice": <doc or null> }
    """
    try:
        db = _get_mongo_db()

        now = datetime.now(timezone.utc)
        # Start and end of current month
        start_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            end_of_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end_of_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)

        invoice = db.invoices.find_one({
            "students.email": {"$regex": f"^{re.escape(student_email)}$", "$options": "i"},
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
        msg["To"] = student_email
        msg["Subject"] = f"Receipt for lesson payment {month_year} | SJ Piano Academy"
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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(SMTP_USER, SMTP_APP_PASSWORD)
            smtp.sendmail(SMTP_USER, [student_email, BCC_EMAIL], msg.as_string())

        return json.dumps({
            "status": "ok",
            "message": f"Thank you email sent to {student_email}",
            "receipt_number": invoice_number,
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🎹 SJ Piano MCP Server starting...")
    mcp.run(transport="stdio")
