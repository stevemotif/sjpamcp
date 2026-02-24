# 🎹 SJ Piano Academy — Payment Tracker

An AI agent that automatically processes Interac e-Transfer payment emails,
validates them against student records, creates invoices in MongoDB, and sends
PDF receipts to students.

---

## Architecture

```
agent.py  (LangChain Agent + Claude)
    │
    │  stdio
    ▼
mcp_server.py  (Single MCP Server)
    ├── search_interac_emails     → Gmail API
    ├── find_student_by_parent    → MongoDB: pianostudents
    ├── check_invoice_exists      → MongoDB: invoices
    ├── create_invoice            → MongoDB: invoices
    └── send_thank_you_email      → Gmail SMTP + receipt_generator.py
```

---

## Project Structure

```
sj-piano-agent/
├── agent.py              ← LangChain agent (run this)
├── mcp_server.py         ← MCP server with all tools
├── receipt_generator.py  ← PDF receipt generator (matches SJ Piano Academy style)
├── requirements.txt      ← Python dependencies
├── .env.example          ← Copy to .env and fill in your credentials
├── credentials.json      ← Gmail OAuth2 credentials (you provide this)
└── token.json            ← Auto-generated after first Gmail auth
```

---

## Setup

### 1. Clone / copy this folder, then install dependencies

```bash
cd sj-piano-agent
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in all values (see section below).

### 3. Set up Gmail API (OAuth2 — for reading emails)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use existing)
3. Enable the **Gmail API**
4. Go to **Credentials → Create Credentials → OAuth 2.0 Client ID**
5. Choose **Desktop App**, download the JSON
6. Rename it to `credentials.json` and place it in this folder
7. First run will open a browser for you to authorise — `token.json` is saved automatically

### 4. Set up Gmail App Password (for sending emails via SMTP)

1. Ensure **2-Step Verification** is enabled on your Google account
2. Go to [App Passwords](https://myaccount.google.com/apppasswords)
3. Create a new App Password → select **Mail** → **Other (custom name)** → `SJ Piano`
4. Copy the 16-character password into `.env` as `GMAIL_SMTP_APP_PASSWORD`

### 5. Start MongoDB locally

```bash
# macOS (Homebrew)
brew services start mongodb-community

# Ubuntu / Debian
sudo systemctl start mongod

# Windows
net start MongoDB
```

Make sure the `sjpiano` database exists with `pianostudents` and `invoices` collections.

### 6. Run the agent

```bash
python agent.py
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_CREDENTIALS_FILE` | Path to Gmail OAuth2 credentials JSON (default: `credentials.json`) |
| `GMAIL_TOKEN_FILE` | Path to auto-saved OAuth2 token (default: `token.json`) |
| `GMAIL_SMTP_USER` | Your Gmail address (e.g. `you@gmail.com`) |
| `GMAIL_SMTP_APP_PASSWORD` | 16-char Gmail App Password |
| `MONGO_URI` | MongoDB connection string (default: `mongodb://localhost:27017`) |
| `MONGO_DB_NAME` | MongoDB database name (default: `sjpiano`) |
| `ACADEMY_NAME` | Printed on receipts (default: `SJ Piano Academy.`) |
| `ACADEMY_ADDRESS` | Printed on receipts |
| `ACADEMY_CITY` | Printed on receipts |

---

## How It Works (Step by Step)

```
1. Agent starts and calls search_interac_emails
   └── Searches Gmail from 1st of month to now
   └── Looks for subjects containing "Interac e-Transfer" + "automatically deposited"
   └── Returns: subject, reply-to email, date received, parent name, amount

2. For each email:
   ├── find_student_by_parent(parent_name, reply_to_email, amount)
   │   └── Matches ParentName + email + amount in pianostudents
   │   └── If no match → skip (logs warning)
   │
   ├── check_invoice_exists(student_email)
   │   └── Checks invoices collection for same email + current month
   │   └── If exists → skip (no duplicate)
   │
   ├── create_invoice(...)
   │   └── Inserts new invoice document with timestamp invoice number
   │
   └── send_thank_you_email(...)
       └── Generates PDF receipt (matching SJ Piano Academy style)
       └── Emails it to the student's address from MongoDB
       └── Subject: "Receipt for lesson payment Feb 2026 | SJ Piano Academy"

3. Final report printed to console
```

---

## Validation Rules

The agent only processes an email if ALL THREE match:

| Email Field | MongoDB Field |
|---|---|
| Parent name from subject | `pianostudents.ParentName` |
| Reply-To address | `pianostudents.email` |
| Dollar amount from subject | `pianostudents.amount` |

---

## Invoice Document Created

```json
{
  "invoicenumber": "1764355491540",
  "students": {
    "name": "Yanish",
    "address": "",
    "email": "stevemotif@gmail.com",
    "phone": ""
  },
  "totalamount": 200.0,
  "tax": 0,
  "feepaiddate": "<actual date email was received>",
  "paymentstatus": "Paid",
  "items": [],
  "dateissued": 1764355491540,
  "__v": 0
}
```

---

## Troubleshooting

**`credentials.json not found`**
→ Download it from Google Cloud Console and place it in the project folder.

**`token.json` auth error**
→ Delete `token.json` and re-run — it will prompt you to re-authorise.

**No emails found but they exist**
→ Check the Gmail query — make sure the subject matches exactly. You can test
  the query directly in Gmail search bar.

**MongoDB connection refused**
→ Make sure `mongod` is running locally on port 27017.

**SMTP auth error**
→ Make sure you're using an App Password (not your regular Gmail password).
  Regular passwords won't work if 2FA is enabled.
