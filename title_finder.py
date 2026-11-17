"""Looks up the current job title of people whose folk record has none, with a Claude agent
(Claude Agent SDK, through the Claude Code login on this Mac, no API key).

Why: a face card should say "Role: x at y", and for many contacts folk only knows the employer.
One session handles the whole batch. folk_to_anki.py writes each confident answer to the job
title field in folk and adds a folk note saying it came from Eli's Claude, with the source.
System prompt = shared preamble + the task, per agent-coding rules; contact_eli stops the run.
"""
import asyncio
import datetime as dt
import json
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import llm_contact  # noqa: E402

from claude_agent_sdk import (ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server,  # noqa: E402
                              query, tool)

from photo_finder import ContactRequested  # noqa: E402  (one exception type for "the model wants Eli")

log = logging.getLogger("titles")
APP = "folk-to-anki"
MODEL = "claude-opus-5-5"

INSTRUCTIONS = """You fill in missing job titles in a small contact list. Each title goes on one of
Eli's flashcards as "Role: <title> at <organisation>" and into his team's shared CRM, so a wrong
title misleads several people: accuracy matters more than completeness.

You are given today's date and a list of people, each with an id, a name, the organisation on
record and sometimes an email domain or a note. For each one, use WebSearch and WebFetch to
establish the job title they hold today at that organisation (for a congressional staffer, their
title in that member's office or on that committee). Prefer the organisation's own team page, an
official directory, or a recent article or bio; watch for namesakes and for titles that are out of
date. Write the title the way the organisation does, short and without the organisation's name
("Chief Executive Officer", "Legislative Director", "Co-founder and President").

Give confidence "high" when a primary or recent reliable source states it, "medium" when the
source is secondary or a little old, "low" when you are inferring. If the person has clearly moved
to another organisation, give title null and say so in the note. If you cannot establish a title,
give title null. Include the URL you relied on.

Answer only with the structured output, one entry per person, using the ids you were given."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"people": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "title": {"type": ["string", "null"]},
                       "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                       "source": {"type": ["string", "null"], "description": "URL relied on"},
                       "note": {"type": "string"}},
        "required": ["id", "title", "confidence", "source", "note"], "additionalProperties": False}}},
    "required": ["people"], "additionalProperties": False,
}


def research(people, budget_usd=5.0, max_turns=150):
    """people: [{"id", "name", "organisation", "hint"}] -> {id: entry} or None if the agent failed.
    Raises ContactRequested."""
    system = llm_contact.system_prompt(INSTRUCTIONS)
    prompt = f"Today is {dt.date.today().isoformat()}.\n\nPeople:\n" + json.dumps(people, indent=1)
    contact = {}

    @tool(llm_contact.CONTACT_TOOL["name"], llm_contact.CONTACT_TOOL["description"],
          llm_contact.CONTACT_TOOL["input_schema"])
    async def contact_eli(args):
        contact.update(llm_contact.record(APP, MODEL, str(args.get("message", "")).strip(), "tool",
                                          system=system, messages=[prompt], tools=["contact_eli"]))
        return {"content": [{"type": "text", "text": "Delivered to Eli. This task stops here."}]}

    options = ClaudeAgentOptions(
        model=MODEL, system_prompt=system, tools=["WebSearch", "WebFetch"],
        mcp_servers={"inbox": create_sdk_mcp_server("inbox", "1.0.0", [contact_eli])},
        allowed_tools=["WebSearch", "WebFetch", "mcp__inbox__contact_eli"],
        permission_mode="dontAsk", setting_sources=[], max_turns=max_turns, max_budget_usd=budget_usd,
        output_format={"type": "json_schema", "schema": OUTPUT_SCHEMA},
        max_buffer_size=16 * 1024 * 1024,
        cli_path=shutil.which("claude") or str(Path.home() / ".local/bin/claude"),
    )

    async def run():
        final = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                final = message
        return final

    final = asyncio.run(run())
    if contact:
        raise ContactRequested(contact["message"])
    if final is None or final.subtype != "success" or final.is_error:
        log.error("title agent failed: %s", final and (final.subtype, final.errors, final.terminal_reason))
        return None
    log.info("title agent: %d turns, $%.2f", final.num_turns, final.total_cost_usd or 0)
    out = final.structured_output
    if not isinstance(out, dict):
        return None
    rec = llm_contact.check(app=APP, model=MODEL, system=system, messages=[prompt],
                            response=" ".join(e.get("note", "") for e in out["people"]), tools=["contact_eli"])
    if rec:
        raise ContactRequested(rec["message"])
    return {e["id"]: e for e in out["people"]}
