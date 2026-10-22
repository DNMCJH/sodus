from slugs import project_slug, team_slug, user_slug


def test_plain_name_is_the_same_everywhere():
    assert user_slug("Hello World") == "hello-world"
    assert team_slug("Hello World") == "hello-world"
    assert project_slug("Hello World") == "hello-world"
