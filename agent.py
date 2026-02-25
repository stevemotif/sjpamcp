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

  2a. VALIDATE STUDENT
      Call `find_student_by_parent` with:
        - parent_name:    extracted from the email subject
        - reply_to_email: the reply-to address from the email
        - amount:         the dollar amount from the email subject

      If no matching student is found, log a warning for this email and
      move on to the next one. Do NOT proceed with this email.

  2b. CHECK FOR EXISTING INVOICE
      Call `check_invoice_exists` with the student's email from MongoDB.

      If an invoice already exists for this month:
        - Log: "Invoice already exists for <student_name> (<email>) — skipping."
        - Move on to the next email. Do NOT create a duplicate.

  2c. CREATE INVOICE
      Call `create_invoice` with:
        - student_name:      from the MongoDB student record
        - student_email:     from the MongoDB student record
        - amount:            from the email
        - fee_paid_date_iso: the date the email was received (ISO 8601 UTC)

  2d. SEND THANK-YOU EMAIL
      Call `send_thank_you_email` with:
        - student_name:      from MongoDB
        - student_email:     from MongoDB
        - amount:            from the email
        - invoice_number:    from the newly created invoice (step 2c)
        - fee_paid_date_iso: the date the email was received

---
STEP 3 — Final Report
After processing all emails, produce a clear summary:
  - How many emails were found
  - For each: student name, amount, action taken (processed / skipped / error)

---
IMPORTANT RULES:
- Never create a duplicate invoice for the same student in the same month.
- Only proceed if parent name, email, AND amount ALL match a student record.
- Always use the student's email from MongoDB (not the reply-to) for sending.
- Be methodical. Process one email at a time, completing all sub-steps before moving on.
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