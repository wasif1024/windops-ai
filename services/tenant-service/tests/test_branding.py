"""The branding rules, which need no server or database."""

import pytest
from windops_tenant import branding as rules
from windops_tenant.branding import BrandingError, BrandingFields

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def fields(**changes: str) -> BrandingFields:
    values = {**rules.default_branding("North Sea Wind").__dict__, **changes}
    return BrandingFields(**values)


def test_the_defaults_pass_their_own_rules() -> None:
    assert rules.check_branding(fields()) == fields()


def test_values_are_cleaned() -> None:
    cleaned = rules.check_branding(fields(display_name="  Gale  ", primary_color=" #0B5394 "))
    assert cleaned.display_name == "Gale"
    assert cleaned.primary_color == "#0b5394"


@pytest.mark.parametrize(
    "changes",
    [
        {"display_name": "   "},
        {"display_name": "x" * 101},
        {"login_message": "x" * 501},
        {"primary_color": "blue"},
        {"accent_color": "#12345"},
        # Pale yellow: white text on it is unreadable.
        {"primary_color": "#ffff99"},
        {"time_zone": "Mars/Olympus"},
        {"locale": "english"},
    ],
)
def test_values_that_are_refused(changes: dict[str, str]) -> None:
    with pytest.raises(BrandingError):
        rules.check_branding(fields(**changes))


def test_contrast_follows_wcag() -> None:
    assert round(rules.contrast_with_white("#000000"), 1) == 21.0
    assert round(rules.contrast_with_white("#ffffff"), 1) == 1.0
    assert rules.contrast_with_white(rules.DEFAULT_PRIMARY) >= rules.MINIMUM_CONTRAST


def test_a_png_or_jpeg_logo_is_accepted() -> None:
    rules.check_logo(PNG, "image/png")
    rules.check_logo(JPEG, "image/jpeg")


@pytest.mark.parametrize(
    ("image", "content_type"),
    [
        (SVG, "image/svg+xml"),
        (SVG, "image/png"),
        (JPEG, "image/png"),
        (PNG + b"\x00" * rules.LOGO_MAXIMUM_BYTES, "image/png"),
        (b"", "image/png"),
    ],
)
def test_logos_that_are_refused(image: bytes, content_type: str) -> None:
    """SVG can carry scripts, and the bytes must be what the type claims."""
    with pytest.raises(BrandingError):
        rules.check_logo(image, content_type)


def test_a_template_may_use_its_own_placeholders() -> None:
    rules.check_template(
        "invitation", "Welcome to {{tenant_name}}", "Hi {{ user_name }}, see {{login_url}}"
    )


@pytest.mark.parametrize(
    ("template", "subject", "body"),
    [
        ("newsletter", "Hi", "Body"),
        ("invitation", "Hi", "Reset at {{reset_url}}"),
        ("invitation", "Hi {{secret}}", "Body"),
        ("invitation", "Two\nlines", "Body"),
        ("invitation", "", "Body"),
        ("invitation", "Hi", "x" * 10_001),
    ],
)
def test_templates_that_are_refused(template: str, subject: str, body: str) -> None:
    with pytest.raises(BrandingError):
        rules.check_template(template, subject, body)


def test_every_default_template_passes_its_own_rules() -> None:
    for name, template in rules.TEMPLATES.items():
        rules.check_template(name, template.subject, template.body)


@pytest.mark.parametrize("domain", ["Ops.North-Sea.example.", "wind.example.co"])
def test_a_domain_is_cleaned(domain: str) -> None:
    assert rules.check_domain(domain) == domain.lower().rstrip(".")


@pytest.mark.parametrize(
    "domain", ["localhost", "app.windops.example", "no_underscores.example", "a.b", "-x.example"]
)
def test_domains_that_are_refused(domain: str) -> None:
    with pytest.raises(BrandingError):
        rules.check_domain(domain)


@pytest.mark.parametrize("tenant", ["North-Sea", "a", "-x", "x" * 64, "../etc"])
def test_tenant_ids_that_are_refused(tenant: str) -> None:
    with pytest.raises(BrandingError):
        rules.check_tenant_id(tenant)
