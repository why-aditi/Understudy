# Target surfaces

Three surfaces, each with one job. None of them gives the automation privileged hooks,
injected test ids, or special endpoints.

| Surface | What it is | Job |
|---|---|---|
| **A. Dolibarr** (`apps/dolibarr/`) | Third-party open-source ERP/CRM, pulled from its official image, unmodified | Discovery runs and the primary evidence |
| **B. Harness** (`apps/harness/`) | FastAPI + Jinja2, ~200 lines, ours | Error-path evidence only |
| **C. Harness `tenant-b`** | Same harness, rebranded, two renamed labels, one reordered column | Cross-tenant overlay evidence |

Surface A exists so the robustness claims are not graded on an exam we wrote ourselves.
Surface B exists because no third-party app will produce `session_timeout` on demand.

---

## A. Dolibarr

### Bring it up

```bash
cd apps/dolibarr
docker compose up -d
```

Dolibarr installs itself on first boot (`DOLI_INSTALL_AUTO=1`); give it 30-90 seconds, then
`http://localhost:8081` serves the login page. Credentials are `admin` / `adminpw`, set in
`docker-compose.yml` and deliberately trivial — this instance holds nothing real.

Two named volumes hold state: `dolibarr_db-data` (MariaDB) and `dolibarr_doc-data` (uploads).

### Seed it

```bash
uv run python apps/dolibarr/seed.py
```

The script:

1. **Logs in** through the normal login form.
2. **Enables the Third Parties module** from Dolibarr's own `admin/modules.php` page, by
   following the same activation link an administrator would click. A fresh install has only
   the User module on, so without this there is nothing to search.
3. **Creates 12 synthetic organisations** by filling and submitting the New Third Party form,
   one record per submission.

It is **idempotent**: it reads the existing list first and skips names already on file, so
re-running tops up rather than duplicating. It is also **verified** — after each submission it
checks for an error element and for `socid=` in the resulting URL, because Dolibarr renders the
submitted name as the page title even when the save failed. An earlier version of this script
trusted the title and cheerfully reported creating twelve records that did not exist.

Everything goes in through the application. Nothing writes to Dolibarr's database behind its
back — which matters more than it sounds: `llx_societe` rows have foreign-key children created
on save, so a direct `DELETE` on the parent fails, and a direct `INSERT` would skip them.

### What gets seeded

Twelve organisations with a member reference in `name_alias` (`MB-12345` and friends), a town,
a postcode, a phone number and an `@…test` email. The data is chosen to be awkward on purpose:

- **Near-duplicates** — `Ferreira & Daughters` and `Ferreira and Daughters (Dormant)`, so a
  name search returns two rows and something has to disambiguate them.
- **A casing collision** — `Blackwood Estates LTD` and `blackwood estates ltd`.
- **Diacritics and apostrophes** — `Nordkvist Ågren Trust`, `Ó Braonáin Savings`,
  `O'Sullivan Mutual`, `Adeyemi-Sørensen Partners`.

The customer code column is **not** ours to set: this instance runs `mod_codeclient_monkey`,
which assigns codes itself (`CU2609-00001`…) and rejects anything supplied by the form. The
member reference lives in `name_alias` instead.

### The flow it supports

`search → detail → action`, all in Dolibarr's own screens:

```
/societe/list.php            filter row, search by name/alias/code/zip/phone
  -> /societe/card.php?socid=N   the record card
       -> actions: Send email, Modify, Merge
```

### Reset

```bash
cd apps/dolibarr
docker compose down -v      # -v drops the volumes, so the next up is a fresh install
docker compose up -d
uv run python apps/dolibarr/seed.py
```

---

## B and C. The fault-injection harness

```bash
uv run python -m apps.harness 8099
```

FastAPI + Jinja2. Port 8099 rather than 8080, which collides with a local Apache on at least
one dev machine. No seeding: its two synthetic members live in the source.

### Tenants are config, not a fork

`/tenant-a/` and `/tenant-b/` are the same routes, the same handlers and the same templates.
A tenant is one row in the `TENANTS` dict; there is no tenant-specific module, template or
branch anywhere, and a test fails if a file appears with a tenant in its name.

| | tenant-a | tenant-b |
|---|---|---|
| brand | Meridian Core Servicing | Northgate Servicing Suite |
| member field | **Member ID** | **Account Holder ID** |
| balance field | **Savings Balance** | **Deposit Balance** |
| accounts heading | Sub-accounts | Linked accounts |
| first table column | Reference | Type |
| footer version | 4.2.1 | 5.0.3 |

Same underlying value either way: the balance reads `4,182.55` on both. That is the point —
an overlay has to survive a renamed label, a reordered column and a bumped version without
the capability being re-recorded.

### The flow

```
/{tenant}/                                     member search
  -> /{tenant}/members?q=12345                 results
       -> /{tenant}/members/12345              detail  ** frameset **
            |- /summary                          frame: name, status, joined
            `- /accounts                         frame: the sub-account table
                 -> /accounts/{ref}            sub-account, with the balance
                      -> /accounts/{ref}/confirm   confirmation
```

### Failure flags

Append `?fail=` to any route. The flag rides along the flow in every link it renders, so a
failure armed on one screen can fire several actions later.

| Flag | What the operator sees |
|---|---|
| `not_found` | The search screen again, with "No member matches '12345'" — a result page, not an error |
| `permission` | A denial panel, HTTP 403: "Your role does not permit access to this member record" |
| `timeout` | A 302 **redirect** to `/{tenant}/session-expired`, mid-flow, wherever you were |
| `modal` | An unexpected confirmation dialog on the **next** action, not on the flag itself. Acknowledging continues to the sub-account |
| `slow` | An 8-second delayed render |

### Deliberately hostile markup

Table-based layout, a fresh `id` on every element on every render (verified: zero overlap
across two renders of the same screen), no test ids of any kind, and **the search submit is an
`<a href="#" onclick="…submit()">`, not a button**.

Two things it produces that our own locators do not yet handle, which is the point of having it:

- **The detail screen is a real `<frameset>`.** `Accessibility.getFullAXTree` returns the main
  frame only, so `WebSurface` observes **1 node** there while the browser holds three frames of
  content. Acting inside the frame fails outright. Per-frame traversal is not built yet.
- **"Search" is a substring of "Member search"** in the sidebar, so `role=link name="Search"`
  matches two controls and silently clicks the wrong one. It needs `exact=True`. Cheap, real,
  and exactly the ambiguity a legacy sidebar creates.
