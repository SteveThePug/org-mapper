"""Turns a person's profile page into a `ProfileRecord`, without an LLM.

Extraction is a cascade, most explicit first; each field keeps the first value found and
records which method produced it (provenance):

1. `selectors`  - CSS selectors a human configured for this organisation
2. `jsonld`     - schema.org `Person` structured data embedded in the page
3. `heuristic`  - generic "find the heading called Education, read what follows"
4. `meta`       - last resort for the name: the page's <h1>
"""

import json
import re
from collections.abc import Iterator, Sequence
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from radar.config import ExtraField
from radar.models import ProfileRecord

PARSER_VERSION = "extract@2"

LIST_FIELDS = ("offices", "groups", "education")
SCALAR_FIELDS = ("name", "title", "email", "phone", "bio")
BIO_MAX_CHARS = 3000

# Generic heading text (lower-cased, stripped of punctuation) -> field. Matched with startswith.
# The domain profile's extra fields add their own headings (see `extract_profile`).
SECTION_HEADINGS: dict[str, tuple[str, ...]] = {
    "education": ("education",),
    "groups": (
        "practice",
        "areas of practice",
        "areas of focus",
        "focus area",
        "capabilities",
        "services",
        "related capabilities",
        "related practices",
        "position",
    ),
    "offices": ("office", "location"),
    "bio": ("overview", "biography", "profile", "about"),
}

# Selector keys saved before "practices" became the generic "groups".
_LEGACY_FIELD_NAMES = {"practices": "groups"}

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "dt"}
_TEXT_NODE = "-text"  # selectolax's tag name for a bare text node
_NOISE = "script, style, noscript, nav, footer, svg, form, select, template, iframe"


def extract_profile(
    html: str,
    url: str,
    selectors: dict[str, str] | None = None,
    fields: Sequence[ExtraField] = (),
) -> ProfileRecord:
    """`fields` are the domain profile's extra fields (bar admissions, nationality...)."""
    tree = HTMLParser(html)
    jsonld = _from_jsonld(tree, fields)  # before noise removal: JSON-LD lives in <script>
    for node in tree.css(_NOISE):
        node.decompose()

    extra_keys = {f.key for f in fields}
    headings = {**SECTION_HEADINGS, **{f.key: tuple(h.lower() for h in f.headings) for f in fields}}
    sources: list[tuple[str, dict[str, Any]]] = [
        ("selector", _from_selectors(tree, selectors or {}, list_fields={*LIST_FIELDS, *extra_keys})),
        ("jsonld", jsonld),
        ("heuristic", _from_headings(tree, headings)),
        ("meta", _from_meta(tree)),
    ]
    values: dict[str, Any] = {"url": url}
    methods: dict[str, str] = {}
    for method, found in sources:
        for field, value in found.items():
            if field not in methods and value:
                values[field] = value
                methods[field] = method
    if isinstance(values.get("bio"), str):
        values["bio"] = values["bio"][:BIO_MAX_CHARS]
    extra = {key: values.pop(key) for key in list(values) if key in extra_keys}
    return ProfileRecord(**values, extra=extra, methods=methods)


# --- schema.org JSON-LD -------------------------------------------------------------


def _from_jsonld(tree: HTMLParser, fields: Sequence[ExtraField]) -> dict[str, Any]:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(deep=True))
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_jsonld(data):
            if _has_type(item, "Person"):
                found = _person_fields(item)
                for extra in fields:
                    if extra.jsonld:
                        found[extra.key] = _names_of(item.get(extra.jsonld))
                return found
    return {}


def _walk_jsonld(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            yield from _walk_jsonld(item)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _walk_jsonld(data["@graph"])


def _has_type(item: dict[str, Any], wanted: str) -> bool:
    kind = item.get("@type")
    return kind == wanted or (isinstance(kind, list) and wanted in kind)


def _person_fields(person: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _clean(person.get("name")),
        "title": _clean(_first(person.get("jobTitle"))),
        "offices": _names_of(person.get("workLocation")),
        "education": _names_of(person.get("alumniOf")),
        "groups": _names_of(person.get("knowsAbout")),
        "email": _clean(_first(person.get("email")), strip_prefix="mailto:"),
        "phone": _clean(_first(person.get("telephone")), strip_prefix="tel:"),
        "bio": _clean(person.get("description")),
    }


def _names_of(value: Any) -> list[str]:
    names = [_name_of(item) for item in _as_list(value)]
    return [name for name in names if name]


def _name_of(value: Any) -> str | None:
    """A JSON-LD value may be a plain string, a Place (use its town) or any named Thing."""
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        address = value.get("address")
        if isinstance(address, dict) and address.get("addressLocality"):
            return _clean(address["addressLocality"])
        return _clean(value.get("name"))
    return None


# --- configured CSS selectors ---------------------------------------------------------


def _from_selectors(tree: HTMLParser, selectors: dict[str, str], list_fields: set[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for field, selector in selectors.items():
        field = _LEGACY_FIELD_NAMES.get(field, field)
        texts = [_node_text(node) for node in tree.css(selector)]
        texts = [text for text in texts if text]
        if not texts:
            continue
        found[field] = _dedupe(texts) if field in list_fields else texts[0]
    return found


# --- generic heading heuristics -------------------------------------------------------


def _from_headings(tree: HTMLParser, headings: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    seen_h1 = False
    for heading in tree.css(", ".join(sorted(_HEADING_TAGS))):
        if heading.tag == "h1":
            seen_h1 = True
            continue
        if not seen_h1:  # headings before the page title are site navigation
            continue
        field = _field_for_heading(_node_text(heading), headings)
        if field is None or field in found:
            continue
        items = _section_items(heading)
        if not items:
            continue
        if field == "bio":
            found[field] = " ".join(items)
        else:
            found[field] = items
    return found


def _field_for_heading(text: str, headings: dict[str, tuple[str, ...]]) -> str | None:
    key = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not key or len(key) > 40:
        return None
    for field, prefixes in headings.items():
        if any(key.startswith(prefix) for prefix in prefixes):
            return field
    return None


def _section_items(heading: Node) -> list[str]:
    """Text items that follow a heading, up to the next heading.

    Handles both `<h2/><ul/>` and `<header><h2/></header><ul/>` layouts.
    """
    siblings = list(_following_siblings(heading))
    if not siblings and heading.parent is not None:
        siblings = list(_following_siblings(heading.parent))
    items: list[str] = []
    for node in siblings:
        list_items = node.css("li") if node.tag not in ("li", _TEXT_NODE) else []
        if list_items:
            items.extend(_node_text(li) for li in list_items)
        else:
            items.extend(line.strip() for line in node.text(deep=True, separator="\n").split("\n"))
    return _dedupe([item for item in items if item and len(item) < 2000])


def _following_siblings(node: Node) -> Iterator[Node]:
    sibling = node.next
    while sibling is not None:
        if sibling.tag in _HEADING_TAGS or (
            sibling.tag != _TEXT_NODE and sibling.css_first("h1, h2, h3, h4")
        ):
            return
        if sibling.tag != _TEXT_NODE or sibling.text(strip=True):
            yield sibling
        sibling = sibling.next


def _from_meta(tree: HTMLParser) -> dict[str, Any]:
    h1 = tree.css_first("h1")
    return {"name": _node_text(h1)} if h1 is not None else {}


# --- helpers --------------------------------------------------------------------------


def visible_text(html: str) -> str:
    tree = HTMLParser(html)
    for node in tree.css(_NOISE):
        node.decompose()
    body = tree.body
    return " ".join((body.text(deep=True, separator=" ") if body else "").split())


def _node_text(node: Node) -> str:
    return " ".join(node.text(deep=True, separator=" ").split())


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first(value: Any) -> Any:
    items = _as_list(value)
    return items[0] if items else None


def _clean(value: Any, strip_prefix: str = "") -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if strip_prefix and text.lower().startswith(strip_prefix):
        text = text[len(strip_prefix) :]
    return text or None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
