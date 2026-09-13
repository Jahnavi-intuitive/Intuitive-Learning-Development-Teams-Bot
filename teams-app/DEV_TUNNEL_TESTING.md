# Testing the Teams bot locally, before any Render deployment

You said you want to test this against real Teams first, via a dev
tunnel, before worrying about committing/deploying anything. This is the
exact sequence for that. `devtunnel` is already installed on this machine
(confirmed: `devtunnel --version`), so no extra install step is needed.

## 1. Start both local servers

Same as any other local run of this project:

```
# terminal 1 — backend
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# terminal 2 — agent (note the flag — wires in the new conversation-memory service)
cd agent
.venv\Scripts\adk.exe api_server --host 0.0.0.0 --port 8001 --session_service_uri=teamsmemory://local .
```

## 2. Open a tunnel to the backend only

Only the **backend** needs a public URL — Azure Bot Service only ever
calls `POST /api/teams/messages` on the backend; the agent is never
reached from outside this machine.

```
devtunnel host -p 8000 --allow-anonymous
```

This prints a public HTTPS URL like
`https://abcd1234-8000.usw2.devtunnels.ms/`. It changes every time you
start a fresh tunnel (a **temporary** tunnel by default) — if you want a
stable URL across restarts, create a **persistent** one instead:

```
devtunnel create --allow-anonymous
devtunnel port create -p 8000
devtunnel host
```

`--allow-anonymous` is required — without it, every request (including
Azure Bot Service's) needs a Microsoft-account sign-in the tunnel itself
would prompt for, which breaks the bot. This is safe here specifically
because the real security is enforced by the backend itself (Bot
Framework JWT validation + the tenant check in routers/teams.py) — the
tunnel is just a dumb pipe, not a trust boundary.

## 3. Point the Azure Bot Service registration at the tunnel

Once you've created the bot registration (see AZURE_BOT_SETUP.md):

- Bot resource → **Configuration** → **Messaging endpoint** →
  `https://YOUR-TUNNEL-URL/api/teams/messages`

Every time the tunnel URL changes (a fresh non-persistent tunnel, or a
machine restart), update this field again — that's the one manual step
tied to using a temporary tunnel instead of a real deployed URL.

## 4. Fill in the real values in `backend/.env`

Add these to your real `backend/.env` (not committed — already
gitignored) once you have them from the Azure setup:

```
TEAMS_BOT_APP_ID=<the bot's Microsoft App ID>
TEAMS_BOT_APP_PASSWORD=<the client secret you created>
TEAMS_TENANT_ID=<your Entra tenant ID>
```

Restart the backend after adding these — `Settings` only reads `.env` at
process startup.

## 5. Install the app in Teams for yourself

Since this is testing, not the full org-catalog submission yet:

1. In Teams admin center → **Teams apps** → **Manage apps** → **Upload
   new app** — but that package needs real values filled into
   `teams-app/manifest.json` first (see `package_app.ps1`, which refuses
   to build the zip while placeholders remain).
2. Simpler for a first test: Teams client → **Apps** → **Manage your
   apps** → **Upload an app** → **Upload a custom app** (visible if
   custom app uploads are enabled for your tenant/account) — sideloads
   directly to just you, no admin-center step needed, and doesn't
   require the manifest's org-catalog-only fields (privacy/terms URLs
   etc.) to be real yet.

## 6. Send it a message

Message the bot in Teams. What should happen, matching
routers/teams.py's checks in order:

- If your Teams account's `aadObjectId` doesn't match any
  `employees.aad_object_id` row yet (true for everyone right now — that
  column is all `NULL` until the directory sync populates it), you'll get
  "You're not yet recognized in the L&D system..." — **this is expected**,
  not a bug. To actually test the full flow, manually set your own
  `aad_object_id` on one seed employee row first (see below).
- Once recognized, it should behave exactly like the local test UI's
  chat — same agent, same tools, same replies.

### Getting your own `aad_object_id` to test with

Teams doesn't show this to you directly. Easiest way: temporarily add a
print/log of `activity.from_property.aad_object_id` in routers/teams.py's
`messages()` right after `employee = ...`, send one message (you'll get
the "not recognized" reply, but the real ID lands in the backend's
console log), copy it, then:

```sql
UPDATE employees SET aad_object_id = '<the id from the log>' WHERE employee_id = 'E1001';
```

(or ask me to do this for you, pointed at whichever seed employee you
want to test as). Remove the temporary log line afterward.

## 7. Testing conversation continuity end-to-end

The definition-of-done test from the brief: have a conversation, restart
the **agent** process (not the tunnel, not the backend — the agent is
what holds the live session cache), send another message referencing
something only the earlier conversation would know, confirm the reply
shows it remembers. Verified working this way already on 2026-09-06 (see
DEVELOPMENT_LOG.md) — this is the same test, just through Teams instead
of the local test UI's `/api/agent/chat`.
