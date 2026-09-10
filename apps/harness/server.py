"""Fault-injection harness: a deliberately legacy-shaped servicing app, tenant-a and tenant-b.

Not a legacy simulation for its own sake - a failure generator. Every screen is table-laid-out,
every element id is regenerated per render, and there is not a single test id anywhere. The
automation gets exactly the markup a human operator sees.
"""

import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# Synthetic throughout. No real people, no real institutions, no real account numbers:
# free-tier prompts leave the machine and are retained by the provider.
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
            {"ref": "SAV-22410", "kind": "Savings", "balance": "77.40", "opened": "2015-11-02"},
        ],
    },
}

TENANTS: dict[str, dict[str, str]] = {
    "tenant-a": {
        "brand": "Meridian Core Servicing",
        "member_label": "Member id",
        "accounts_label": "Sub-accounts",
        "column_order": "ref-first",
    },
    # Two renamed labels and one reordered column. Same flow, same markup shape.
    "tenant-b": {
        "brand": "Northgate Servicing Suite",
        "member_label": "Membership number",
        "accounts_label": "Linked accounts",
        "column_order": "kind-first",
    },
}

FAIL_MODES = ("not_found", "permission", "timeout", "modal", "slow")


def eid() -> str:
    """A fresh element id on every render. Nothing may be located by it twice."""
    return f"e{secrets.token_hex(4)}"


def page(tenant: str, title: str, body: str) -> str:
    """Table-based chrome, the way an application written in 2004 lays out a screen."""
    brand = TENANTS[tenant]["brand"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title} - {brand}</title></head>
<body>
<table width="100%" cellpadding="4" cellspacing="0" border="0">
<tr><td bgcolor="#003366"><font color="#ffffff" size="4"><b>{brand}</b></font></td></tr>
<tr><td><table width="100%" border="0"><tr>
  <td width="180" valign="top" bgcolor="#eeeeee">
    <table border="0"><tr><td><a href="/{tenant}/" id="{eid()}">Member search</a></td></tr>
    <tr><td><a href="/{tenant}/reports" id="{eid()}">Reports</a></td></tr></table>
  </td>
  <td valign="top"><h1 id="{eid()}">{title}</h1>{body}</td>
</tr></table></td></tr>
</table>
</body></html>"""


def search_screen(tenant: str, message: str = "") -> str:
    label = TENANTS[tenant]["member_label"]
    field, note = eid(), f'<p id="{eid()}">{message}</p>' if message else ""
    return page(
        tenant,
        "Member search",
        f"""{note}
<form method="get" action="/{tenant}/members" id="{eid()}">
<table border="0" cellpadding="3">
<tr><td><label for="{field}" id="{eid()}">{label}</label></td>
    <td><input type="text" name="q" id="{field}" size="20"></td>
    <td><button type="submit" id="{eid()}">Search</button></td></tr>
</table>
</form>""",
    )


def results_screen(tenant: str, query: str) -> str:
    member = MEMBERS.get(query.strip())
    if member is None:
        return search_screen(tenant, f"No member matches {query!r}.")
    rows = (
        f'<tr><td id="{eid()}">{query}</td><td id="{eid()}">{member["name"]}</td>'
        f'<td id="{eid()}">{member["status"]}</td>'
        f'<td><a href="/{tenant}/members/{query}" id="{eid()}">Open</a></td></tr>'
    )
    return page(
        tenant,
        "Search results",
        f"""<table border="1" cellpadding="4" cellspacing="0" id="{eid()}">
<tr bgcolor="#dddddd"><th id="{eid()}">Member</th><th id="{eid()}">Name</th>
    <th id="{eid()}">Status</th><th id="{eid()}">&nbsp;</th></tr>
{rows}
</table>""",
    )


def member_screen(tenant: str, member_id: str, modal: bool = False) -> str:
    member = MEMBERS[member_id]
    kind_first = TENANTS[tenant]["column_order"] == "kind-first"
    heads = ["Type", "Reference"] if kind_first else ["Reference", "Type"]
    rows = []
    for account in member["accounts"]:
        first, second = (
            (account["kind"], account["ref"]) if kind_first else (account["ref"], account["kind"])
        )
        rows.append(
            f'<tr><td id="{eid()}">{first}</td><td id="{eid()}">{second}</td>'
            f'<td id="{eid()}">{account["opened"]}</td>'
            f'<td><a href="/{tenant}/members/{member_id}/accounts/{account["ref"]}" '
            f'id="{eid()}">Open</a></td></tr>'
        )
    interstitial = (
        f"""<div role="dialog" aria-label="Service notice" id="{eid()}">
<table border="1" bgcolor="#ffffcc" cellpadding="6"><tr><td>
<p id="{eid()}">Scheduled maintenance begins at 23:00. Acknowledge to continue.</p>
<a href="/{tenant}/members/{member_id}" id="{eid()}">Acknowledge</a>
</td></tr></table></div>"""
        if modal
        else ""
    )
    return page(
        tenant,
        f"Member {member_id}",
        f"""{interstitial}
<table border="0" cellpadding="3">
<tr><td><b id="{eid()}">Name</b></td><td id="{eid()}">{member["name"]}</td></tr>
<tr><td><b id="{eid()}">Status</b></td><td id="{eid()}">{member["status"]}</td></tr>
<tr><td><b id="{eid()}">Joined</b></td><td id="{eid()}">{member["joined"]}</td></tr>
</table>
<h2 id="{eid()}">{TENANTS[tenant]["accounts_label"]}</h2>
<table border="1" cellpadding="4" cellspacing="0" id="{eid()}">
<tr bgcolor="#dddddd"><th id="{eid()}">{heads[0]}</th><th id="{eid()}">{heads[1]}</th>
    <th id="{eid()}">Opened</th><th id="{eid()}">&nbsp;</th></tr>
{"".join(rows)}
</table>""",
    )


def account_screen(tenant: str, member_id: str, ref: str) -> str | None:
    member = MEMBERS[member_id]
    account = next((a for a in member["accounts"] if a["ref"] == ref), None)
    if account is None:
        return None
    return page(
        tenant,
        f"Account {ref}",
        f"""<table border="0" cellpadding="3">
<tr><td><b id="{eid()}">Account reference</b></td><td id="{eid()}">{account["ref"]}</td></tr>
<tr><td><b id="{eid()}">Account type</b></td><td id="{eid()}">{account["kind"]}</td></tr>
<tr><td><b id="{eid()}">Current balance</b></td><td id="{eid()}">{account["balance"]}</td></tr>
<tr><td><b id="{eid()}">Opened</b></td><td id="{eid()}">{account["opened"]}</td></tr>
</table>
<table border="0"><tr>
<td><a href="/{tenant}/members/{member_id}" id="{eid()}">Back to member</a></td>
<td><button type="button" id="{eid()}">Close account</button></td>
</tr></table>""",
    )


def reports_screen(tenant: str) -> str:
    """The frameset screen. Nothing in the demo path goes through it, by design."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Reports</title></head>
<frameset cols="200,*">
  <frame src="/{tenant}/reports/nav" name="nav">
  <frame src="/{tenant}/reports/body" name="body">
</frameset>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MeridianCore/4.2"

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence the per-request stderr chatter; the run log is the record."""

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        fail = (query.get("fail") or [""])[0]
        parts = [p for p in parsed.path.split("/") if p]

        if not parts:
            self._send("", 302)
            return
        tenant = parts[0]
        if tenant not in TENANTS:
            self._send(page("tenant-a", "Not found", "<p>No such tenant.</p>"), 404)
            return

        if fail == "slow":
            time.sleep(3)
        if fail == "permission":
            self._send(
                page(tenant, "Permission denied", "<p>You may not view this account.</p>"), 403
            )
            return
        if fail == "timeout":
            self._send(
                page(tenant, "Session expired", "<p>Your session expired. Sign in again.</p>"), 440
            )
            return

        rest = parts[1:]
        if not rest:
            self._send(search_screen(tenant))
        elif rest == ["reports"]:
            self._send(reports_screen(tenant))
        elif rest[:1] == ["reports"]:
            self._send(page(tenant, "Reports", "<p>No reports scheduled.</p>"))
        elif rest == ["members"]:
            requested = (query.get("q") or [""])[0]
            self._send(
                search_screen(tenant, f"No member matches {requested!r}.")
                if fail == "not_found"
                else results_screen(tenant, requested)
            )
        elif len(rest) == 2 and rest[0] == "members":
            if rest[1] not in MEMBERS:
                self._send(search_screen(tenant, "No member matches that id."), 404)
                return
            self._send(member_screen(tenant, rest[1], modal=fail == "modal"))
        elif len(rest) == 4 and rest[0] == "members" and rest[2] == "accounts":
            screen = account_screen(tenant, rest[1], rest[3]) if rest[1] in MEMBERS else None
            if screen is None:
                self._send(page(tenant, "Not found", "<p>No such account.</p>"), 404)
            else:
                self._send(screen)
        else:
            self._send(page(tenant, "Not found", "<p>No such screen.</p>"), 404)


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    ThreadingHTTPServer((host, port), Handler).serve_forever()
