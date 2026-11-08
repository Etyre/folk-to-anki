"""AnkiConnect side: make sure the two note types and the decks exist, upload photos,
and add-or-update notes by their Key field. Nothing here ever deletes a note."""
import base64
import json
import logging
import urllib.error
import urllib.request

from cards import BASIC, CLOZE

log = logging.getLogger("anki")

CSS = """.card { font-family: arial; font-size: 20px; text-align: center; color: black; background-color: white; }
img { max-width: 260px; max-height: 320px; }
.back-side.list { display: inline-block; text-align: left; }
.back-side.list ul { padding-left: 1.2em; margin: 0; }
.key { font-size: 10px; color: #bbb; margin-top: 20px; }"""

MODELS = {
    BASIC: {"fields": ["Front", "Back", "Key"], "isCloze": False, "cardTemplates": [
        {"Name": "Card 1", "Front": "{{Front}}",
         "Back": "{{FrontSide}}<hr id=answer>{{Back}}<div class=key>{{Key}}</div>"}]},
    CLOZE: {"fields": ["Text", "Back Extra", "Key"], "isCloze": True, "cardTemplates": [
        {"Name": "Cloze", "Front": "{{cloze:Text}}",
         "Back": "{{cloze:Text}}<br>{{Back Extra}}<div class=key>{{Key}}</div>"}]},
}


class AnkiError(Exception):
    pass


class Anki:
    def __init__(self, url):
        self.url = url

    def call(self, action, **params):
        req = urllib.request.Request(self.url, json.dumps({"action": action, "version": 6, "params": params}).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.load(r)
        except urllib.error.URLError as e:
            raise AnkiError(f"AnkiConnect not reachable at {self.url} (is Anki open?): {e.reason}") from None
        if res.get("error"):
            raise AnkiError(f"{action}: {res['error']}")
        return res["result"]

    def ensure_setup(self, decks):
        existing = set(self.call("modelNames"))
        for name, spec in MODELS.items():
            if name not in existing:
                log.info("creating note type %s", name)
                self.call("createModel", modelName=name, inOrderFields=spec["fields"], css=CSS,
                          isCloze=spec["isCloze"], cardTemplates=spec["cardTemplates"])
        have = set(self.call("deckNames"))
        for d in decks:
            if d not in have:
                log.info("creating deck %s", d)
                self.call("createDeck", deck=d)

    def store_photo(self, filename, data):
        self.call("storeMediaFile", filename=filename, data=base64.b64encode(data).decode())

    def upsert(self, note):
        """-> 'added' | 'updated' | 'unchanged'."""
        key = note["key"].replace('"', '\\"')
        ids = self.call("findNotes", query=f'"Key:{key}"')
        if not ids:
            self.call("addNote", note={"deckName": note["deck"], "modelName": note["model"],
                                       "fields": note["fields"], "tags": note["tags"],
                                       "options": {"allowDuplicate": True}})
            return "added"
        if len(ids) > 1:
            log.warning("key %s matches %d notes; updating the first", note["key"], len(ids))
        info = self.call("notesInfo", notes=ids[:1])[0]
        current = {k: v["value"] for k, v in info["fields"].items()}
        if all(current.get(k) == v for k, v in note["fields"].items()):
            return "unchanged"
        self.call("updateNoteFields", note={"id": ids[0], "fields": note["fields"]})
        return "updated"
