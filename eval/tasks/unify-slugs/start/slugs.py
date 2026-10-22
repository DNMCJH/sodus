"""Slug helpers. The same normalisation, copied three times."""

import re
import unicodedata


def user_slug(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text)
    return text.strip("-").lower()


def team_slug(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", name)
    return text.strip("-").lower()


def project_slug(name: str) -> str:
    text = name.strip().replace(" ", "-")
    return text.lower()
