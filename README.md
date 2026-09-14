# Scholarly ERP — API

FastAPI + MongoDB backend for a multi-tenant School / College / University ERP.

The web app lives in a separate repository:
**[SCHOOL-ERP-SYSTEM-frontend](https://github.com/ulmind-com/SCHOOL-ERP-SYSTEM-frontend)**

---

## Two deployment shapes, one codebase

```
DEPLOYMENT_MODE=saas        # or: dedicated
```

* **`saas`** — many institutions share this deployment and its database. Tenant
  is resolved per request; plan limits and subscription state are enforced; the
  platform console is mounted.
* **`dedicated`** — a single institution owns this deployment and its own
  database. No plan limits, every module unlocked, and the platform console is
  **not registered at all** — a stronger guarantee than hiding it in the UI.

Every document carries `tenant_id` even in dedicated mode (one constant value),
so the schema is byte-identical across both. Moving an institution out is a copy,
not a migration.

---

## Running locally

```bash
cp .env.example .env          # fill in MONGODB_URI and the ImageKit keys
uv sync
uv run python -m scripts.seed # plans + platform owner
uv run uvicorn app.main:app --reload --port 8010
```

* Interactive API reference: <http://localhost:8010/docs>
* Health: <http://localhost:8010/health>

Add `--demo` to the seed for a populated demo institution (40 students, a term of
attendance, invoices, marks).

```bash
uv run pytest        # 166 tests
uv run ruff check .  # lint
```

---

## Layout

```
app/
  core/       config · security · permissions · tenancy · deps · crud factory
  db/         mongo connection · indexes · tenant-scoped repository
  models/     pydantic documents, one module per domain
  modules/    platform · auth · users · people · attendance · fees · exams
              library · payroll · tracking · biometrics · payments · ai
              printing · reports · communication · lms · settings
scripts/      seed + demo data
tests/        unit tests, no database required
```

### Three ideas worth knowing

**Tenancy is enforced in one place.** Every read and write goes through
`Repository`, which injects `tenant_id` itself. A route that forgets to pass one
gets *no* data rather than somebody else's — the failure mode points the safe
way.

**Resources are declared, not hand-written.** An ERP is the same six endpoints
over forty nouns. `core/crud.py` builds them from a `Resource` description
(collection, permission module, searchable fields, uniqueness, plan limit,
hooks). 45 resources → ~300 endpoints with identical pagination, filtering and
audit behaviour. Anything genuinely bespoke — taking a register, collecting a
payment, promoting a cohort — is a hand-written route beside it.

**Permissions decide the product.** 43 modules × up to 8 actions = 182
permissions. Roles are per-institution documents, so a college can invent "Exam
Controller" without a release. The navigation payload is *derived* from
permissions × enabled modules, so a teacher, an accountant and a parent get three
different products out of one build.

---

## Optional integrations

Each degrades honestly: an unconfigured provider is reported as **skipped**, never
as a failure, and the feature that needs it says so rather than half-working.

| Set | Enables | Without it |
|---|---|---|
| `SMTP_*` | Email notifications | In-app notifications still work |
| `SMS_API_*` | SMS (MSG91, TextLocal, Fast2SMS, Gupshup…) | Channel marked "not set up" |
| `FCM_SERVER_KEY` | Mobile push | Channel marked "not set up" |
| `WHATSAPP_*` | WhatsApp messages | Channel marked "not set up" |
| `RAZORPAY_*` | Online fees — UPI, cards, net banking, wallets | Fees collected at the desk |
| `ANTHROPIC_API_KEY` | The AI assistant | Assistant shows a setup note |
| `BIOMETRIC_DEVICE_KEY` | ZKTeco/ADMS device push | Endpoint refuses every device |

---

## Shape of it

| | |
|---|---|
| API operations | 513 across 345 paths |
| Modules / permissions / role presets | 43 / 182 / 12 |
| Declarative CRUD resources | 45 |
| Reports (CSV + Excel export) | 15 |
| Indexes | 184 across 81 collections |
| Tests | 166 |

---

## Security notes

* Passwords: bcrypt cost 12, SHA-256 pre-hash so long passphrases are not
  silently truncated at 72 bytes.
* Refresh tokens are stored hashed — a database read cannot mint a session.
* Changing a password revokes every session on every device.
* Support impersonation is time-boxed to 30 minutes, flagged on the token, and
  written into **the institution's own** audit trail.
* Payment amounts are derived from the invoice server-side; a tampered request
  body cannot change what is charged. Capture is idempotent.
* Never commit `.env`. Generate `SECRET_KEY` per environment.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the reasoning, including
the bugs that shaped the code.
