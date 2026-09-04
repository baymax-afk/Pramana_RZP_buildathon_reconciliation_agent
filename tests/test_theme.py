"""
One palette, or a whole other one — never half.

`ui/src/styles.css` declares `color-scheme: light` on `:root` and defines a single set of
tokens. It also used to carry three `@media (prefers-color-scheme: dark)` blocks, left
over from a half-finished attempt at theming a few components.

**Media queries fire regardless of `color-scheme`.** So on any machine set to dark — which
is most laptops — sixteen rules activated on a page whose tokens stayed light:

    .match-head { background: rgb(20, 26, 34); }   with inherited rgb(0, 0, 0) text
    .explanation .plain { color: rgb(231, 236, 243); }   near-white, on a white panel

Measured in a browser at the time: a contrast ratio of about **1.1:1** on the tab the page
now lands on, and invisible text in every transcript. Nobody had looked, because whoever
writes the CSS is not usually the one running dark.

A partial theme is worse than no theme. It does not fall back to the light design; it
half-overrides it. So the rule this file enforces is all-or-nothing: **a dark media block
is permitted only when the palette itself goes dark.**
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "ui" / "src" / "styles.css").read_text(
    encoding="utf-8"
)

# The tokens every component colour is built from. If dark mode is ever done properly,
# these are what must move.
_TOKENS = ("--bg", "--panel", "--ink", "--muted", "--line")


def _dark_blocks() -> list[str]:
    """Every `@media (prefers-color-scheme: dark)` body, brace-matched."""
    blocks = []
    for m in re.finditer(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{", CSS):
        depth, j = 0, m.end() - 1
        while j < len(CSS):
            if CSS[j] == "{":
                depth += 1
            elif CSS[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blocks.append(CSS[m.end() : j])
    return blocks


def test_a_dark_block_is_only_allowed_when_the_palette_goes_dark():
    """
    The whole rule, in one assertion.

    Theming a handful of components while `--panel` stays white is how a match row ends up
    near-black under black text. If someone adds dark support, the tokens move with it.
    """
    blocks = _dark_blocks()
    if not blocks:
        return  # no dark support at all: consistent, and what ships today

    joined = "\n".join(blocks)
    missing = [t for t in _TOKENS if t not in joined]
    assert not missing, (
        f"the stylesheet restyles components for dark mode but never redefines "
        f"{missing} — those components will render dark against a light page, which is "
        f"how `.match-head` came to be rgb(20,26,34) under rgb(0,0,0) text. Move the "
        f"whole palette or none of it."
    )


def test_the_declared_colour_scheme_matches_what_the_stylesheet_actually_does():
    """
    `color-scheme: light` tells the browser to render form controls and scrollbars light.
    Declaring it while shipping dark rules is the same contradiction from the other side.
    """
    declares_light = re.search(r"color-scheme:\s*light", CSS)
    if _dark_blocks():
        assert not declares_light, (
            "the page declares `color-scheme: light` and also ships dark rules; the "
            "browser will render controls light around components styled for dark"
        )
    else:
        assert declares_light, (
            "the stylesheet has one light palette and does not say so. Without "
            "`color-scheme: light` the browser may render form controls and scrollbars "
            "dark around it."
        )


def test_no_component_hardcodes_a_near_black_background():
    """
    A backstop that does not depend on media queries at all.

    Every surface on this page sits on `--panel` (#ffffff) or `--bg` (#fbfbfa) and
    inherits near-black text. A rule painting a surface darker than mid-grey is either a
    theme that was never finished or a mistake, and both read the same way to a user.
    Deliberate dark accents — a badge or a bar segment that sets its own text colour — are
    fine, so only `background` on a bare element selector is checked.
    """
    offenders = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        selector, body = m.group(1).strip(), m.group(2)
        if selector.startswith("@") or "seg" in selector or "outcome" in selector:
            continue
        # A rule that sets its own colour has taken responsibility for the pairing.
        if re.search(r"(^|;)\s*color\s*:", body):
            continue
        for hexval in re.findall(r"background(?:-color)?:\s*#([0-9a-fA-F]{6})\b", body):
            r, g, b = (int(hexval[i : i + 2], 16) for i in (0, 2, 4))
            if (0.299 * r + 0.587 * g + 0.114 * b) < 96:
                offenders.append(f"{selector.splitlines()[-1].strip()} -> #{hexval}")
    assert not offenders, (
        "these rules paint a dark surface but do not set a text colour, so they inherit "
        "the page's near-black ink:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Layout at the sizes this actually gets shown at
# --------------------------------------------------------------------------
def test_the_layout_has_a_narrow_width_rule_for_every_fixed_row():
    """
    Measured in a browser at four widths. A 1024x768 projector and a 1280 laptop were
    clean; a 390px phone overflowed by 107px, in two places I had just made worse:

      * `.match-head` — a flex row of amount, id, two new outcome badges, tier and
        residual, none of which wrapped;
      * `.tabs` — a fifth tab ("How to read this") and count badges pushed five tabs
        past one row.

    Both wrap now rather than hide anything. Every field on a match row answers a
    question somebody might be asking, and a narrow screen is a reason to stack them, not
    to decide for the reader which ones matter. The tab bar wraps rather than scrolls for
    the same reason: a scrolled strip hides tabs with no sign they exist, and "How to read
    this" is the one a confused reader most needs to find.
    """
    narrow = re.findall(
        r"@media\s*\(max-width:\s*(\d+)px\)\s*\{([^@]*?)\n\}", CSS, re.S
    )
    assert narrow, "the stylesheet has no narrow-width rules at all"
    joined = "\n".join(body for _, body in narrow)
    for selector in (".match-head", ".tabs"):
        assert selector in joined, (
            f"{selector} is a fixed-width row with no narrow-width rule; it overflowed a "
            f"390px viewport when this was last measured"
        )
        assert "flex-wrap: wrap" in joined


def test_keyboard_focus_stays_visible():
    """
    Nothing here resets `outline`, so the browser's default ring already survived —
    verified by tabbing to a control and reading back `outline: auto 1px`, rather than
    assumed. This pins the stronger replacement, and pins that it is not removed.
    """
    assert ":focus-visible" in CSS, "no keyboard focus style"
    assert not re.search(r"outline:\s*(none|0)\b", CSS), (
        "something removes the focus outline; keyboard users lose their place on a page "
        "whose every control is a button"
    )
