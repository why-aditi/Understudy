"""Resolve our own ControlDescriptor candidates against a native Windows application.

A proof, not an implementation: there is no DesktopSurface and this builds none. It dumps the
UI Automation tree of Calculator, maps UIA control types onto the same role vocabulary the web
surface produces, and calls `cua.replay.resolver.matches` - the identical function replay uses,
unchanged - against it. If the artifact format is genuinely surface-agnostic that function
should not need to know which operating system produced the tree, and the only strategy that
should fail is `dom_hint`, flagged `surface_specific` precisely because it means nothing here.

    uv run python scripts/desktop_ax_proof.py
"""

import subprocess
import sys
import time
from pathlib import Path

import uiautomation as auto

from cua.replay.resolver import matches
from cua.schema.models import Locator
from cua.surfaces.base import AXNode

OUTPUT = Path("evidence/desktop-ax-proof.txt")

# The documented ARIA<->UIA correspondence read backwards, lossy in the usual place: UIA has
# one Group type where ARIA distinguishes group from region.
ROLES = {
    "ButtonControl": "button",
    "TextControl": "StaticText",
    "GroupControl": "region",
    "EditControl": "textbox",
    "WindowControl": "window",
    "MenuBarControl": "menubar",
    "MenuItemControl": "menuitem",
    "ImageControl": "img",
    "ListItemControl": "listitem",
    "DataItemControl": "row",
}


def locator(strategy: str, **params: object) -> Locator:
    return Locator(
        strategy=strategy,  # type: ignore[arg-type]
        params=params,
        stability_score=0.8,
        verified_unique_at_record=True,
    )


def region(anchor: str, index: int) -> Locator:
    return locator(
        "anchor_relative",
        anchor_text=anchor,
        relation="within_region",
        target_role="button",
        index=index,
    )


def following(anchor: str) -> Locator:
    return locator(
        "anchor_relative",
        anchor_text=anchor,
        relation="following",
        target_role="button",
        index=0,
    )


# Written exactly as the synthesizer emits them for a web surface. Nothing is desktop-specific.
CANDIDATES = {
    "role_name": locator("role_name", role="button", name="Seven", match="exact"),
    "anchor_relative / within_region": region("Number pad", 7),
    "anchor_relative / following": following("Four"),
    "text_content": locator("text_content", text="Equals", match="exact"),
    "dom_hint": locator("dom_hint", css="#seven"),
}

# The same two relations with different arguments: if the index and the anchor genuinely drive
# the resolution rather than coincidentally landing on the right control, these must come back
# with different, predictable answers.
CONTROLS = {
    "within_region index=3": region("Number pad", 3),
    "following 'Seven'": following("Seven"),
    "following 'Memory recall'": following("Memory recall"),
}


def to_ax(node: auto.Control, depth: int = 0) -> AXNode:
    """One UIA element into the same AXNode the web surface builds from CDP."""
    return AXNode(
        role=ROLES.get(node.ControlTypeName, "generic"),
        name=(node.Name or None),
        children=[to_ax(child, depth + 1) for child in node.GetChildren()] if depth < 12 else [],
    )


def find_calculator(timeout: float = 25.0) -> auto.Control:
    """Calculator is not findable by Name at searchDepth=1 and its UWP host exits quickly."""
    subprocess.Popen(["cmd", "/c", "start", "", "calc.exe"], shell=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for child in auto.GetRootControl().GetChildren():
            if "Calculator" in (child.Name or ""):
                return child
        time.sleep(1)
    raise SystemExit("could not find a Calculator window")


def outline(node: AXNode, depth: int = 0) -> list[str]:
    """The mapped tree, trimmed to the roles a reader needs to check the resolutions."""
    here = (
        [f"  {'  ' * depth}- {node.role} {node.name!r}"]
        if node.role in ("button", "region") and node.name
        else []
    )
    return here + [line for child in node.children for line in outline(child, depth + 1)]


def main() -> None:
    window = find_calculator()
    tree = to_ax(window)
    resolved = {label: matches(tree, c) for label, c in CANDIDATES.items()}
    portable = [label for label, c in CANDIDATES.items() if not c.surface_specific]

    report = [
        "Desktop accessibility proof",
        "=" * 78,
        "",
        f"application : {window.Name!r} ({window.ControlTypeName})",
        f"platform    : Windows UI Automation via uiautomation, {sys.platform}",
        "resolver    : cua.replay.resolver.matches, unmodified - the same function replay uses",
        "",
        "UIA tree, mapped onto the role vocabulary the web surface emits:",
        "",
        *outline(tree)[:22],
        "",
        "Resolving recorded candidates against that tree:",
        "",
        *(
            f"  {label:<32} SKIPPED (surface_specific)  cannot be expressed on a non-web surface"
            if CANDIDATES[label].surface_specific
            else f"  {label:<32} {'resolved' if len(found) == 1 else f'{len(found)} matches':<27}"
            f" {', '.join(repr(n.name) for n in found[:3]) or '-'}"
            for label, found in resolved.items()
        ),
        "",
        "The relations are structural, not coincidence - vary the argument:",
        "",
        *(
            f"  {label:<32} -> {', '.join(repr(n.name) for n in matches(tree, c)[:2]) or '-'}"
            for label, c in CONTROLS.items()
        ),
        "",
        "-" * 78,
        f"{sum(len(resolved[label]) == 1 for label in portable)} of {len(portable)} portable "
        "candidates resolved against a native desktop tree,",
        "with no change to the resolver and no DesktopSurface in existence.",
        "",
        "dom_hint was skipped, which is the point: it is the one strategy the schema flags",
        "surface_specific, and the one that cannot survive leaving the browser.",
    ]

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(report) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    print("\n".join(report))
    print(f"\nwritten to {OUTPUT}")


if __name__ == "__main__":
    main()
