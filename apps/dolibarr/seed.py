"""Seed the unmodified Dolibarr instance with synthetic records.

Everything here goes in through Dolibarr's own web forms: the module is switched on from
its admin page and every record is created by filling and submitting the same New Third
Party form a human operator uses. Nothing patches the application, and nothing writes to
its database behind its back, so the markup the automation later drives is exactly what
Dolibarr produces for its own data.

Idempotent: records that already exist are skipped, so re-running tops up rather than
duplicating.

    uv run python apps/dolibarr/seed.py [--base-url http://localhost:8081]
"""

import argparse
import re
import sys
from dataclasses import dataclass

from playwright.sync_api import Page, sync_playwright

ADMIN_USER = "admin"
ADMIN_PASSWORD = "adminpw"

# Dolibarr's internal module id for Third Parties (modSociete), which also carries Contacts.
THIRD_PARTY_MODULE_ID = 1


@dataclass(frozen=True)
class Member:
    """A synthetic servicing record. No real person, institution or account number."""

    name: str
    code: str
    town: str
    zipcode: str
    phone: str
    email: str


# Deliberately awkward names: diacritics, hyphens, apostrophes, a casing collision and two
# near-duplicates, so locator strategies meet the ambiguity they will meet in production.
MEMBERS: tuple[Member, ...] = (
    Member(
        "Okonkwo-Bright Holdings",
        "MB-12345",
        "Kettering",
        "NN15 6XT",
        "01536 555 010",
        "ops@okonkwo-bright.test",
    ),
    Member(
        "Quist Fiduciary Services",
        "MB-67890",
        "Dunfermline",
        "KY12 7AA",
        "01383 555 021",
        "desk@quist-fiduciary.test",
    ),
    Member(
        "Ferreira & Daughters",
        "MB-24680",
        "Aberystwyth",
        "SY23 1DE",
        "01970 555 032",
        "contact@ferreira-daughters.test",
    ),
    Member(
        "Ferreira and Daughters (Dormant)",
        "MB-24681",
        "Aberystwyth",
        "SY23 1DE",
        "01970 555 033",
        "dormant@ferreira-daughters.test",
    ),
    Member(
        "Nordkvist Ågren Trust",
        "MB-13579",
        "Inverness",
        "IV1 1QY",
        "01463 555 043",
        "trust@nordkvist-agren.test",
    ),
    Member(
        "O'Sullivan Mutual",
        "MB-11223",
        "Waterford",
        "X91 P8H2",
        "051 555 054",
        "members@osullivan-mutual.test",
    ),
    Member(
        "Vasquez-Iyer Credit Union",
        "MB-33445",
        "Leicester",
        "LE1 6RA",
        "0116 555 065",
        "help@vasquez-iyer.test",
    ),
    Member(
        "Blackwood Estates LTD",
        "MB-55667",
        "Perth",
        "PH1 5EX",
        "01738 555 076",
        "estates@blackwood.test",
    ),
    Member(
        "blackwood estates ltd",
        "MB-55668",
        "Perth",
        "PH1 5EX",
        "01738 555 077",
        "second@blackwood.test",
    ),
    Member(
        "Ó Braonáin Savings",
        "MB-77889",
        "Galway",
        "H91 XY24",
        "091 555 087",
        "savings@obraonain.test",
    ),
    Member(
        "Thistlewaite Provident",
        "MB-99001",
        "Harrogate",
        "HG1 2AB",
        "01423 555 098",
        "provident@thistlewaite.test",
    ),
    Member(
        "Adeyemi-Sørensen Partners",
        "MB-10111",
        "Norwich",
        "NR2 1TF",
        "01603 555 109",
        "partners@adeyemi-sorensen.test",
    ),
)


def log(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def login(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    if "index.php" in page.url and page.get_by_role("textbox").count() == 0:
        return
    page.fill("#username", ADMIN_USER)
    page.fill("#password", ADMIN_PASSWORD)
    page.get_by_role("button").first.click()
    page.wait_for_load_state("domcontentloaded")
    if "Login" in (page.title() or ""):
        raise RuntimeError("login failed; check ADMIN_USER / ADMIN_PASSWORD against compose")


def enable_third_parties(page: Page, base_url: str) -> None:
    """Switch the Third Parties module on from Dolibarr's own setup page."""
    page.goto(f"{base_url}/admin/modules.php?mainmenu=home", wait_until="domcontentloaded")
    for link in page.get_by_role("link").all():
        href = link.get_attribute("href") or ""
        match = re.search(r"modules\.php\?id=(\d+)", href)
        if match and int(match.group(1)) == THIRD_PARTY_MODULE_ID and "action=set" in href:
            page.goto(base_url + href, wait_until="domcontentloaded")
            log("enabled Third Parties module")
            return
    log("Third Parties module already enabled")


def existing_names(page: Page, base_url: str) -> set[str]:
    """Names already on file, so a re-run tops up instead of duplicating."""
    page.goto(f"{base_url}/societe/list.php?limit=200", wait_until="domcontentloaded")
    return {
        (link.inner_text() or "").strip()
        for link in page.get_by_role("link").all()
        if "societe/card.php?socid=" in (link.get_attribute("href") or "")
    }


def create_member(page: Page, base_url: str, member: Member) -> None:
    """Fill and submit the New Third Party form, then confirm the record really exists.

    The customer code is left alone: this instance runs mod_codeclient_monkey, which
    assigns codes itself and rejects anything we supply. The member reference goes in
    name_alias instead, which is a first-class searchable field on the card.
    """
    page.goto(f"{base_url}/societe/card.php?action=create", wait_until="domcontentloaded")
    page.fill("#name", member.name)
    page.fill("#name_alias_input", member.code)
    page.fill("#town", member.town)
    page.fill("#zipcode", member.zipcode)
    page.fill("#phone", member.phone)
    page.fill("#email", member.email)
    page.check("input[name=customer][value='1']")
    page.click("input[name=save]")
    page.wait_for_load_state("domcontentloaded")

    # Dolibarr renders the submitted name as the page title even when the save failed,
    # so the title proves nothing. Only landing on a saved card does.
    errors = page.locator("[class*=error]")
    if errors.count():
        raise RuntimeError(f"{member.name}: {errors.first.inner_text().strip()}")
    if "socid=" not in page.url:
        raise RuntimeError(f"{member.name}: form did not save; still at {page.url}")


def seed(base_url: str) -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        try:
            login(page, base_url)
            enable_third_parties(page, base_url)
            already = existing_names(page, base_url)
            created = 0
            for member in MEMBERS:
                if member.name in already:
                    log(f"  skip   {member.name}")
                    continue
                create_member(page, base_url, member)
                created += 1
                log(f"  create {member.name}")
            log(f"seeded {created} new record(s); {len(already)} already present")
            return created
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8081")
    seed(parser.parse_args().base_url)


if __name__ == "__main__":
    main()
