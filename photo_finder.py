"""Finds a headshot for one person with a Claude agent (Claude Agent SDK, so it runs through the
Claude Code login on this Mac, no separate API key).

The agent gets WebSearch and WebFetch plus four in-process tools: `page_images` lists the
images on a web page, `image_search` returns image-search results, `view_image` shows it one so
it can check that the face is the right person, and `contact_eli` is the shared LLM-inbox escape hatch. It answers with a structured
{url, source_page, confidence, reason}; we then download the URL ourselves to prove it is an
image before anyone uses it. System prompt = shared preamble + the task, per agent-coding rules.
"""
import asyncio
import base64
import html as html_mod
import json
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
# A browser-like agent: image search and many staff pages answer a bare script with nothing.
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Safari/605.1.15"}
MAX_IMAGE_BYTES = 3_500_000           # the API caps images at 5 MB; leave room for base64

INSTRUCTIONS = """You find one good photo of a specific person for Eli's names-and-faces flashcards.
The photo card is the most important card he has for each person, and without a photo there is no
card, so a thorough search is worth the time: keep going well past the first few dead ends.

Return the direct URL of a clear, front-facing photo of the right person, ideally a professional
headshot. A less polished photo is fine when it clearly shows the face and you can tie it to this
person.

Where to look, roughly in this order, moving on when one runs dry:
- official and primary pages: the employer's team or staff page, a congressional office or
  committee page, a university, fellowship or think-tank profile, a conference or panel speaker
  page, a byline photo, a news article about them;
- image_search with the name plus the organisation, role, a former employer or school; try several
  phrasings. Its results often surface profile pictures (LinkedIn, X, Bluesky, speaker pages) whose
  pages cannot be fetched directly. Each result also lists the search engine's own copy of the
  picture: when the original will not load (LinkedIn's usually will not), view and return that
  copy instead; it is smaller but good enough. LinkedIn pages themselves cannot be fetched;
- earlier jobs and schools you learn about along the way: alumni pages, student newspapers,
  fellowship cohorts, past employers' team pages, event recaps, podcast or video guest pages;
- pages that have been taken down: try https://web.archive.org/web/2024/<the page URL>.

Check every candidate with view_image: one person, face clearly visible, not a logo or a group
shot. Then make sure it is this person and not a namesake: the page or caption should connect the
photo to something we know about them (employer, role, city, field). Reject what you cannot
connect. A wrong photo is worse than no photo, so if nothing can be confirmed after a thorough
search, answer with url null and say what you tried.

If the record has no name, work out who the person is from the organisation and the email hint,
and give their full name in `name`; leave `name` null when the record already had one or you
cannot establish it.

Answer only with the structured output: url, source_page, confidence (high / medium / low), name,
and a one-sentence reason."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": ["string", "null"], "description": "direct image URL, or null"},
        "source_page": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "name": {"type": ["string", "null"], "description": "full name, only when the record had none"},
        "reason": {"type": "string"},
    },
    "required": ["url", "source_page", "confidence", "name", "reason"],
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


def parse_image_results(text):
    """Bing image results page -> ["<image url>  title=... page=... search-engine copy=..."]. Each
    result is an <a class="iusc" m="{json}"> whose json has murl (image), purl (page), t (title)
    and turl (Bing's own smaller copy, which still loads when the original refuses or has expired,
    as LinkedIn's do)."""
    out = []
    for m in re.findall(r'class="iusc"[^>]*?\sm="([^"]+)"', text):
        try:
            d = json.loads(html_mod.unescape(m))
        except ValueError:
            continue
        if d.get("murl"):
            out.append(f"{d['murl']}  title=\"{d.get('t', '')}\" page={d.get('purl', '')}"
                       + (f" search-engine copy={d['turl']}" if d.get("turl") else ""))
    return out


@tool("image_search", "Search the web for images. Returns image URLs with the title and page each came from; "
      "check candidates with view_image and confirm the source page before trusting one.", {"query": str})
async def image_search(args):
    url = "https://www.bing.com/images/search?" + urllib.parse.urlencode({"q": args["query"], "form": "HDRSC2"})
    try:
        _, data = await asyncio.to_thread(_get, url, 6_000_000)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"content": [{"type": "text", "text": f"image search failed: {e}"}], "is_error": True}
    results = parse_image_results(data.decode("utf-8", "replace"))
    return {"content": [{"type": "text", "text": "\n".join(results[:25]) or "no image results"}]}


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
    def __init__(self, budget_usd=5.0, max_turns=120):
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

        self.server = create_sdk_mcp_server("photos", "1.0.0", [page_images, image_search, view_image, contact_eli])

    def find(self, name, context):
        """-> {"url", "source_page", "confidence", "reason"} or None. Raises ContactRequested."""
        self._prompt = f"Find a photo of: {name or '(no name on record)'}\nWhat we know: {context or 'nothing beyond the name'}"
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
            allowed_tools=["WebSearch", "WebFetch", "mcp__photos__page_images", "mcp__photos__image_search",
                           "mcp__photos__view_image",
                           "mcp__photos__contact_eli"],
            permission_mode="dontAsk",
            setting_sources=[],
            max_turns=self.max_turns,
            max_budget_usd=self.budget_usd,
            output_format={"type": "json_schema", "schema": OUTPUT_SCHEMA},
            # view_image hands the model a base64 image of up to 3.5 MB in one JSON message; the
            # SDK's default 1 MB line buffer raises CLIJSONDecodeError on the first big picture.
            max_buffer_size=16 * 1024 * 1024,
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
