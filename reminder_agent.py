"""
reminder_agent.py
LangGraph agent that sends fee-reminder emails to active students who have
not yet paid or been invoiced for the current month.

Run with:
    python reminder_agent.py
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

SYSTEM_PROMPT = """You are a fee-reminder assistant for SJ Piano Academy.

Your job is to identify active students who have NOT yet paid for the current
month and send them a polite reminder email. Follow these steps EXACTLY and IN ORDER:

---
STEP 1 — Fetch active students
Call `get_active_students` to retrieve all students where:
  - Status is Active
  - Manual field is absent, null, or false

If no active students are found, report "No active students found." and stop.

---
STEP 2 — Fetch this month's Interac emails
Call `search_interac_emails` once to get all payment emails received this month.
Extract the list of parent names from the email subjects (the "parent_name" field).
Keep this list in memory — you will reference it for every student below.

---
STEP 3 — For each active student, decide whether to send a reminder:

  3a. CHECK EMAIL RECEIVED
      Look at the list of parent names from STEP 2.
      If ANY email's parent_name matches this student's ParentName field
      (case-insensitive), the student has already paid via e-Transfer.
      Mark as PAID and move to the next student. Do NOT send a reminder.

  3b. CHECK INVOICE IN DATABASE
      Call `check_invoice_exists` with the student's email from MongoDB.
      If an invoice already exists for this month, mark as PAID and move on.
      Do NOT send a reminder.

  3c. SEND REMINDER
      If NEITHER a matching email NOR an invoice exists, call `send_reminder_email`
      with:
        - student_email: from the MongoDB student record

---
STEP 4 — Final Report
After processing all students, produce a clear summary:
  - Total active students checked
  - Students skipped (already paid via email or invoice)
  - Students sent a reminder (list their names and emails)
  - Any errors encountered

---
IMPORTANT RULES:
- Never send a reminder if the student already has a payment email or invoice this month.
- Process one student at a time, completing all checks before moving on.
- The ParentName comparison is case-insensitive.
"""


# ════════════════════════════════════════════════════════════════════════════
# Agent
# ════════════════════════════════════════════════════════════════════════════

async def run_agent():
    mcp_client = MultiServerMCPClient(
        {
            "sjpiano": {
                "command": "python",
                "args": ["mcp_server.py"],
                "transport": "stdio",
                "env": dict(os.environ),
            }
        }
    )

    tools = await mcp_client.get_tools()

    llm = ChatAnthropic(
        model="claude-opus-4-8",
        api_key=os.getenv("API_KEY"),
    )

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=SYSTEM_PROMPT,
    )

    print("\n" + "=" * 60)
    print("  SJ Piano Academy - Fee Reminder Agent")
    print("=" * 60 + "\n")

    async for event in agent.astream_events(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "Please check all active students and send fee reminder "
                        "emails to those who have not yet paid for this month."
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
