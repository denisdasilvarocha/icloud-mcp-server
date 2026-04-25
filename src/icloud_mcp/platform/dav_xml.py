"""Safe DAV XML helpers."""

from __future__ import annotations

from defusedxml import ElementTree


def parse_xml(xml_text: str) -> ElementTree.Element:
    """Parse untrusted DAV XML with defusedxml."""

    return ElementTree.fromstring(xml_text)
