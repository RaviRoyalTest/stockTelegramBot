"""Email theme: light palette constants + progressive dark-mode CSS.

Single-responsibility: theme text lives here, transport lives in client.py,
table/section builders live in tables.py. Design contract:

- tables.py's inline styles are a FIXED light palette, chosen for maximum
  readability in casual dark readers via Gmail/Outlook partial auto-adapt.
- For full dark themes (Apple Mail / iOS Mail / newer Outlook web/mobile),
  the document <style> block in client.py carries a
  `@media (prefers-color-scheme: dark)` overlay that light-upgrades cards,
  tables, sections, chips and pills to a dark palette.

Outlook-desktop safety contract (see tests/test_email_html.py):
- Outlook's Word engine parses the whole <style> block statically BEFORE
  dropping it. It turns off its auto dark-tone inverting when the block
  carries `prefers-color-scheme` or `:root` colour declarations - so the
  old inline light design survives there untouched.
- Every dark declaration is `!important`: inline styles cannot express
  media queries, so specificity alone loses to inline styles.
- The overlay re-specifies exactly the declarations tables.py writes
  inline (background-color, color, border-color, border-left-color);
  clients that do not support the media query simply ignore it.
"""
from __future__ import annotations

# --------------------------------------------------------------------
# LIGHT palette - single source of truth.
# tables.py's inline styles and client.py's <style> block must write
# EXACTLY these values; tests/test_email_html.py pins that parity.
# --------------------------------------------------------------------
POS = "#059669"            # gains (emerald 600)
NEG = "#e11d48"            # losses (rose 600)
INK = "#1e293b"            # body text
MUTED = "#64748b"          # secondary text
LINE = "#f1f5f9"           # row separators
ZEBRA = "#f8fafc"          # even-row tint
TH_BG = "#f1f5f9"          # table-header strip
TH_FG = "#64748b"          # table-header text
TH_LINE = "#e2e8f0"        # cell/grid borders
CARD_BG = "#ffffff"        # white card
PAGE_BG = "#edf1f7"        # soft page backdrop
TITLE_FG = "#0f172a"       # headings / bold ink
LABEL_FG = "#475569"       # brand row label
SOFT_LABEL_FG = "#94a3b8"  # brand-row suffix + footer text
ACCENT = "#6366f1"         # accent bar + default section accent
ACCENT_GREEN = "#10b981"
ACCENT_RED = "#f43f5e"
ACCENT_AMBER = "#f59e0b"
PILL_POS_BG = "#dcfce7"
PILL_POS_FG = "#15803d"
PILL_NEG_BG = "#ffe4e6"
PILL_NEG_FG = "#be123c"
CHIP_BG = "#f1f5f9"
CHIP_FG = "#334155"
SECTION_BG = "#f8fafc"

# --------------------------------------------------------------------
# DARK palette - the prefers-color-scheme overlay targets. Picked for
# contrast on a near-black canvas: lighter tint text for gains/losses,
# deep tint pill/chip backgrounds, softer borders, header text one step
# up from body ink.
# --------------------------------------------------------------------
D_POS = "#4ade80"            # gains (emerald 400)
D_NEG = "#fb7185"            # losses (rose 400)
D_INK = "#e2e8f0"            # body text on dark
D_MUTED = "#94a3b8"          # secondary text on dark
D_PAGE_BG = "#0b1120"        # page backdrop (near-black)
D_CARD_BG = "#0f172a"        # card + table surface
D_ZEBRA = "#111a2e"          # even-row tint
D_TH_BG = "#1e293b"          # header strip
D_TH_FG = "#94a3b8"
D_TH_LINE = "#1e293b"        # grid borders
D_TITLE_FG = "#f1f5f9"       # headings
D_LABEL_FG = "#cbd5e1"       # brand row label
D_SOFT_LABEL_FG = "#64748b"  # footer text
D_PILL_POS_BG = "#052e16"    # deep tint keeps the mark readable
D_PILL_POS_FG = "#4ade80"
D_PILL_NEG_BG = "#4c0519"
D_PILL_NEG_FG = "#fb7185"
D_CHIP_BG = "#1e293b"
D_CHIP_FG = "#e2e8f0"
D_SECTION_BG = "#111827"


def _imp(**pairs) -> str:
    """One complete rule: `{prop:value !important; ...}` (braces included).

    Underscores in property names become hyphens (CSS kwargs limit):
    background_color -> background-color, text_align -> text-align."""
    decls = ";".join(
        f"{name.replace('_', '-')}:{value} !important"
        for name, value in pairs.items()
    )
    return "{" + decls + ";}"


def css() -> str:
    """The full <style> block: light polish + prefers-color-scheme dark
    overlay. Every dark declaration is !important because inline styles
    cannot be reached by media-query specificity alone."""
    dark = (
        "@media (prefers-color-scheme:dark){"
        # --- document shell -------------------------------------------
        f"body{_imp(background_color=D_PAGE_BG, color=D_INK)}"
        # --- brand row / card chrome (hooks added in client.py) --------
        f"[data-rs-brand]{_imp(color=D_LABEL_FG)}"
        f"[data-rs-brand] span{_imp(color=D_SOFT_LABEL_FG)}"
        f"[data-rs-card]{_imp(background_color=D_CARD_BG, border_color=D_TH_LINE)}"
        f"[data-rs-title]{_imp(color=D_TITLE_FG)}"
        f"[data-rs-subtitle]{_imp(color=D_MUTED)}"
        f"[data-rs-footer]{_imp(color=D_SOFT_LABEL_FG)}"
        # --- tokens embedded by tables.py ------------------------------
        f".rs-sec{_imp(background_color=D_SECTION_BG, color=D_TITLE_FG)}"
        f".rs-table{_imp(background_color=D_CARD_BG, border_color=D_TH_LINE)}"
        f".rs-table th{_imp(background_color=D_TH_BG, color=D_TH_FG, border_color=D_TH_LINE)}"
        f".rs-table td{_imp(background_color=D_CARD_BG, color=D_INK, border_color=D_TH_LINE)}"
        # Zebra: even rows carry an inline background-color; sweep tinted
        # cells back to the surface (attribute selectors, no regex needed).
        ".rs-table td[style*='background-color']"
        f"{_imp(background_color=D_ZEBRA)}"
        f".rs-table td[style*='background-color'].num"
        f"{_imp(text_align='right')}"
        # --- spans / pills / chips --------------------------------------
        f".pos{_imp(color=D_POS)}"
        f".neg{_imp(color=D_NEG)}"
        f".muted{_imp(color=D_MUTED)}"
        f".pill{_imp(background_color=D_CHIP_BG, color=D_MUTED)}"
        f".pill.pos{_imp(background_color=D_PILL_POS_BG, color=D_PILL_POS_FG)}"
        f".pill.neg{_imp(background_color=D_PILL_NEG_BG, color=D_PILL_NEG_FG)}"
        f".chip{_imp(background_color=D_CHIP_BG, color=D_CHIP_FG, border_color=D_TH_LINE)}"
        f".chip b{_imp(color=D_TITLE_FG)}"
        f".chip span{_imp(color=D_MUTED)}"
        "}"
    )
    light = (
        # Outlook static-guard signature: seeing prefers-color-scheme /
        # :root colour declarations in the stylesheet is what makes its
        # engine skip auto dark-tone inverting of the inline design.
        ":root{color-scheme:light dark;supported-color-schemes:light dark;}"
        ".rs-sec{margin:24px 0 10px;padding:10px 14px;"
        "background-color:" + SECTION_BG + ";"
        "border-left:4px solid " + ACCENT + ";"
        "border-radius:0 10px 10px 0;"
        "font-size:15px;font-weight:800;color:" + TITLE_FG + ";}"
        ".rs-sec.green{border-left-color:" + ACCENT_GREEN + ";}"
        ".rs-sec.red{border-left-color:" + ACCENT_RED + ";}"
        ".rs-sec.amber{border-left-color:" + ACCENT_AMBER + ";}"
        ".rs-sec.slate{border-left-color:" + ACCENT + ";}"
        ".rs-table{width:100%;border-collapse:collapse;margin:10px 0 14px;"
        "font-size:13px;background-color:" + CARD_BG + ";"
        "border:1px solid " + TH_LINE + ";}"
        ".rs-table th{background-color:" + TH_BG + ";color:" + TH_FG + ";"
        "padding:9px 10px;text-align:left;font-size:11px;font-weight:800;"
        "letter-spacing:0.6px;white-space:nowrap;"
        "border:1px solid " + TH_LINE + ";"
        "text-transform:uppercase;}"
        ".rs-table th.num,.rs-table td.num{text-align:right;white-space:nowrap;"
        "font-variant-numeric:tabular-nums;}"
        ".rs-table td{padding:9px 10px;border:1px solid " + TH_LINE + ";"
        "color:" + INK + ";}"
        ".pos{color:" + POS + ";font-weight:700;}"
        ".neg{color:" + NEG + ";font-weight:700;}"
        ".pill{display:inline-block;padding:3px 10px;border-radius:999px;"
        "font-size:12px;font-weight:700;background-color:" + CHIP_BG + ";"
        "color:" + MUTED + ";}"
        ".pill.pos{background-color:" + PILL_POS_BG + ";color:" + PILL_POS_FG + ";}"
        ".pill.neg{background-color:" + PILL_NEG_BG + ";color:" + PILL_NEG_FG + ";}"
        ".chip{display:inline-block;padding:6px 12px;margin:0 6px 6px 0;"
        "border-radius:10px;font-size:13px;font-weight:700;"
        "background-color:" + CHIP_BG + ";color:" + CHIP_FG + ";"
        "border:1px solid " + TH_LINE + ";}"
        ".chip b{color:" + TITLE_FG + ";}"
        ".muted{color:" + MUTED + ";font-size:12px;}"
        "@media only screen and (max-width:480px){"
        ".rs-table th,.rs-table td{padding:7px 6px;font-size:12px;}}"
    )
    return light + dark


def green(value: object) -> str:
    """Inline color for a positive move (light palette - fixed inline)."""
    return POS


def red(value: object) -> str:
    """Inline color for a negative move (light palette - fixed inline)."""
    return NEG


__all__ = ["css", "green", "red"]
