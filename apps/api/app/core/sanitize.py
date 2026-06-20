import bleach

# Zero-tolerance text sanitization — annotations/comments are plain text
# fields, not rich text. Strip ALL HTML tags and attributes.
ALLOWED_TAGS: list[str] = []
ALLOWED_ATTRIBUTES: dict = {}


def sanitize_text(value: str) -> str:
    """
    Strip all HTML/script content from user-supplied text.
    Applied globally to annotation titles, bodies, and comment bodies
    before persistence — never trust client input, even from authenticated
    users, since stored XSS can target other project members.
    """
    if not value:
        return value
    return bleach.clean(value, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, strip=True)