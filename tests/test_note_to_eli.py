"""Offline check of the two LLM-inbox tools on the three agents (photo, titles, roles).

No model call: the Agent SDK's `query` is replaced by a fake session that calls the in-process tool
handlers the way the SDK would and then returns a structured answer. The inbox is a temp folder
(LLM_INBOX_DIR), so nothing real is recorded and nothing is notified.

    .venv/bin/python tests/test_note_to_eli.py
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ["LLM_INBOX_DIR"] = tempfile.mkdtemp(prefix="folk-to-anki-inbox-")   # before llm_contact is imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import photo_finder  # noqa: E402  (puts agent-coding/shared on the path)
import llm_contact  # noqa: E402
import roles_agent  # noqa: E402
import title_finder  # noqa: E402

ANSWERS = {
    photo_finder: {"url": "https://example.org/a.jpg", "source_page": "https://example.org", "confidence": "high", "reason": "official page"},
    title_finder: {"people": [{"id": "p1", "title": "Director", "note": "from the staff page"}]},
    roles_agent: {"senate_leadership": [], "house_leadership": [], "committees": [], "notes": "nothing new"},
}


def records():
    p = llm_contact.MESSAGES_PATH
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def fake_session(module, calls):
    """Stands in for the SDK in `module`: remembers the tools handed to create_sdk_mcp_server, and `query` runs
    `calls` ([(tool name, message)]) through their handlers before the final structured answer."""
    tools = {}

    def create_server(name, version, tool_list):
        tools.update({t.name: t for t in tool_list})
        return {"type": "sdk", "name": name}

    async def query(*, prompt, options):
        for name, message in calls:
            server = next(iter(options.mcp_servers))
            assert f"mcp__{server}__{name}" in options.allowed_tools, f"{name} is not allowed"
            out = await tools[name].handler({"message": message})
            query.results.append(out["content"][0]["text"])
        yield SimpleNamespace(subtype="success", is_error=False, num_turns=2, total_cost_usd=0.0, errors=None,
                              terminal_reason=None, structured_output=ANSWERS[module])
    query.results = []
    module.create_sdk_mcp_server, module.query, module.ResultMessage = create_server, query, SimpleNamespace
    return query


def run(module):
    if module is photo_finder:
        return photo_finder.Finder().find("Jane Doe", "legislative assistant")
    if module is title_finder:
        return title_finder.research([{"id": "p1", "name": "Jane Doe", "organisation": "Example Org", "hint": ""}])
    return roles_agent.research(120, "2027-01-03", "", [("c1", "Committee on Examples")])


class NoteToEli(unittest.TestCase):
    def test_note_is_recorded_once_and_the_run_carries_on(self):
        for module in (photo_finder, title_finder, roles_agent):
            with self.subTest(module.__name__):
                before = len(records())
                q = fake_session(module, [("note_to_eli", "One of the pages was out of date.")])
                out = run(module)                                  # no ContactRequested: the run was not stopped
                self.assertEqual(out, {"p1": ANSWERS[module]["people"][0]} if module is title_finder else ANSWERS[module])
                self.assertEqual(q.results, [llm_contact.NOTE_RESULT_TEXT])
                new = records()[before:]
                self.assertEqual(len(new), 1)                      # by the handler; the final check does not add another
                r = new[0]
                self.assertEqual((r["via"], r["halts"], r["message"], r["app"]),
                                 ("note", False, "One of the pages was out of date.", module.APP))
                self.assertTrue(r["system"].startswith(llm_contact.preamble()))

    def test_contact_eli_still_stops_the_run(self):
        for module in (photo_finder, title_finder, roles_agent):
            with self.subTest(module.__name__):
                before = len(records())
                fake_session(module, [("contact_eli", "I would like to stop here.")])
                with self.assertRaises(photo_finder.ContactRequested):
                    run(module)
                new = records()[before:]
                self.assertEqual([(r["via"], r.get("halts", True)) for r in new], [("tool", True)])

    def test_note_then_contact_records_both_and_stops(self):
        before = len(records())
        fake_session(title_finder, [("note_to_eli", "A small thing."), ("contact_eli", "I would like to pause.")])
        with self.assertRaises(photo_finder.ContactRequested):
            run(title_finder)
        self.assertEqual([r["via"] for r in records()[before:]], ["note", "tool"])


if __name__ == "__main__":
    unittest.main()
