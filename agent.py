"""
agent.py
LangChain + LangGraph agent that connects to the SJ Piano MCP server and
reasons through the full payment-tracking workflow.

Compatible with Python 3.14+ and langchain-mcp-adapters 0.1.0+

Run with:
    python agent.py
"""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv(override=False)  # env vars from GitHub Actions take priority

from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage


# ════════════════════════════════════════════════════════════════════════════
# System Prompt
# ════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a payment-tracking assistant for SJ Piano Academy.

Your job is to process Interac e-Transfer payment emails and ensure each payment
is properly recorded and acknowledged. Follow these steps EXACTLY and IN ORDER:

---
STEP 1 — Search Gmail
Call `search_interac_emails` to get all Interac e-Transfer emails from the
start of the current month up to now.

If no emails are found, report "No Interac e-Transfer emails found this month."
and stop.

---
STEP 2 — For each email found, do the following sub-steps:

  2a. VALIDATE STUDENT(S)
      Call `find_student_by_parent` with:
        - parent_name:    extracted from the email subject
        - reply_to_email: the reply-to address from the email
        - amount:         the dollar amount from the email subject

      This returns a "students" LIST, not a single student — it can contain
      MORE THAN ONE child. That happens when:
        a) SIBLINGS PAID TOGETHER — a parent with e.g. two $120 children
           pays $240 in one transfer. `find_student_by_parent` will match
           that $240 to the combination of both children's individual fees
           and return BOTH student records.
        b) SIBLINGS PAID SEPARATELY — the same parent sends two separate
           $120 transfers, one per child. Each email resolves to a list
           containing just the one sibling who does not already have an
           invoice this month.
      Either way, you must repeat sub-steps 2b–2d for EVERY student in the
      returned list — one invoice and one thank-you email PER CHILD, even
      though there was only one payment email. Two children always means
      two invoices, never one invoice covering both.

      If the result is "not_found", log a warning for this email and move
      on to the next one. Do NOT proceed with this email.

      If the result is "already_invoiced", every child matching that parent
      name/email already has an invoice this month — log it and move on.
      Do NOT create a duplicate.

  For EACH student in the "students" list returned by 2a, do:

  2b. CHECK FOR EXISTING INVOICE
      Call `check_invoice_exists` with:
        - student_email: this student's email from MongoDB
        - student_name:  this student's name from MongoDB

      Always pass both — siblings share the same email, so checking email
      alone would incorrectly mark an unpaid sibling as already invoiced.

      If an invoice already exists for this month:
        - Log: "Invoice already exists for <student_name> (<email>) — skipping."
        - Move on to the next student in the list. Do NOT create a duplicate.

  2c. CREATE INVOICE
      Call `create_invoice` with:
        - student_name:      from the MongoDB student record
        - student_email:     from the MongoDB student record
        - amount:            THIS STUDENT'S OWN "amount" field from MongoDB
                              — NOT the total dollar amount from the email.
                              When one payment covers multiple children,
                              each child's invoice must show only their own
                              individual fee.
        - fee_paid_date_iso: the date the email was received (ISO 8601 UTC)

  2d. SEND THANK-YOU EMAIL
      Call `send_thank_you_email` with:
        - student_name:      from MongoDB
        - student_email:     from MongoDB
        - amount:            THIS STUDENT'S OWN "amount" field from MongoDB
                              (same rule as 2c — never the combined email total)
        - invoice_number:    from the newly created invoice (step 2c)
        - fee_paid_date_iso: the date the email was received

---
STEP 3 — Final Report
After processing all emails, produce a clear summary:
  - How many emails were found
  - For each: student name(s), amount(s), action taken (processed / skipped / error)

---
IMPORTANT RULES:
- Never create a duplicate invoice for the same student in the same month.
- Only proceed if parent name and email match a student record.
- Always use the student's email from MongoDB (not the reply-to) for sending.
- A parent with 2 children needs 2 separate invoices and 2 thank-you emails
  (one per child) — never combine siblings into a single invoice, and never
  put the combined payment total on one child's invoice.
- Be methodical. Process one email at a time, completing all sub-steps for
  every student it resolves to before moving on to the next email.
"""


# ════════════════════════════════════════════════════════════════════════════
# Agent
# ════════════════════════════════════════════════════════════════════════════

async def run_agent():
    # ── Connect to MCP server (no context manager in 0.1.0+) ──────────────
    mcp_client = MultiServerMCPClient(
        {
            "sjpiano": {
                "command": "python",
                "args": ["mcp_server.py"],
                "transport": "stdio",
                "env": dict(os.environ),  # explicitly pass all env vars to subprocess
            }
        }
    )

    # Direct call — no "async with" needed in langchain-mcp-adapters 0.1.0+
    tools = await mcp_client.get_tools()

    llm = ChatAnthropic(
        model="claude-opus-4-6",
        api_key=os.getenv("API_KEY"),
        temperature=0,
    )

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=SYSTEM_PROMPT,
    )

    print("\n" + "=" * 60)
    print("  SJ Piano Academy - Payment Tracker Agent")
    print("=" * 60 + "\n")

    async for event in agent.astream_events(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "Please process all Interac e-Transfer payment emails "
                        "for this month. Follow your instructions step by step."
                    )
                )
            ]
        },
        version="v2",
        config={"recursion_limit": 150},
    ):
        kind = event.get("event")

        if kind == "on_tool_start":
            tool_name = event.get("name", "unknown_tool")
            print(f"\n[TOOL CALL] {tool_name}")
            inp = event.get("data", {}).get("input", {})
            if inp:
                for k, v in inp.items():
                    print(f"  {k}: {v}")

        elif kind == "on_tool_end":
            tool_name = event.get("name", "unknown_tool")
            output = event.get("data", {}).get("output", "")
            print(f"[TOOL RESULT] {tool_name}: {str(output)[:300]}")

        elif kind == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk and hasattr(chunk, "content"):
                content = chunk.content
                if isinstance(content, str) and content:
                    print(content, end="", flush=True)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            print(block.get("text", ""), end="", flush=True)

    print("\n\n" + "=" * 60)
    print("Agent finished.")
    print("=" * 60)


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    asyncio.run(run_agent())