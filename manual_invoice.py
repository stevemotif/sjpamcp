"""
manual_invoice.py
Creates and emails an invoice for a student who pays outside the automated
Interac e-Transfer flow (e.g. cash) — these students are flagged
Manual/manual = true in pianostudents and are skipped by agent.py and
reminder_agent.py on purpose.

Reuses the same create_invoice / send_thank_you_email / check_invoice_exists
functions the automated payment tracker uses, so cash invoices look
identical to e-Transfer ones and share the same invoice-number sequence.

Usage:
    python manual_invoice.py --student "Yanish" --amount 120
    python manual_invoice.py --student "Yanish" --amount 120 --date 2026-09-01
    python manual_invoice.py --student "Yanish" --amount 120 --force
"""

import argparse
import json
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(override=False)  # env vars from GitHub Actions take priority

import mcp_server as m


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and email an invoice for a cash/manual-billing student."
    )
    parser.add_argument(
        "--student", required=True,
        help="Student name (matches pianostudents.studentname, case-insensitive)",
    )
    parser.add_argument("--amount", type=float, required=True, help="Amount paid, e.g. 120")
    parser.add_argument("--date", default=None, help="Payment date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--force", action="store_true",
        help="Create a new invoice even if one already exists for this student this month",
    )
    args = parser.parse_args()

    db = m._get_mongo_db()
    student = db.pianostudents.find_one({
        "studentname": {"$regex": f"^{args.student}$", "$options": "i"},
    })
    if not student:
        print(f"No student found matching '{args.student}'")
        return 1

    name = student["studentname"]
    email = student["email"]

    if args.date:
        paid_date = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    else:
        paid_date = m._now()
    date_iso = paid_date.isoformat()

    if not args.force:
        existing = json.loads(m.check_invoice_exists(email, name))
        if existing.get("exists"):
            inv = existing["invoice"]
            print(
                f"{name} already has an invoice this month "
                f"(#{inv['invoicenumber']}, ${inv['totalamount']}). "
                f"Pass --force to create another one anyway."
            )
            return 1

    result = json.loads(m.create_invoice(name, email, args.amount, date_iso))
    if result.get("status") != "ok":
        print("Failed to create invoice:", result)
        return 1

    invoice_number = result["invoice_number"]
    print(f"Invoice #{invoice_number} created for {name} ({email}), ${args.amount:.2f}")

    email_result = json.loads(m.send_thank_you_email(name, email, args.amount, invoice_number, date_iso))
    if email_result.get("status") != "ok":
        print("Invoice created but the receipt email failed:", email_result)
        return 1

    print(email_result["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
