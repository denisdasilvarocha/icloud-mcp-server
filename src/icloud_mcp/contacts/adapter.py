"""CardDAV adapter for iCloud Contacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import httpx
import vobject
from defusedxml import ElementTree

from icloud_mcp.platform.dav_xml import parse_sync_collection, sync_collection_body

DAV_NS = "DAV:"
CARDDAV_NS = "urn:ietf:params:xml:ns:carddav"
CALSERVER_NS = "http://calendarserver.org/ns/"
NS = {"d": DAV_NS, "card": CARDDAV_NS, "cs": CALSERVER_NS}


@dataclass(frozen=True)
class CardDAVConfig:
    """iCloud CardDAV discovery root."""

    root_url: str = "https://contacts.icloud.com/"
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class SyncedAddressBook:
    """Discovered CardDAV addressbook."""

    id: str
    url: str
    display_name: str
    sync_token: str | None
    ctag: str | None


@dataclass(frozen=True)
class SyncedContact:
    """CardDAV contact normalized for local storage."""

    id: str
    addressbook_id: str
    href: str
    etag: str | None
    uid: str | None
    raw_vcard: str
    display_name: str
    given_name: str | None
    family_name: str | None
    emails: list[str]
    phones: list[str]
    organization: str | None
    notes: str | None
    extra_aliases: list[tuple[str, str, float]] = field(default_factory=list)


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


class CardDAVContactsAdapter:
    """Read-only CardDAV client for iCloud Contacts."""

    def __init__(self, config: CardDAVConfig | None = None) -> None:
        self.config = config or CardDAVConfig()

    def configured(self, apple_id: str | None, app_password: str | None) -> bool:
        """Return whether credentials are available out-of-band."""

        return bool(apple_id and app_password)

    def sync_contacts(self, *, apple_id: str, app_password: str) -> tuple[list[SyncedAddressBook], list[SyncedContact]]:
        """Discover addressbooks and fetch all vCards."""

        auth = (apple_id, app_password)
        with httpx.Client(auth=auth, timeout=self.config.timeout_seconds, follow_redirects=True) as client:
            principal_url = self._principal_url(client)
            home_url = self._addressbook_home_url(client, principal_url)
            addressbooks = self._addressbooks(client, home_url)
            contacts: list[SyncedContact] = []
            for addressbook in addressbooks:
                contacts.extend(self._contacts(client, addressbook))
            return addressbooks, contacts

    def discover_addressbooks(self, *, apple_id: str, app_password: str) -> list[SyncedAddressBook]:
        """Discover CardDAV addressbooks without fetching all contacts."""

        auth = (apple_id, app_password)
        with httpx.Client(auth=auth, timeout=self.config.timeout_seconds, follow_redirects=True) as client:
            principal_url = self._principal_url(client)
            home_url = self._addressbook_home_url(client, principal_url)
            return self._addressbooks(client, home_url)

    def sync_contact_changes(
        self,
        *,
        apple_id: str,
        app_password: str,
        addressbook: SyncedAddressBook,
        sync_token: str,
    ) -> tuple[WebDAVSyncResult, list[SyncedContact]]:
        """Fetch changed/deleted contacts using WebDAV sync-collection."""

        auth = (apple_id, app_password)
        with httpx.Client(auth=auth, timeout=self.config.timeout_seconds, follow_redirects=True) as client:
            result = _sync_collection(client, addressbook.url, sync_token)
            return result, self._contacts_by_hrefs(client, addressbook, [change.href for change in result.changed])

    def _principal_url(self, client: httpx.Client) -> str:
        root = _propfind(
            client,
            self.config.root_url,
            0,
            """
            <d:propfind xmlns:d="DAV:">
              <d:prop><d:current-user-principal/></d:prop>
            </d:propfind>
            """,
        )
        href = root.find(".//d:current-user-principal/d:href", NS)
        if href is None or not href.text:
            raise ValueError("CardDAV principal discovery failed")
        return urljoin(self.config.root_url, href.text)

    def _addressbook_home_url(self, client: httpx.Client, principal_url: str) -> str:
        root = _propfind(
            client,
            principal_url,
            0,
            """
            <d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
              <d:prop><card:addressbook-home-set/></d:prop>
            </d:propfind>
            """,
        )
        href = root.find(".//card:addressbook-home-set/d:href", NS)
        if href is None or not href.text:
            raise ValueError("CardDAV addressbook home discovery failed")
        return urljoin(self.config.root_url, href.text)

    def _addressbooks(self, client: httpx.Client, home_url: str) -> list[SyncedAddressBook]:
        root = _propfind(
            client,
            home_url,
            1,
            """
            <d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav" xmlns:cs="http://calendarserver.org/ns/">
              <d:prop>
                <d:displayname/>
                <d:resourcetype/>
                <d:sync-token/>
                <cs:getctag/>
              </d:prop>
            </d:propfind>
            """,
        )
        books = []
        for response in root.findall("d:response", NS):
            href = _text(response, "d:href")
            if not href:
                continue
            resource_type = response.find(".//d:resourcetype", NS)
            if resource_type is None or resource_type.find("card:addressbook", NS) is None:
                continue
            url = urljoin(self.config.root_url, href)
            display_name = _text(response, ".//d:displayname") or "Contacts"
            books.append(
                SyncedAddressBook(
                    id=f"addr_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}",
                    url=url,
                    display_name=display_name,
                    sync_token=_text(response, ".//d:sync-token"),
                    ctag=_text(response, ".//cs:getctag"),
                )
            )
        return books

    def _contacts(self, client: httpx.Client, addressbook: SyncedAddressBook) -> list[SyncedContact]:
        root = _report(
            client,
            addressbook.url,
            1,
            """
            <card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
              <d:prop>
                <d:getetag/>
                <card:address-data/>
              </d:prop>
            </card:addressbook-query>
            """,
        )
        contacts = []
        for response in root.findall("d:response", NS):
            href = _text(response, "d:href")
            raw_vcard = _text(response, ".//card:address-data")
            if not href or not raw_vcard:
                continue
            contacts.append(
                _contact_from_vcard(
                    addressbook.id, urljoin(addressbook.url, href), raw_vcard, _text(response, ".//d:getetag")
                )
            )
        return contacts

    def _contacts_by_hrefs(
        self,
        client: httpx.Client,
        addressbook: SyncedAddressBook,
        hrefs: list[str],
    ) -> list[SyncedContact]:
        if not hrefs:
            return []
        href_xml = "\n".join(f"<d:href>{escape(href)}</d:href>" for href in hrefs)
        root = _report(
            client,
            addressbook.url,
            1,
            f"""
            <card:addressbook-multiget xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
              <d:prop>
                <d:getetag/>
                <card:address-data/>
              </d:prop>
              {href_xml}
            </card:addressbook-multiget>
            """,
        )
        contacts = []
        for response in root.findall("d:response", NS):
            href = _text(response, "d:href")
            raw_vcard = _text(response, ".//card:address-data")
            if href and raw_vcard:
                contacts.append(
                    _contact_from_vcard(
                        addressbook.id, urljoin(addressbook.url, href), raw_vcard, _text(response, ".//d:getetag")
                    )
                )
        return contacts


def _propfind(client: httpx.Client, url: str, depth: int, body: str) -> ElementTree.Element:
    return _dav_request(client, "PROPFIND", url, depth, body)


def _report(client: httpx.Client, url: str, depth: int, body: str) -> ElementTree.Element:
    return _dav_request(client, "REPORT", url, depth, body)


def _sync_collection(client: httpx.Client, url: str, sync_token: str | None) -> WebDAVSyncResult:
    root = _report(client, url, 1, sync_collection_body(sync_token))
    return _parse_sync_collection_root(root)


def _parse_sync_collection_response(xml_text: str) -> WebDAVSyncResult:
    sync_token, changed, deleted = parse_sync_collection(xml_text)
    return WebDAVSyncResult(
        sync_token=sync_token,
        changed=[WebDAVSyncChange(href=href, etag=etag) for href, etag in changed],
        deleted=deleted,
    )


def _parse_sync_collection_root(root: ElementTree.Element) -> WebDAVSyncResult:
    sync_token, changed, deleted = parse_sync_collection(ElementTree.tostring(root, encoding="unicode"))
    return WebDAVSyncResult(
        sync_token=sync_token,
        changed=[WebDAVSyncChange(href=href, etag=etag) for href, etag in changed],
        deleted=deleted,
    )


def _dav_request(client: httpx.Client, method: str, url: str, depth: int, body: str) -> ElementTree.Element:
    response = client.request(
        method,
        url,
        headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
        content=body.strip().encode("utf-8"),
    )
    response.raise_for_status()
    return ElementTree.fromstring(response.text)


def _contact_from_vcard(addressbook_id: str, href: str, raw_vcard: str, etag: str | None) -> SyncedContact:
    card = vobject.readOne(raw_vcard)
    uid = _first_value(card, "uid")
    display_name = _first_value(card, "fn") or "Unnamed Contact"
    emails = [item.value for item in card.contents.get("email", []) if getattr(item, "value", None)]
    phones = [item.value for item in card.contents.get("tel", []) if getattr(item, "value", None)]
    organization = _organization(card)
    extra_aliases = _extra_aliases(card)
    name = card.contents.get("n", [None])[0]
    given_name = getattr(getattr(name, "value", None), "given", None) if name else None
    family_name = getattr(getattr(name, "value", None), "family", None) if name else None
    return SyncedContact(
        id=f"contact_{hashlib.sha256((addressbook_id + href).encode('utf-8')).hexdigest()[:24]}",
        addressbook_id=addressbook_id,
        href=href,
        etag=etag,
        uid=uid,
        raw_vcard=raw_vcard,
        display_name=display_name,
        given_name=given_name,
        family_name=family_name,
        emails=emails,
        phones=phones,
        organization=organization,
        notes=_first_value(card, "note"),
        extra_aliases=extra_aliases,
    )


def _first_value(card: Any, name: str) -> str | None:
    values = card.contents.get(name, [])
    if not values:
        return None
    value = getattr(values[0], "value", None)
    return str(value) if value else None


def _organization(card: Any) -> str | None:
    org = card.contents.get("org", [None])[0]
    value = getattr(org, "value", None) if org else None
    if isinstance(value, list):
        return " ".join(str(part) for part in value if part)
    return str(value) if value else None


def _extra_aliases(card: Any) -> list[tuple[str, str, float]]:
    aliases: list[tuple[str, str, float]] = []
    for name, alias_type, confidence in [
        ("nickname", "nickname", 0.9),
        ("x-phonetic-first-name", "phonetic", 0.7),
        ("x-phonetic-last-name", "phonetic", 0.7),
        ("related", "relation", 0.65),
    ]:
        for item in card.contents.get(name, []):
            value = getattr(item, "value", None)
            if value:
                aliases.append((str(value), alias_type, confidence))
    return aliases


def _text(element: ElementTree.Element, path: str) -> str | None:
    found = element.find(path, NS)
    if found is None or found.text is None:
        return None
    return found.text.strip()
