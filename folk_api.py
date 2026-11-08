"""Thin client for the folk CRM REST API (https://developer.folk.app), stdlib only.

Everything the rest of the program needs from folk goes through here: list the people
in a group, find the group and its custom fields, create missing fields, and update one
person's custom field values. Respects folk's 20-requests-per-2-seconds burst limit by
sleeping on 429.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("folk")
BASE = "https://api.folk.app/v1"

# Custom fields the integration owns, created on FOLK_GROUP if missing.
# Select colours must come from folk's fixed list.
OWNED_FIELDS = [
    {"type": "singleSelect", "name": "Meeting status",
     "options": [{"label": "Met", "color": "#20cea9"}, {"label": "Planning to meet", "color": "#4a90e2"}]},
    {"type": "dateField", "name": "Last met"},
    {"type": "dateField", "name": "Next meeting"},
    {"type": "singleSelect", "name": "Anki card",
     "options": [{"label": "Congressperson", "color": "#5738ff"}, {"label": "DC staffer", "color": "#f5a623"},
                 {"label": "Networking", "color": "#20cea9"}, {"label": "Skip", "color": "#879aab"}]},
    {"type": "singleSelect", "name": "Party",
     "options": [{"label": "Democrat", "color": "#4a90e2"}, {"label": "Republican", "color": "#f54e50"},
                 {"label": "Independent", "color": "#879aab"}]},
    {"type": "textField", "name": "Office"},
    {"type": "textField", "name": "Deal"},
    {"type": "textField", "name": "Photo URL"},
]


class FolkError(Exception):
    pass


class Folk:
    def __init__(self, api_key):
        if not api_key:
            raise FolkError("FOLK_API_KEY is empty; put it in config.env")
        self.api_key = api_key

    def _call(self, method, path, params=None, body=None):
        url = path if path.startswith("http") else BASE + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "User-Agent": "folk-to-anki"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")[:500]
                if e.code == 429 and attempt < 4:
                    wait = float(e.headers.get("Retry-After") or 2)
                    log.info("folk rate limit, sleeping %.0fs", wait)
                    time.sleep(wait)
                    continue
                raise FolkError(f"{method} {url} -> HTTP {e.code}: {text}") from None
            except urllib.error.URLError as e:
                raise FolkError(f"{method} {url} failed: {e.reason}") from None

    def _pages(self, path, params=None):
        params = dict(params or {}, limit=100)
        url, out = path, []
        while url:
            res = self._call("GET", url, params if url == path else None)
            out.extend(res["data"]["items"])
            url = res["data"].get("pagination", {}).get("nextLink")
        return out

    # --- groups and fields -------------------------------------------------------------------
    def groups(self):
        return self._pages("/groups")

    def group_by_name(self, name):
        for g in self.groups():
            if g["name"].strip().lower() == name.strip().lower():
                return g
        raise FolkError(f"folk group {name!r} not found; groups are: "
                        + ", ".join(g["name"] for g in self.groups()))

    def group_fields(self, group_id):
        return self._pages(f"/groups/{group_id}/custom-fields/person")

    def missing_fields(self, group_id):
        have = {f["name"] for f in self.group_fields(group_id)}
        return [f for f in OWNED_FIELDS if f["name"] not in have]

    def create_field(self, group_id, field):
        return self._call("POST", f"/groups/{group_id}/custom-fields/person", body=field)

    # --- people ------------------------------------------------------------------------------
    def people_in_group(self, group_id):
        return self._pages("/people", {f"filter[groups][in]": group_id})

    def all_people(self):
        return self._pages("/people")

    def update_person(self, person, group_id, values):
        """Set custom field values on `person` for `group_id`, adding the person to that group
        if needed (folk replaces the groups list, so every existing group is resent)."""
        group_ids = [g["id"] for g in person.get("groups", [])]
        if group_id not in group_ids:
            group_ids.append(group_id)
        body = {"groups": [{"id": g} for g in group_ids], "customFieldValues": {group_id: values}}
        return self._call("PATCH", f"/people/{person['id']}", body=body)

    # --- optional photo source: folk calendar/email participants carry a picture ------------
    def participant_picture(self, person):
        """Best-effort: the picture folk attached to this person in a past interaction."""
        try:
            res = self._call("GET", "/interactions", {"entity.id": person["id"], "timeframe": "past"})
        except FolkError as e:
            log.debug("interactions lookup failed for %s: %s", person.get("fullName"), e)
            return None
        emails = {e.lower() for e in person.get("emails", [])}
        for it in res["data"]["items"]:
            parts = [it.get("from")] if isinstance(it.get("from"), dict) else list(it.get("from") or [])
            to = it.get("to")
            parts += [to] if isinstance(to, dict) else list(to or [])
            for p in parts:
                if p and p.get("picture") and str(p.get("value", "")).lower() in emails:
                    return p["picture"]
        return None


def field_values(person, group_id):
    return (person.get("customFieldValues") or {}).get(group_id) or {}
