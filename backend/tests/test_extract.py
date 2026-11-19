"""Extraction against real (anonymised) bio pages: the regression net for "sites that change"."""

from conftest import fixture_html

from radar.config import AppConfig
from radar.scraping.extract import extract_profile, visible_text

KIRKLAND_SELECTORS = {
    "title": ".profile-heading__position",
    "groups": ".profile-heading__specialty",
    "offices": ".profile-heading__location-link",
    "education": ".normalized-rte-list--education li",
    "admissions": ".normalized-rte-list--admission li",
}


def test_weil_uses_jsonld_then_headings(app_config: AppConfig) -> None:
    fields = app_config.domain.fields  # the legal profile's extra fields: bar admissions, languages
    record = extract_profile(
        fixture_html("weil.html"), "https://www.weil.com/people/jane-example", None, fields
    )

    assert record.name == "Jane Example"
    assert record.title == "Partner"
    assert record.offices == ["Paris"]
    assert record.email == "jane.example@weil.com"
    assert "Mergers & Acquisitions" in record.groups
    assert record.extra["admissions"] == ["Paris"]
    assert any("1989" in line for line in record.education)
    assert record.methods["title"] == "jsonld"
    assert record.methods["groups"] == "heuristic"


def test_kirkland_uses_configured_selectors(app_config: AppConfig) -> None:
    record = extract_profile(
        fixture_html("kirkland.html"),
        "https://www.kirkland.com/lawyers/s/sample-john",
        KIRKLAND_SELECTORS,
        app_config.domain.fields,
    )

    assert record.name == "John Sample"
    assert record.title == "Associate"
    assert record.offices == ["Chicago"]
    assert record.groups == ["Restructuring"]
    assert record.methods["title"] == "selector"
    assert record.methods["name"] == "meta"


def test_skadden_without_selectors_still_gets_the_sections(app_config: AppConfig) -> None:
    record = extract_profile(
        fixture_html("skadden.html"),
        "https://www.skadden.com/professionals/p/placeholder-alex",
        None,
        app_config.domain.fields,
    )

    assert record.name == "Alex Q. Placeholder"
    assert record.groups == ["White Collar Defense and Investigations", "Middle East"]
    assert record.education[0] == "J.D., UCLA School of Law, 2020"
    assert record.extra["admissions"] == ["California"]
    assert record.title is None  # needs a selector: that is what organisations.yaml provides


def test_content_hash_ignores_extraction_method() -> None:
    html = fixture_html("weil.html")
    a = extract_profile(html, "u")
    b = a.model_copy(update={"methods": {}})
    assert a.content_hash() == b.content_hash()


def test_navigation_and_scripts_are_not_visible_text() -> None:
    text = visible_text("<html><body><nav>Menu</nav><script>x=1</script><p>Bio text</p></body></html>")
    assert text == "Bio text"


def test_heading_heuristics_ignore_site_chrome_before_h1() -> None:
    html = """
    <html><body>
      <div><h3>Education</h3><ul><li>Navigation link</li></ul></div>
      <h1>Pat Doe</h1>
      <section><header><h2>Education</h2></header><ul><li>J.D., Yale Law School, 2001</li></ul></section>
      <section><h2>Practice Areas</h2><ul><li>Mergers &amp; Acquisitions</li><li>Private Equity</li></ul></section>
    </body></html>
    """
    record = extract_profile(html, "u")
    assert record.education == ["J.D., Yale Law School, 2001"]
    assert record.groups == ["Mergers & Acquisitions", "Private Equity"]
