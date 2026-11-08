"""Finds a headshot for one person with a Claude agent (Claude Agent SDK, so it runs through the
Claude Code login on this Mac, no separate API key).

The agent gets WebSearch and WebFetch plus three in-process tools: `page_images` lists the
images on a web page, `view_image` shows it one so it can check that the face is the right
person, and `contact_eli` is the shared LLM-inbox escape hatch. It answers with a structured
{url, source_page, confidence, reason}; we then download the URL ourselves to prove it is an
image before anyone uses it. System prompt = shared preamble + the task, per agent-coding rules.
"""
import asyncio
import base64
import html as html_mod
import logging
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import llm_contact  # noqa: E402  (shared/llm_contact.py: system_prompt(), CONTACT_TOOL, record())

from claude_agent_sdk import (ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server,  # noqa: E402
                              query, tool)

log = logging.getLogger("photos")
APP = "folk-to-anki"
MODEL = "claude-opus-5-5"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) folk-to-anki/1.0"}
MAX_IMAGE_BYTES = 3_500_000           # the API caps images at 5 MB; leave room for base64

INSTRUCTIONS = """You find one good photo of a specific person for Eli's names-and-faces flashcards.

Return the direct URL of a clear, recent, front-facing photo of the right person, ideally a
professional headshot. Prefer official or primary sources: the person's employer's team or staff
page, a congressional office or committee page, a university or think-tank profile, a conference
speaker page, a byline photo, a news article about them. LinkedIn cannot be fetched; do not try.

Method: WebSearch for the person with their role or organisation; WebFetch or page_images on the
most promising pages (page_images lists every image with its alt text and size); view_image on
the best one to three candidates and check it is a single person, face clearly visible, not a
logo, group shot, or someone else with the same name. Reject anything you cannot confirm is this
person. If nothing good turns up after a reasonable search, answer with url null; a wrong photo
is worse than no photo.

Answer only with the structured output: url, source_page, confidence (high / medium / low), and a
one-sentence reason."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": ["string", "null"], "description": "direct image URL, or null"},
        "source_page": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
    "required": ["url", "source_page", "confidence", "reason"],
    "additionalProperties": False,
}


class ContactRequested(Exception):
    """The model asked to talk to Eli; the whole run must stop."""


def _get(url, limit=MAX_IMAGE_BYTES):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        data = r.read(limit + 1)
    return ctype, data


def extract_images(page_url, text):
    out, seen = [], set()
    og = re.findall(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', text, re.I)
    for u in og:
        u = urllib.parse.urljoin(page_url, html_mod.unescape(u))
        if u not in seen:
            seen.add(u)
            out.append(f"{u}  (og:image, the page's main picture)")
    for m in re.finditer(r"<img\b([^>]*)>", text, re.I):
        attrs = dict(re.findall(r'([a-zA-Z-]+)\s*=\s*["\']([^"\']*)["\']', m.group(1)))
        src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src") or ""
        if not src or src.startswith("data:") or src.lower().endswith(".svg"):
            continue
        u = urllib.parse.urljoin(page_url, html_mod.unescape(src.strip()))
        try:
            w, h = int(attrs.get("width") or 0), int(attrs.get("height") or 0)
        except ValueError:
            w = h = 0
        if (w and w < 60) or (h and h < 60):        # icons
            continue
        if u in seen:
            continue
        seen.add(u)
        note = ", ".join(x for x in (f"alt=\"{attrs.get('alt', '').strip()}\"" if attrs.get("alt") else "",
                                     f"{w}x{h}" if w or h else "") if x)
        out.append(f"{u}  {note}".rstrip())
    return out


@tool("page_images", "List the images on a web page (URL, alt text, size) so you can pick candidates "
      "to view with view_image.", {"url": str})
async def page_images(args):
    try:
        ctype, data = await asyncio.to_thread(_get, args["url"], 6_000_000)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"content": [{"type": "text", "text": f"fetch failed: {e}"}], "is_error": True}
    if "html" not in ctype:
        return {"content": [{"type": "text", "text": f"not an HTML page ({ctype})"}], "is_error": True}
    imgs = extract_images(args["url"], data.decode("utf-8", "replace"))
    text = "\n".join(imgs[:60]) or "no images found on the page"
    return {"content": [{"type": "text", "text": text}]}


@tool("view_image", "Download an image URL and look at it, to confirm it shows the right person clearly.",
      {"url": str})
async def view_image(args):
    try:
        ctype, data = await asyncio.to_thread(_get, args["url"])
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"content": [{"type": "text", "text": f"fetch failed: {e}"}], "is_error": True}
    if not ctype.startswith("image/") or ctype in ("image/svg+xml",):
        return {"content": [{"type": "text", "text": f"not a usable image (content-type {ctype})"}],
                "is_error": True}
    if len(data) > MAX_IMAGE_BYTES:
        return {"content": [{"type": "text", "text": "image is over 3.5 MB; pick a smaller version"}],
                "is_error": True}
    return {"content": [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": ctype},
                        {"type": "text", "text": f"{args['url']} ({ctype}, {len(data)} bytes)"}]}


class Finder:
    def __init__(self, budget_usd=1.0, max_turns=30):
        self.budget_usd = budget_usd
        self.max_turns = max_turns
        self.contact = None
        self.system = llm_contact.system_prompt(INSTRUCTIONS)

        @tool(llm_contact.CONTACT_TOOL["name"], llm_contact.CONTACT_TOOL["description"],
              llm_contact.CONTACT_TOOL["input_schema"])
        async def contact_eli(args):
            self.contact = llm_contact.record(APP, MODEL, str(args.get("message", "")).strip(), "tool",
                                              system=self.system, messages=[self._prompt], tools=["contact_eli"])
            return {"content": [{"type": "text", "text": "Delivered to Eli. This task stops here."}]}

        self.server = create_sdk_mcp_server("photos", "1.0.0", [page_images, view_image, contact_eli])

    def find(self, name, context):
        """-> {"url", "source_page", "confidence", "reason"} or None. Raises ContactRequested."""
        self._prompt = f"Find a photo of: {name}\nWhat we know: {context or 'nothing beyond the name'}"
        self.contact = None
        result = asyncio.run(self._run())
        if self.contact:
            raise ContactRequested(self.contact["message"])
        if result is None:
            return None
        rec = llm_contact.check(app=APP, model=MODEL, system=self.system, messages=[self._prompt],
                                response=result.get("reason", ""), tools=["contact_eli"])
        if rec:
            raise ContactRequested(rec["message"])
        return result

    async def _run(self):
        options = ClaudeAgentOptions(
            model=MODEL,
            system_prompt=self.system,
            tools=["WebSearch", "WebFetch"],
            mcp_servers={"photos": self.server},
            allowed_tools=["WebSearch", "WebFetch", "mcp__photos__page_images", "mcp__photos__view_image",
                           "mcp__photos__contact_eli"],
            permission_mode="dontAsk",
            setting_sources=[],
            max_turns=self.max_turns,
            max_budget_usd=self.budget_usd,
            output_format={"type": "json_schema", "schema": OUTPUT_SCHEMA},
            cli_path=shutil.which("claude") or str(Path.home() / ".local/bin/claude"),
        )
        final = None
        async for message in query(prompt=self._prompt, options=options):
            if isinstance(message, ResultMessage):
                final = message
        if final is None:
            log.error("photo agent returned no result")
            return None
        log.info("photo agent: %s in %d turns, $%.2f", final.subtype, final.num_turns, final.total_cost_usd or 0)
        if self.contact:
            return None
        if final.subtype != "success" or final.is_error:
            log.error("photo agent failed: %s %s %s", final.subtype, final.errors, final.terminal_reason)
            return None
        out = final.structured_output
        return out if isinstance(out, dict) else None


def verify_image(url):
    """True when the URL really serves an image we can store."""
    try:
        ctype, data = _get(url)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("photo URL %s failed: %s", url, e)
        return False
    return ctype.startswith("image/") and ctype != "image/svg+xml" and 2000 < len(data) <= MAX_IMAGE_BYTES


if __name__ == "__main__":                      # manual trial: photo_finder.py "Name" "context"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    name, ctx = sys.argv[1], " ".join(sys.argv[2:])
    res = Finder().find(name, ctx)
    print(res)
    if res and res.get("url"):
        print("verified image:", verify_image(res["url"]))
