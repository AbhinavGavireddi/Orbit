import secrets


def authorized(header: str | None, expected: str) -> bool:
    return bool(expected and header and secrets.compare_digest(header, "Bearer " + expected))
