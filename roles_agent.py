"""Keeps the incoming Congress's leadership posts and committee leaders up to date with a Claude
agent (Claude Agent SDK, through the Claude Code login on this Mac, no API key).

Why an agent: between an election and the day the public @unitedstates data describes the new
Congress, posts are decided one party vote at a time (leaders in November, committee chairs in
December and January, the speaker on the first day). Nothing structured publishes that, so the
agent searches the news, and answers with every post it could establish, each marked confirmed
(the vote or the official announcement has happened) or expected, each with a source URL.
folk_to_anki.py stores the answer in congress-roles.json and only confirmed posts become cards.
System prompt = shared preamble + the task, per agent-coding rules; contact_eli stops the run.
"""
import asyncio
import datetime as dt
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import llm_contact  # noqa: E402

from claude_agent_sdk import (ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server,  # noqa: E402
                              query, tool)

from photo_finder import ContactRequested  # noqa: E402  (one exception type for "the model wants Eli")

log = logging.getLogger("roles")
APP = "folk-to-anki"
MODEL = "claude-opus-5-5"

INSTRUCTIONS = """You keep a small fact file about the incoming United States Congress accurate. It
feeds Eli's flashcards about who holds which post, so a wrong entry teaches him something false:
accuracy matters more than completeness.

You are given today's date, the Congress in question, the posts currently on file, and a list of
committees. Using WebSearch and WebFetch, establish who will hold, in that Congress:
- the floor and party leadership posts of both chambers (Speaker, majority and minority leaders
  and whips, President pro tempore, and the conference or caucus chairs);
- the chair and the ranking member of each listed committee.

Mark each post "confirmed" only if the deciding step has actually happened and a reliable source
says so: the party conference or caucus has voted or formally announced it, or the chamber has
voted. Mark it "expected" if it is only projected, uncontested but not yet voted on, a nominee
awaiting a floor vote, or inferred from seniority. Leave out anything you cannot support at all.
Re-check the posts on file too: an "expected" post may now be confirmed, or may have gone to
someone else. Prefer primary and major news sources and give the URL you relied on for each
entry. Each leadership title becomes a flashcard prompt on its own ("Senate Republican Conference
Chair:"), so it must point to exactly one person: always include the chamber and, for party posts,
the party; never a bare "Conference Chair" or "Whip". Use people's full names as Congress lists them, and the exact committee id you were given.

Answer only with the structured output."""

POST = {"type": "object",
        "properties": {"name": {"type": "string"}, "title": {"type": "string"},
                       "status": {"type": "string", "enum": ["confirmed", "expected"]},
                       "source": {"type": "string", "description": "URL relied on"}},
        "required": ["name", "title", "status", "source"], "additionalProperties": False}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "senate_leadership": {"type": "array", "items": POST},
        "house_leadership": {"type": "array", "items": POST},
        "committees": {"type": "array", "items": {
            "type": "object",
            "properties": {"committee_id": {"type": "string"}, "name": {"type": "string"},
                           "title": {"type": "string", "enum": ["chair", "ranking member"]},
                           "status": {"type": "string", "enum": ["confirmed", "expected"]},
                           "source": {"type": "string"}},
            "required": ["committee_id", "name", "title", "status", "source"], "additionalProperties": False}},
        "notes": {"type": "string", "description": "anything Eli should know, e.g. a vote date coming up"},
    },
    "required": ["senate_leadership", "house_leadership", "committees", "notes"],
    "additionalProperties": False,
}


def research(congress_number, starts, on_file, committees, budget_usd=5.0, max_turns=80):
    """-> the OUTPUT_SCHEMA dict, or None if the agent failed. Raises ContactRequested.
    on_file: text describing the posts currently recorded; committees: [(id, name)]."""
    system = llm_contact.system_prompt(INSTRUCTIONS)
    prompt = (f"Today is {dt.date.today().isoformat()}. The Congress in question is the {congress_number}th, "
              f"which convenes on {starts}.\n\nPosts currently on file:\n{on_file or '(none)'}\n\n"
              "Committees (id: name):\n" + "\n".join(f"{cid}: {name}" for cid, name in committees))
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
        log.error("roles agent failed: %s", final and (final.subtype, final.errors, final.terminal_reason))
        return None
    log.info("roles agent: %d turns, $%.2f", final.num_turns, final.total_cost_usd or 0)
    out = final.structured_output
    if not isinstance(out, dict):
        return None
    rec = llm_contact.check(app=APP, model=MODEL, system=system, messages=[prompt],
                            response=out.get("notes", ""), tools=["contact_eli"])
    if rec:
        raise ContactRequested(rec["message"])
    return out
