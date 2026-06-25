"""Safe DAV XML helpers."""

from __future__ import annotations

from dataclasses import dataclass

from defusedxml import ElementTree

DAV_NS = "DAV:"
NS = {"d": DAV_NS}


@dataclass(frozen=True)
class WebDAVSyncChange:
    """Changed WebDAV member returned by sync-collection."""

    href: str
    etag: str | None


@dataclass(frozen=True)
class WebDAVSyncResult:
    """Parsed WebDAV sync-collection result."""

    sync_token: str | None
    changed: list[WebDAVSyncChange]
    deleted: list[str]


def sync_collection_body(sync_token: str | None) -> str:
    """Build a minimal WebDAV sync-collection body."""

    from xml.sax.saxutils import escape

    token = escape(sync_token or "")
    return f"""
    <d:sync-collection xmlns:d="DAV:">
      <d:sync-token>{token}</d:sync-token>
      <d:sync-level>1</d:sync-level>
      <d:prop>
        <d:getetag/>
      </d:prop>
    </d:sync-collection>
    """.strip()


def parse_sync_collection_result(xml_text: str) -> WebDAVSyncResult:
    """Return parsed sync-collection result objects."""

    root = ElementTree.fromstring(xml_text)
    changed: list[WebDAVSyncChange] = []
    deleted: list[str] = []
    for response in root.findall("d:response", NS):
        href = _text(response, "d:href")
        if not href:
            continue
        status = _text(response, "d:status")
        if status and " 404 " in f" {status} ":
            deleted.append(href)
            continue
        changed.append(WebDAVSyncChange(href=href, etag=_text(response, ".//d:getetag")))
    return WebDAVSyncResult(
        sync_token=_text(root, "d:sync-token"),
        changed=changed,
        deleted=deleted,
    )


def _text(element: ElementTree.Element, path: str) -> str | None:
    found = element.find(path, NS)
    if found is None or found.text is None:
        return None
    return found.text.strip()
