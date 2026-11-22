"""Shared logical-record identity rule (`[A-Za-z0-9_-]{1,40}`)."""
import re

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,40}")


def identifier(value, name):
    """Validate a logical-record id, rejecting bools, unicode tricks and overlong ids."""
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{name} requires id matching [A-Za-z0-9_-]{{1,40}}")
    return value
