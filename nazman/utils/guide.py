from pathlib import Path
from typing import List, Optional, Tuple

import markdown
from markupsafe import Markup

DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs"

GUIDE_PAGES: List[Tuple[str, str]] = [
    ("overview", "Overview"),
    ("pools", "Set Up a Pool"),
    ("datasets", "Create a Dataset"),
    ("shares", "Share Data (NFS & SMB)"),
    ("snapshots", "Snapshots"),
    ("backup", "Set Up Backups"),
    ("restore", "Restore & Rebuild"),
    ("alerts", "Set Up Telegram Alerts"),
    ("disk-failure", "Handle a Disk Failure"),
    ("recover-dataset", "Recover a Dataset"),
    ("recover-system", "Recover a Complete System"),
    ("troubleshooting", "Troubleshooting"),
]


def guide_page(slug: str) -> Optional[str]:
    for page_slug, _title in GUIDE_PAGES:
        if page_slug == slug:
            return slug
    return None


def guide_title(slug: str) -> Optional[str]:
    for page_slug, title in GUIDE_PAGES:
        if page_slug == slug:
            return title
    return None


def guide_nav(active: str) -> List[dict]:
    return [{"slug": slug, "title": title} for slug, title in GUIDE_PAGES]


def prev_next(slug: str) -> Tuple[Optional[dict], Optional[dict]]:
    for i, (page_slug, title) in enumerate(GUIDE_PAGES):
        if page_slug == slug:
            prev = None
            if i > 0:
                prev = {"slug": GUIDE_PAGES[i - 1][0], "title": GUIDE_PAGES[i - 1][1]}
            nxt = None
            if i < len(GUIDE_PAGES) - 1:
                nxt = {"slug": GUIDE_PAGES[i + 1][0], "title": GUIDE_PAGES[i + 1][1]}
            return prev, nxt
    return None, None


def load_guide_md(slug: str) -> Optional[str]:
    path = DOCS_DIR / f"{slug}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def render_guide_md(source: str) -> Markup:
    body = markdown.markdown(
        source,
        extensions=["fenced_code", "tables", "sane_lists"],
    )
    body = _callout_classes(body)
    return Markup(body)


_CALLOUT_CLASSES = (
    ("Danger:", "callout-danger"),
    ("Warning:", "callout-warning"),
    ("Tip:", "callout-info"),
    ("Info:", "callout-info"),
    ("Recommended:", "callout-success"),
    ("Note:", "callout-info"),
    ("Summary:", "callout-success"),
)


def _callout_classes(body: str) -> str:
    """Tag blockquotes with a CSS class from their leading keyword."""
    for keyword, klass in _CALLOUT_CLASSES:
        body = body.replace(
            f"<blockquote>\n<p><strong>{keyword}</strong>",
            f'<blockquote class="{klass}">\n<p><strong>{keyword}</strong>',
        )
    return body


def guide_page_context(slug: str) -> Optional[dict]:
    active = guide_page(slug)
    if not active:
        return None
    source = load_guide_md(active)
    if source is None:
        return None
    prev, nxt = prev_next(active)
    return {
        "active": active,
        "title": guide_title(active),
        "content": render_guide_md(source),
        "guide_pages": guide_nav(active),
        "prev": prev,
        "next": nxt,
    }