"""Explicit HTTP-boundary failures safe to expose as client errors."""


class InvalidRequest(ValueError):
    """The caller supplied a syntactically valid but unacceptable request."""
