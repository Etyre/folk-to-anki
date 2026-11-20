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

    def older(self, note):
        """Ids of hand-made notes (any note type but the tool's) that already ask this card's question."""
        for query in note.get("older", []):
            found = self.call("findNotes", query=query + ' -"note:Folk Basic" -"note:Folk Cloze"')
            if found:
                return found
        return []

    WAITING = "folk-waiting-for-face"

    def learned(self, face_queries):
        """Has a names-and-faces card graduated to review? None when there is no such card."""
        face = [i for fq in face_queries for i in self.call("findNotes", query=fq)]
        if not face:
            return None
        cards = self.call("findCards", query="nid:" + ",".join(map(str, face)))
        return any(c["type"] in (2, 3) for c in self.call("cardsInfo", cards=cards))

    STALE = "folk-stale"

    def retire(self, person_id, keys):
        """A member's post cards that the tool no longer generates (the post went to someone else,
        or its title was reworded and got a new key) are suspended and tagged, never deleted;
        one that is generated again comes back. -> how many were newly suspended."""
        ids = [i for kind in ("role", "committee")
               for i in self.call("findNotes", query=f'"Key:{person_id}|{kind}*"')]
        if not ids:
            return 0
        info = self.call("notesInfo", notes=ids)
        gone = [n["noteId"] for n in info if n["fields"]["Key"]["value"] not in keys and self.STALE not in n["tags"]]
        back = [n["noteId"] for n in info if n["fields"]["Key"]["value"] in keys and self.STALE in n["tags"]]
        nid = lambda x: "nid:" + ",".join(map(str, x))
        if gone:
            self.call("suspend", cards=self.call("findCards", query=nid(gone)))
            self.call("addTags", notes=gone, tags=self.STALE)
        if back:
            self.call("unsuspend", cards=self.call("findCards", query=nid(back)))
            self.call("removeTags", notes=back, tags=self.STALE)
        return len(gone)

    def gate(self, face_queries, keys):
        """Face first (Eli, 2026-11-17): a person's other cards stay suspended until their
        names-and-faces card (the tool's or his own) has graduated to a review card, then come
        back. Only never-seen cards are held, and only cards held here are released, so anything
        Eli suspended or already studies is left alone. -> 'held' | 'released' | None."""
        face = [i for fq in face_queries for i in self.call("findNotes", query=fq)]
        if not face or not keys:
            return None                            # no face card at all: nothing to wait for
        nid = lambda ids: "nid:" + ",".join(map(str, ids))
        known = any(c["type"] in (2, 3) for c in
                    self.call("cardsInfo", cards=self.call("findCards", query=nid(face))))
        notes = [i for k in keys for i in self.call("findNotes", query='"Key:%s"' % k.replace('"', '\\"'))]
        if not notes:
            return None
        if known:
            held = self.call("findNotes", query=f"tag:{self.WAITING} {nid(notes)}")
            if not held:
                return None
            self.call("unsuspend", cards=self.call("findCards", query=nid(held)))
            self.call("removeTags", notes=held, tags=self.WAITING)
            return "released"
        new = self.call("findCards", query=f"is:new -is:suspended {nid(notes)}")
        if not new:
            return None
        self.call("suspend", cards=new)
        self.call("addTags", notes=sorted({c["note"] for c in self.call("cardsInfo", cards=new)}), tags=self.WAITING)
        return "held"

    def upsert(self, note):
        """-> 'added' | 'updated' | 'unchanged' | 'skipped' (Eli already has his own card for this;
        his keeps its review history and the tool never adds a second one or touches his)."""
        key = note["key"].replace('"', '\\"')
        ids = self.call("findNotes", query=f'"Key:{key}"')
        if not ids:
            if self.older(note):
                return "skipped"
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
