# Creating the Azure Bot Service registration

You said you need the steps first — this needs your own Azure subscription
access, so I can't run any of it for you. Follow it in order, then come back
with the two values noted at the bottom.

## 1. Create the bot registration (Azure Portal)

1. Go to https://portal.azure.com → **Create a resource** → search
   **"Azure Bot"** → Create.
2. Fill in:
   - **Bot handle**: a globally-unique name, e.g. `intuitive-lnd-agent`
     (this is just an internal Azure resource name, not what users see in
     Teams — the manifest's display name, asked for separately, is that).
   - **Subscription** / **Resource group**: your existing ones (or a new
     resource group, e.g. `lnd-agent-rg`, if you want this isolated).
   - **Pricing tier**: **F0 (Free)** is enough for this — 10,000
     messages/month, no cost.
   - **Type of App**: choose **Single Tenant** (not Multi Tenant) — this
     restricts the bot to your own tenant at the Azure AD app level, which
     is a stronger version of the tenant check Section 4 of the brief
     asks for at the application layer. When prompted, this will create
     (or let you pick) an **Entra ID app registration** — let Azure create
     a new one for you unless you already have one you want to reuse.
3. Click **Review + create**, then **Create**. Wait for deployment to finish
   (a couple of minutes).

## 2. Set the messaging endpoint

Once created, open the bot resource → **Configuration** (left nav):

- **Messaging endpoint**: `https://9d9xtxqt-8000.asse.devtunnels.ms/api/teams/messages`
  (this is the current dev tunnel exposing your local backend's port 8000
  — verified reachable just now via `/health`. It changes if this tunnel
  is restarted; update this field again whenever that happens. Once this
  is deployed for real, swap in the real public backend URL instead —
  same as `BACKEND_URL` from the Render setup.)

You can revisit and update this field later once the backend route exists
and you know the real deployed URL — it doesn't need to be correct before
you finish the steps below.

## 3. Get the App ID

Still on the bot resource → **Configuration** tab: the **Microsoft App ID**
field is right there — copy it. This is the value I need for
`TEAMS_BOT_APP_ID`.

## 4. Create the client secret

The bot's App ID is backed by an Entra ID app registration. To get a secret:

1. From the bot's **Configuration** tab, click **Manage** next to the
   Microsoft App ID (opens the Entra app registration).
2. Left nav → **Certificates & secrets** → **Client secrets** tab →
   **+ New client secret**.
3. Description: anything (e.g. `lnd-agent-bot-secret`). Expiry: your call —
   24 months is a reasonable default; just note the expiry somewhere so it
   doesn't lapse silently later.
4. Click **Add**, then **immediately copy the secret's Value column** (not
   the Secret ID) — Azure only shows it once.

**Don't paste this secret into our chat.** Put it directly into
`backend/.env` (a file that's already gitignored in this repo) as:

```
TEAMS_BOT_APP_PASSWORD=<the secret value>
```

and separately give me just the **App ID** (not secret) — that one isn't
sensitive the same way and I'll wire it into both `backend/.env` and the
Teams manifest.

## 5. Enable the Teams channel

Bot resource → **Channels** (left nav) → click the **Microsoft Teams**
icon → accept the terms → **Apply**. Without this step Teams won't route
messages to the bot at all, regardless of anything else being correct.

## What to send back

Once you've done the above:
- The **App ID** (paste it directly — not secret)
- Confirmation that you've put the secret into `backend/.env` as
  `TEAMS_BOT_APP_PASSWORD` yourself
- Your **tenant ID** (Entra ID → Overview → Tenant ID — needed for both
  the manifest and the Section 4 tenant-restriction check; separate from
  the App ID)
