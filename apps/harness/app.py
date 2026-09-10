"""Fault-injection harness: a failure generator wearing legacy clothes.

Not a legacy simulation for its own sake. Its job is to produce, on demand, the five failure
states no public application will produce for you. The 2004 markup is there so the locator
strategies meet honest resistance while doing it: table layout, a frameset on the detail
screen, an id regenerated on every element on every render, no test ids anywhere, and a
search control that is an <a onclick> rather than a button.

    uv run python -m apps.harness 8099
"""

import secrets
from typing import Annotated, Any

import anyio
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Synthetic throughout. Free-tier prompts leave the machine and are retained, so no real
# person, institution or account number appears here.
MEMBERS: dict[str, dict[str, Any]] = {
    "12345": {
        "name": "Wilhelmina Okonkwo-Bright",
        "status": "Active",
        "joined": "2019-03-14",
        "accounts": [
            {"ref": "SAV-88120", "kind": "Savings", "balance": "4,182.55", "opened": "2019-03-14"},
            {"ref": "CHQ-40771", "kind": "Chequing", "balance": "912.08", "opened": "2019-03-14"},
            {
                "ref": "TRM-10093",
                "kind": "Term deposit",
                "balance": "25,000.00",
                "opened": "2021-08-02",
            },
        ],
    },
    "67890": {
        "name": "Bartholomew Quist",
        "status": "Dormant",
        "joined": "2015-11-02",
        "accounts": [
            {"ref": "SAV-22410", "kind": "Savings", "balance": "77.40", "opened": "2015-11-02"}
        ],
    },
}

# The only difference between tenants. Same routes, same handlers, same templates: a tenant
# is a row in this table, not a fork. Everything a capability overlay must absorb - branding,
# renamed labels, a reordered column, a bumped version - is expressible here and nowhere else.
TENANTS: dict[str, dict[str, str]] = {
    "tenant-a": {
        "brand": "Meridian Core Servicing",
        "member_label": "Member ID",
        "balance_label": "Savings Balance",
        "accounts_label": "Sub-accounts",
        "column_order": "ref-first",
        "version": "4.2.1",
    },
    "tenant-b": {
        "brand": "Northgate Servicing Suite",
        "member_label": "Account Holder ID",
        "balance_label": "Deposit Balance",
        "accounts_label": "Linked accounts",
        "column_order": "kind-first",
        "version": "5.0.3",
    },
}

SLOW_SECONDS = 8.0

app = FastAPI(docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="apps/harness/templates")

Fail = Annotated[str, Query()]


def eid() -> str:
    """A fresh element id on every element on every render. Nothing is locatable by id twice."""
    return f"e{secrets.token_hex(4)}"


templates.env.globals["eid"] = eid


def render(request: Request, template: str, status: int = 200, **context: object) -> HTMLResponse:
    tenant = str(context["tenant"])
    return templates.TemplateResponse(
        request=request,
        name=template,
        status_code=status,
        context={"labels": TENANTS[tenant], **context},
    )


async def injected(request: Request, tenant: str, fail: str) -> HTMLResponse | None:
    """Failure states that pre-empt the screen entirely. None means carry on.

    `not_found` and `modal` are not here: they are states of a real screen rather than a
    replacement for one, so they are handled where that screen is built.
    """
    if fail == "slow":
        await anyio.sleep(SLOW_SECONDS)
        return None
    if fail == "permission":
        return render(request, "denied.html", status=403, tenant=tenant, fail="")
    if fail == "timeout":
        # A real session timeout bounces you somewhere else entirely, mid-flow.
        return RedirectResponse(f"/{tenant}/session-expired", status_code=302)  # type: ignore[return-value]
    return None


def carry(fail: str) -> str:
    """Failure flags ride along the flow, so `modal` can fire on a *later* action."""
    return f"?fail={fail}" if fail else ""


templates.env.globals["carry"] = carry


@app.get("/", response_class=HTMLResponse)
async def root() -> RedirectResponse:
    return RedirectResponse("/tenant-a/", status_code=302)


@app.get("/{tenant}/", response_class=HTMLResponse)
async def search(request: Request, tenant: str, fail: Fail = "") -> HTMLResponse:
    if (early := await injected(request, tenant, fail)) is not None:
        return early
    return render(request, "search.html", tenant=tenant, fail=fail, message="")


@app.get("/{tenant}/session-expired", response_class=HTMLResponse)
async def session_expired(request: Request, tenant: str) -> HTMLResponse:
    return render(request, "expired.html", tenant=tenant, fail="")


@app.get("/{tenant}/members", response_class=HTMLResponse)
async def members(request: Request, tenant: str, q: str = "", fail: Fail = "") -> HTMLResponse:
    if (early := await injected(request, tenant, fail)) is not None:
        return early
    member = None if fail == "not_found" else MEMBERS.get(q.strip())
    if member is None:
        return render(
            request,
            "search.html",
            tenant=tenant,
            fail=fail,
            message=f"No member matches '{q}'.",
        )
    return render(
        request, "results.html", tenant=tenant, fail=fail, member_id=q.strip(), member=member
    )


@app.get("/{tenant}/members/{member_id}", response_class=HTMLResponse)
async def detail(request: Request, tenant: str, member_id: str, fail: Fail = "") -> HTMLResponse:
    """The detail screen is a frameset. Its content lives in two child documents."""
    if (early := await injected(request, tenant, fail)) is not None:
        return early
    if member_id not in MEMBERS:
        return render(
            request, "search.html", tenant=tenant, fail=fail, message="No member matches that id."
        )
    return render(request, "detail_frameset.html", tenant=tenant, fail=fail, member_id=member_id)


@app.get("/{tenant}/members/{member_id}/summary", response_class=HTMLResponse)
async def summary_frame(
    request: Request, tenant: str, member_id: str, fail: Fail = ""
) -> HTMLResponse:
    return render(
        request,
        "detail_summary.html",
        tenant=tenant,
        fail=fail,
        member_id=member_id,
        member=MEMBERS[member_id],
    )


@app.get("/{tenant}/members/{member_id}/accounts", response_class=HTMLResponse)
async def accounts_frame(
    request: Request, tenant: str, member_id: str, fail: Fail = ""
) -> HTMLResponse:
    member = MEMBERS[member_id]
    kind_first = TENANTS[tenant]["column_order"] == "kind-first"
    return render(
        request,
        "detail_accounts.html",
        tenant=tenant,
        fail=fail,
        member_id=member_id,
        accounts=member["accounts"],
        headings=["Type", "Reference"] if kind_first else ["Reference", "Type"],
        kind_first=kind_first,
    )


@app.get("/{tenant}/members/{member_id}/accounts/{ref}", response_class=HTMLResponse)
async def account(
    request: Request, tenant: str, member_id: str, ref: str, fail: Fail = "", ack: str = ""
) -> HTMLResponse:
    if (early := await injected(request, tenant, fail)) is not None:
        return early
    account = next((a for a in MEMBERS[member_id]["accounts"] if a["ref"] == ref), None)
    if account is None:
        return render(
            request, "search.html", tenant=tenant, fail=fail, message="No such sub-account."
        )
    # The unexpected dialog fires on the action *after* the flag was set, not on the flag.
    if fail == "modal" and not ack:
        return render(request, "modal.html", tenant=tenant, fail=fail, member_id=member_id, ref=ref)
    return render(
        request, "account.html", tenant=tenant, fail=fail, member_id=member_id, account=account
    )


@app.get("/{tenant}/members/{member_id}/accounts/{ref}/confirm", response_class=HTMLResponse)
async def confirm(
    request: Request, tenant: str, member_id: str, ref: str, fail: Fail = ""
) -> HTMLResponse:
    if (early := await injected(request, tenant, fail)) is not None:
        return early
    return render(
        request,
        "confirm.html",
        tenant=tenant,
        fail=fail,
        member_id=member_id,
        member=MEMBERS[member_id],
        account=next(a for a in MEMBERS[member_id]["accounts"] if a["ref"] == ref),
    )
