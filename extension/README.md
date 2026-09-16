# seshat capture — browser extension

Two jobs, per spec §10.2:

- **Capture.** Read a page, write the note then and there, with the DOM
  attached as evidence.
- **Repair.** Something failed to fetch — open the page you are already
  authenticated on, click once.

## Why this exists, and what it is not

§6.6's capture deadline comes from the gap between reading a page and fetching
it afterwards, which is where rot and drift live. **Through the extension that
gap is zero**: the browser supplies content at write time, nothing is queued,
`thin` is decidable immediately, and §10.2's recovery window never opens. That
is the main argument for it, larger than the reduction in friction.

What it writes is **a note with evidence attached** (§6.10) — not an unattached
capture awaiting a note. It is a second front door to `note()`, not a new model.

**It writes the capture note and stops.** There is deliberately no "and what do
you think?" box. A capture says what a page contains; an analysis says what
follows from it, and they are separate notes so a later correction can say
which one was wrong. Write the analysis in conversation, with the capture's id
in hand.

The popup asks **"What did this page tell you?"** rather than what the page is.
Same field, different question, and the question is the whole difference —
between a finding and a bookmark.

## Install

1. Run the local interface: `seshat --db ~/notes/seshat.db review`
2. Get the token: `seshat --db ~/notes/seshat.db token`
3. Load the extension: Chrome → Extensions → Developer mode → **Load unpacked**
   → select this directory.
4. Open its options page, paste the token, Save. It will confirm the connection.

Firefox works the same way via `about:debugging` → Load Temporary Add-on.

## The trust boundary

"localhost is safe" stops being true the moment a browser is a client: any page
you visit can try to post to 127.0.0.1. So the endpoints require **both** a
bearer token and a recognised origin — an ordinary web page is refused even
holding the token.

The token lives in `<your-store>.db.token`, mode 0600. It is the only thing
between any page you visit and your note store. `seshat review --no-capture-api`
turns these endpoints off entirely if you only want to browse.

## Provenance

Extension captures record `extraction=browser`, distinct from `fetch` and
`manual`. The DOM after script execution is better fidelity than a fetch **and**
rendered for you specifically — personalisation and A/B bucketing included.
Worth distinguishing if the store is ever evidence.

When a browser capture repairs an earlier fetch, the fetcher's original bytes
stay exactly as captured. Both witnesses survive: what the URL served an
anonymous client, and what it served you.

## Endpoints

| method | path | purpose |
|---|---|---|
| GET | `/api/ping` | version and capability handshake |
| GET | `/api/targets` | what is in the triage queue, so the popup can offer a one-click repair |
| POST | `/api/capture` | `{desc, detail?, url, title?, text, html?}` → writes a note with the page attached |
| POST | `/api/repair` | `{note_id, target, text, html?}` → supplies content for a failed capture |

There is no update or delete endpoint, and there will not be: notes are
immutable (§2, §10.3).
