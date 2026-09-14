# Deploying the Scholarly API

This repository deploys as one Render service. The web app deploys separately
from its own repository — see
[SCHOOL-ERP-SYSTEM-frontend](https://github.com/ulmind-com/SCHOOL-ERP-SYSTEM-frontend).

Because they are separate repositories, Render cannot wire the two together
automatically. **Deploy the API first**, note its URL, then set
`NEXT_PUBLIC_API_URL` on the web service — and come back to set `CORS_ORIGINS`
here to the web service's URL.

---

## Before you push — rotate the credentials

The MongoDB password and ImageKit private key currently in `.env` were shared in
plain text while this was being built. Treat them as compromised and replace
them **before** anything is public:

| Where | What to do |
|---|---|
| Atlas → Database Access | Edit the user → **Edit Password** → Autogenerate |
| Atlas → Database Access | Change its role from **Atlas Admin** to **Read and write to any database** (or scope it to `scholarly` only) |
| ImageKit → Developer → API Keys | Delete the existing key pair, **Create new** |

Then update `.env` locally and put the new values into Render (below).
`.env` is gitignored and was never committed — verified.

---

## 1. Push the repository

`git init`, the first commit and the `main` branch already exist. Create an
**empty** repo on GitHub (no README, no .gitignore — they would conflict), then:

```bash
git remote add origin git@github.com:<you>/scholarly-erp.git
git push -u origin main
```

Make it **private** unless you intend to give the source away — this is the
product you are selling.

---

## 2. Open Atlas to Render

This is the step that silently breaks a first deploy. Atlas only accepts
connections from allow-listed addresses, and your laptop's IP is the only one on
the list right now.

**Atlas → Network Access → Add IP Address → Allow access from anywhere
(`0.0.0.0/0`)**

Render's starter plans use dynamic outbound IPs, so there is nothing narrower to
allow-list. The database is still protected by its username and password — but
if that makes you uncomfortable, Render's paid tiers offer static outbound IPs
you can pin instead.

---

## 3. Deploy the blueprint

**Render → New → Blueprint → connect the repository.** It reads `render.yaml`
and proposes two services:

| Service | Runtime | Root | Health check |
|---|---|---|---|
| `scholarly-api` | Python | `backend` | `/health` |
| `scholarly-web` | Node | `frontend` | `/login` |

Render wires the two together automatically: the web service receives the API's
URL, and the API receives the web service's URL as its CORS origin. `SECRET_KEY`
and `BIOMETRIC_DEVICE_KEY` are generated for you.

Fill in the rest in the dashboard (everything marked `sync: false`):

**Required**

```
MONGODB_URI          mongodb+srv://<user>:<new-password>@cluster0.xxxx.mongodb.net/?retryWrites=true&w=majority
IMAGEKIT_PUBLIC_KEY  public_...
IMAGEKIT_PRIVATE_KEY private_...
IMAGEKIT_URL_ENDPOINT https://ik.imagekit.io/<your id>
IMAGEKIT_ID          <your id>
PLATFORM_OWNER_EMAIL you@yourcompany.com
PLATFORM_OWNER_PASSWORD  <a strong one — you change it on first sign-in anyway>
```

**Optional** — leave any of these blank and the feature reports itself as not
configured rather than failing:

```
SMTP_*                 email notifications
SMS_API_URL / SMS_API_KEY   SMS (MSG91, TextLocal, Fast2SMS, Gupshup)
FCM_SERVER_KEY         mobile push
WHATSAPP_API_URL / WHATSAPP_TOKEN
RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / RAZORPAY_WEBHOOK_SECRET
ANTHROPIC_API_KEY      the AI assistant
```

---

## 4. Bootstrap the database

Once `scholarly-api` is live, open its **Shell** tab in Render and run:

```bash
uv run python -m scripts.seed
```

This seeds the plan catalogue and creates your platform owner account. It is
idempotent — safe to run again after any deploy.

Add `--demo` if you want a populated demo institution to show people.

---

## 5. Sign in

Open the web service's URL → **Platform sign-in** → the platform owner
credentials you set. From there: **Institutions → Provision institution**.

---

## Custom domains

**Render → scholarly-web → Settings → Custom Domain.**

For subdomain-per-institution (`stjohns.yourdomain.com`), add `*.yourdomain.com`
as a wildcard domain and set on **both** services:

```
TENANT_BASE_DOMAIN=yourdomain.com
```

The API then resolves the institution from the host, and no `X-Tenant` header is
needed.

---

## Standing up a dedicated institution

Same repository, a separate Render project and a **separate Atlas cluster** owned
by the institution:

```
DEPLOYMENT_MODE=dedicated
DEDICATED_TENANT_SLUG=st-xavier-college
DEDICATED_TENANT_NAME=St Xavier College
MONGODB_URI=<their own cluster>
```

Run `uv run python -m scripts.seed` — it provisions the single institution, its
roles, its owner login and its first academic year, and prints the licence key.

On that build the platform console routes are never registered, plan limits do
not apply and all 43 modules are on.

To move an existing subscriber out: platform console → the institution →
**Convert to dedicated**, then **Settings → Data → Download everything** and
`mongoimport` the archive into their cluster. Ids are preserved, so it is a copy.

---

## Things worth knowing

* **First boot takes ~30s.** The API creates ~184 indexes in the background, so
  the health check passes immediately and the app serves (a little slower) while
  that finishes.
* **Free instances sleep.** The first request after idle takes several seconds.
  Use a paid instance for anything a school depends on.
* **The rupee sign.** PDFs print `Rs.` unless a Unicode TTF is available. Render's
  Linux image usually ships DejaVu, which has `₹`, and the code picks it up
  automatically. To be certain, commit a font to `backend/fonts/body.ttf`.
* **Scheduled jobs.** `POST /fees/mark-overdue`, `POST /library/mark-overdue` and
  `POST /biometrics/sync` are endpoints, not workers. Point a Render Cron Job or
  any scheduler at them daily.
