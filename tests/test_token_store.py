from __future__ import annotations

from health_sync.auth.token_store import TokenStore


def test_stored_token_persists_person(app_config, stored_token_factory) -> None:
    store = TokenStore(app_config.tokens_dir("person_a"))
    store.save(stored_token_factory(slug="mount-sinai", person="person_a"))

    loaded = store.load("mount-sinai")

    assert loaded is not None
    assert loaded.person == "person_a"


def test_list_authenticated_is_scoped_to_one_person(
    app_config,
    stored_token_factory,
) -> None:
    TokenStore(app_config.tokens_dir("person_a")).save(
        stored_token_factory(slug="mount-sinai", person="person_a")
    )
    TokenStore(app_config.tokens_dir("person_b")).save(
        stored_token_factory(slug="ucsf", person="person_b")
    )

    assert TokenStore(app_config.tokens_dir("person_a")).list_authenticated() == [
        "mount-sinai"
    ]
    assert TokenStore(app_config.tokens_dir("person_b")).list_authenticated() == ["ucsf"]


def test_rejects_token_copied_to_wrong_person_directory(
    app_config,
    stored_token_factory,
) -> None:
    person_b_token = stored_token_factory(slug="ucsf", person="person_b")
    wrong_path = app_config.tokens_dir("person_a") / "ucsf_token.json"
    wrong_path.write_text(person_b_token.model_dump_json(indent=2))

    try:
        TokenStore(app_config.tokens_dir("person_a")).load("ucsf")
    except RuntimeError as e:
        assert "belongs to 'person_b', not 'person_a'" in str(e)
    else:
        raise AssertionError("Expected copied token to be rejected")
