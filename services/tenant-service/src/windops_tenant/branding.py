"""What a tenant may set as branding. Pure, so every rule is easy to test."""

import re
from dataclasses import dataclass
from zoneinfo import available_timezones

PLATFORM_TENANT = "windops-platform"
TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
COLOR = re.compile(r"^#[0-9a-f]{6}$")
LOCALE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
# Ours, so no tenant can claim them.
RESERVED_DOMAINS = ("windops.example", "windops.local", "localhost")
LOGO_MAXIMUM_BYTES = 256 * 1024
LOGO_SIGNATURES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
MINIMUM_CONTRAST = 4.5
REASON_MINIMUM = 10
LIMITS = {"display_name": 100, "login_title": 100, "login_message": 500}
SUBJECT_MAXIMUM, BODY_MAXIMUM = 200, 10_000
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


@dataclass(frozen=True)
class Template:
    placeholders: frozenset[str]
    subject: str
    body: str


TEMPLATES = {
    "invitation": Template(
        frozenset({"tenant_name", "user_name", "login_url"}),
        "Your {{tenant_name}} account",
        "Hello {{user_name}},\n\nAn account was created for you. Sign in at {{login_url}}.",
    ),
    "password_reset": Template(
        frozenset({"tenant_name", "user_name", "reset_url"}),
        "Reset your {{tenant_name}} password",
        "Hello {{user_name}},\n\nReset your password at {{reset_url}}.",
    ),
    "approval_request": Template(
        frozenset({"tenant_name", "user_name", "request_url"}),
        "A {{tenant_name}} request needs your decision",
        "Hello {{user_name}},\n\nA request is waiting for you at {{request_url}}.",
    ),
}


@dataclass(frozen=True)
class BrandingFields:
    display_name: str
    primary_color: str
    accent_color: str
    login_title: str
    login_message: str
    time_zone: str
    locale: str


DEFAULT_PRIMARY, DEFAULT_ACCENT = "#0b5394", "#38761d"


def default_branding(name: str) -> BrandingFields:
    return BrandingFields(
        display_name=name[: LIMITS["display_name"]],
        primary_color=DEFAULT_PRIMARY,
        accent_color=DEFAULT_ACCENT,
        login_title=f"Sign in to {name}"[: LIMITS["login_title"]],
        login_message="",
        time_zone="UTC",
        locale="en-GB",
    )


class BrandingError(ValueError):
    """A value a tenant may not use."""


def _luminance(color: str) -> float:
    """WCAG 2.2 relative luminance."""
    channels = []
    for index in (1, 3, 5):
        value = int(color[index : index + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_with_white(color: str) -> float:
    return 1.05 / (_luminance(color) + 0.05)


def check_tenant_id(tenant_id: str) -> None:
    if not TENANT_ID.match(tenant_id):
        raise BrandingError("A tenant ID is 2 to 63 lowercase letters, digits and hyphens")


def check_branding(fields: BrandingFields) -> BrandingFields:
    """Return the fields cleaned, or raise BrandingError."""
    cleaned = BrandingFields(
        display_name=fields.display_name.strip(),
        primary_color=fields.primary_color.strip().lower(),
        accent_color=fields.accent_color.strip().lower(),
        login_title=fields.login_title.strip(),
        login_message=fields.login_message.strip(),
        time_zone=fields.time_zone.strip(),
        locale=fields.locale.strip(),
    )
    if not cleaned.display_name:
        raise BrandingError("A display name is required")
    for name, limit in LIMITS.items():
        if len(getattr(cleaned, name)) > limit:
            raise BrandingError(f"The {name.replace('_', ' ')} is longer than {limit} characters")
    for color in (cleaned.primary_color, cleaned.accent_color):
        if not COLOR.match(color):
            raise BrandingError("Colors are hex, such as #0b5394")
    # Buttons use white text on the primary color, which must stay readable (WCAG 2.2 AA).
    if contrast_with_white(cleaned.primary_color) < MINIMUM_CONTRAST:
        raise BrandingError("White text on the primary color would be hard to read")
    if cleaned.time_zone not in available_timezones():
        raise BrandingError("Unknown time zone")
    if not LOCALE.match(cleaned.locale):
        raise BrandingError("A locale is a language tag, such as en-GB")
    return cleaned


def check_logo(image: bytes, content_type: str) -> None:
    signature = LOGO_SIGNATURES.get(content_type)
    if signature is None:
        raise BrandingError("A logo is PNG or JPEG")
    if not image.startswith(signature):
        raise BrandingError("The image is not the type it claims")
    if len(image) > LOGO_MAXIMUM_BYTES:
        raise BrandingError(f"A logo is at most {LOGO_MAXIMUM_BYTES // 1024} KiB")


def check_template(template: str, subject: str, body: str) -> None:
    rules = TEMPLATES.get(template)
    if rules is None:
        raise BrandingError("Unknown email template")
    if not subject.strip() or len(subject) > SUBJECT_MAXIMUM or "\n" in subject:
        raise BrandingError(f"A subject is one line, up to {SUBJECT_MAXIMUM} characters")
    if not body.strip() or len(body) > BODY_MAXIMUM:
        raise BrandingError(f"A body is up to {BODY_MAXIMUM} characters")
    used = {name for text in (subject, body) for name in _PLACEHOLDER.findall(text)}
    unknown = used - rules.placeholders
    if unknown:
        raise BrandingError(f"Not allowed in this template: {', '.join(sorted(unknown))}")


def check_domain(domain: str) -> str:
    cleaned = domain.strip().lower().rstrip(".")
    if not DOMAIN.match(cleaned):
        raise BrandingError("Not a valid domain name")
    if any(cleaned == r or cleaned.endswith(f".{r}") for r in RESERVED_DOMAINS):
        raise BrandingError("This domain cannot be used")
    return cleaned


def check_reason(reason: str) -> str:
    cleaned = reason.strip()
    if len(cleaned) < REASON_MINIMUM:
        raise BrandingError(f"A reason of at least {REASON_MINIMUM} characters is required")
    return cleaned[:500]
