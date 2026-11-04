"""Shared navigation for the HTML reports in one execution."""

from html import escape

PAGES = (
    ("summary.html", "Summary"),
    ("interactive.html", "Timelines"),
    ("trials.html", "Trials"),
    ("report.html", "Detailed report"),
)


def navigation_html(current: str) -> str:
    """Return a self-contained top menu with relative report links."""
    links = "".join(
        f'<a href="{escape(path)}"'
        + (' aria-current="page"' if path == current else "")
        + f">{escape(label)}</a>"
        for path, label in PAGES
    )
    return (
        "<style>"
        ".pj-report-nav{position:sticky;top:0;z-index:1000;display:flex;gap:.4rem;"
        "flex-wrap:wrap;padding:.65rem 1rem;background:#fff;border-bottom:1px solid #dde4ef;"
        "font:600 .9rem system-ui,sans-serif}"
        ".pj-report-nav a{padding:.4rem .7rem;border-radius:.45rem;color:#2256b2;"
        "text-decoration:none}"
        ".pj-report-nav a:hover,.pj-report-nav a:focus-visible{background:#e8eef7}"
        ".pj-report-nav a[aria-current=page]{background:#dce8fa;color:#17233a}"
        "</style>"
        f'<nav class="pj-report-nav" aria-label="Report pages">{links}</nav>'
    )
