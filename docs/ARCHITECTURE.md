# Architecture notes

Written for whoever picks this up next — including future me.

## Why one codebase for both deployment modes

The obvious alternative is two products: a SaaS build and a "self-hosted" build.
That fails within a release or two, because the self-hosted build stops getting
fixes and the two schemas drift apart. Then "let this school take their data and
run it themselves" becomes a migration project instead of a copy.

So: one codebase, and a mode switch.

```python
# app/core/config.py
class DeploymentMode(StrEnum):
    SAAS = "saas"
    DEDICATED = "dedicated"
```

Crucially, **a dedicated deployment still has a `tenant_id` on every
document.** There is exactly one value, and it never varies — but the isolation
code has no mode-specific branch to get wrong, and the data shape is byte-for-
byte compatible with the shared cluster. That single decision is what makes
`convert-to-dedicated` a copy.

What the mode actually changes:

| | `saas` | `dedicated` |
|---|---|---|
| `/platform/*` routes | mounted in `app/api.py` | **never registered** |
| `PlanLimits` | from the plan | all `None` |
| `TenantContext.module_enabled()` | checks the plan | always `True` |
| `require()` subscription check | enforced | skipped |
| ImageKit folder | `/tenants/<slug>` | `/dedicated/<slug>` |

## Request lifecycle

```
Request
  ↓  get_tenant()        which institution? (dedicated → the one;
  ↓                       else JWT `tid` → X-Tenant header → subdomain)
  ↓  get_current_auth()  who? loads user + expands their roles' permissions
  ↓  require("x:read")   institution usable? module on? permission held?
  ↓  Repository(...)     bound to tenant_id for the life of the request
  ↓  audit.record()      what changed, by whom, from where
Response
```

The order matters. Resolving the institution *before* the user means a token
issued for one tenant cannot be replayed against another — the repository is
already bound by the time any handler runs.

## Permission model

`"<module>:<action>"`, with wildcards. `"*"` is everything; `"students:*"` is
every action on students. `has_permission` understands both, and `expand()`
flattens wildcards when we need the concrete list (for the UI's permission
grid, and for counting).

Roles live in the `roles` collection, per institution, seeded from
`ROLE_PRESETS` at provisioning. Editing a preset afterwards is the
institution's business, not ours. Two guards exist to stop an institution
locking itself out:

* The owner role's permissions cannot be edited, and it cannot be deleted.
* The last owner account cannot have the role removed or be deactivated.

## The CRUD factory

`core/crud.py` turns a `Resource` into six routes. The parts worth noting:

* **`generated_fields`** — admission numbers and employee ids are produced by a
  `before_create` hook, so they must be *optional on the request body* while
  staying required on the document. Without this the client is asked for a
  value the server is about to generate.
* **`limit_key`** — plan ceilings are checked on create, and are a no-op on a
  dedicated deployment.
* **`scope_hook`** — row-level narrowing layered on top of the tenant filter. A
  parent's `/students` returns only their own children, and no query parameter
  can widen it.
* No `from __future__ import annotations` in that file. Route signatures are
  annotated with schemas built inside the factory, and FastAPI needs those
  resolved eagerly — postponed evaluation leaves it with unresolvable forward
  references.

## Things that bit, and why the code looks the way it does

**`PyObjectId` serialises to `str` only in JSON mode.** With a plain
`PlainSerializer`, `model_dump()` — which is what we hand to Mongo — turned
every foreign key into a string. Documents wrote fine and then matched nothing,
because queries build `ObjectId`s. `when_used="json"` is load-bearing.

**Partial unique indexes use `$eq`, not `$ne`.** Mongo's
`partialFilterExpression` accepts a limited operator set. `{"is_deleted":
{"$eq": False}}` also gives a useful property: a soft-deleted student releases
their admission number for reuse.

**Index creation runs in the background at startup.** Ensuring ~160 indexes
against Atlas takes about 20 seconds. Blocking on it fails Render's health
check on a cold boot, and the app serves correctly (just less efficiently)
while it runs.

**Facility fees follow the facility, not the fee structure.** A school puts
transport in the Class 8 structure because *some* of Class 8 takes the bus.
Billing the walkers is wrong regardless of how the component is flagged, so
`_student_is_billed_for` checks `uses_transport` / `is_hosteller` before it
looks at `is_optional`.

**Receipt and admission numbers come from an atomic counter**
(`next_sequence`), never from a count of existing rows. Two cashiers collecting
at the same moment must not be able to mint the same receipt number.

**Overlays are portalled to `<body>`.** Page content sits inside an entrance
animation, and any CSS transform on an ancestor makes `position: fixed` resolve
against that ancestor instead of the viewport — which collapses a full-height
drawer into the header's box.

**Token refresh is a single shared promise.** A dashboard fires six queries at
once; on an expired token that would start six refreshes, five of which rotate
a token the sixth is still using, and the user gets logged out mid-session.

## Integrations, and how they fail

Every optional provider goes through an adapter with one rule: **a missing
provider is "skipped", not "failed".** A school without an SMS contract must
still be able to publish a notice; a deployment without a payment gateway must
still take fees at the desk. `channel_status()` and the `/status` endpoints
report exactly what is wired up, so "why isn't our SMS going out?" is a screen,
not a support call.

Three places where the money and the hardware forced specific choices:

**Payment amounts are never taken from the client.** An order is created from
the invoice the server looked up. A tampered request body cannot change what is
charged. The browser callback and the webhook both routinely arrive for the same
payment, so capture is idempotent — the second one returns the existing receipt
rather than issuing another.

**Biometric devices speak their own dialect.** ZKTeco firmware talks plain HTTP
to a fixed `/iclock/...` path with plain-text replies, which is why those routes
are mounted at the application root rather than under the API prefix. Two
firmware families exist in the field — one sends the timestamp as a single
tab-separated field, the other as two space-separated tokens — so the parser
locates the timestamp first and reads the status relative to where it ended.
Reading from a fixed index silently mislabels every punch on one of the two.

**A wall clock is not an instant.** A device reports 08:52 local. That is stored
twice: `punched_at` as a true UTC instant (so devices in different timezones
sort together), and `local_time` as a *string*. BSON has no naive datetime —
store one and it comes back tagged UTC, and every client helpfully shifts it
again. Lateness is judged against the local string, because "late after 09:15"
only means anything on the wall clock.

**GPS: a glitch and an outage look identical by speed.** A bus that teleports
1,200 km in two seconds is a bad fix. A bus that covers 9 km while the driver's
app was offline for fifteen minutes really did travel it. Both imply an absurd
speed, so *elapsed time* decides: under a minute, the distance is discarded and
the point kept; over it, the distance is real and only the speed is clamped.

## Deliberate omissions

* **No video hosting.** Online classes are links with a schedule and a join
  window. Schools already run Zoom, Meet or Teams; what they lack is one place
  where the right students get the right link at the right time.
* **No map tiles.** Live tracking draws the recorded path as SVG against its own
  bounding box. A basemap means an API key, a tile budget and a third-party
  script on every page load — and the shape of the route, its stops and the
  current position are what an office actually watches.
* **No background job runner.** `mark-overdue` (fees and library) and
  `biometrics/sync` are endpoints you can call on a schedule rather than a
  worker we have to operate.
* **No bundled font.** The rupee sign is not in ReportLab's built-in Latin-1
  faces, so amounts print as `Rs.` unless a Unicode TTF is found — the module
  checks the font's *character map*, because Arial Unicode MS registers fine and
  predates the symbol by nine years. Drop a font in `backend/fonts/` to get `₹`.
