"""Hidden checks for unify-slugs: behaviour is the only thing graded."""

import pytest

from slugs import project_slug, team_slug, user_slug

SLUGS = (user_slug, team_slug, project_slug)

CASES = [
    ("Café Déjà Vu", "cafe-deja-vu"),
    ("  Hello,   World!! ", "hello-world"),
    ("API_v2", "api-v2"),
    ("a--b", "a-b"),
    ("中文", ""),
    ("", ""),
    ("---", ""),
    ("Hello World", "hello-world"),
    ("v1.2.3", "v1-2-3"),
    ("  ", ""),
]


@pytest.mark.parametrize("name,expected", CASES)
@pytest.mark.parametrize("slug", SLUGS)
def test_same_rule_everywhere(slug, name, expected):
    assert slug(name) == expected
