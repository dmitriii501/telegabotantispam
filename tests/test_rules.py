from core.rules import (
    ACTIONS,
    EVERYTHING,
    PERMISSION,
    PROHIBITION,
    Rule,
    build_parse_questions,
    parse_rules_response,
    rules_from_json,
    rules_from_text,
    rules_to_json,
    split_rules,
)


def test_split_by_sentences_and_commas():
    assert split_rules("Канал про крипту, мат можно, рекламу нельзя") == [
        "Канал про крипту",
        "мат можно",
        "рекламу нельзя",
    ]


def test_split_keeps_action_with_its_rule():
    assert split_rules("Рекламу нельзя — бан. Политику — просто удалять") == [
        "Рекламу нельзя — бан",
        "Политику — просто удалять",
    ]


def test_split_by_comma_before_action_words():
    assert split_rules("мат можно, политику — удалять") == ["мат можно", "политику — удалять"]


def test_split_by_lines():
    assert split_rules("- без политики\n- ссылки запрещены") == ["без политики", "ссылки запрещены"]


def test_fallback_rules_are_prohibitions_without_action():
    assert rules_from_text("без политики") == [Rule("без политики", PROHIBITION, None)]


def test_parse_questions_cover_every_sentence_and_action():
    q = build_parse_questions(["a rule", "another"])
    assert set(q) == {"k0", "a0", "k1", "a1"}
    assert set(q["a0"]["criteria"]) == set(ACTIONS) | {"unspecified"}


def answer(choice):
    return {"choice": choice, "confidence": 1.0}


def test_parse_response_maps_kinds_and_actions():
    sentences = ["мат можно", "рекламу нельзя — бан", "удаляй всё", "политику нельзя"]
    response = {
        "answers": {
            "k0": answer("permission"), "a0": answer("unspecified"),
            "k1": answer("prohibition"), "a1": answer("ban"),
            "k2": answer("everything"), "a2": answer("delete"),
            "k3": answer("prohibition"), "a3": answer("unspecified"),
        }
    }
    rules = parse_rules_response(sentences, response)
    assert [(r.kind, r.action) for r in rules] == [
        (PERMISSION, None),
        (PROHIBITION, "ban"),
        (EVERYTHING, "delete"),
        (PROHIBITION, None),
    ]


def test_action_is_dropped_for_permissions():
    response = {"answers": {"k0": answer("permission"), "a0": answer("ban")}}
    assert parse_rules_response(["мат можно"], response)[0].action is None


def test_json_roundtrip():
    rules = [Rule("а", PROHIBITION, "ban"), Rule("б", PERMISSION)]
    assert rules_from_json(rules_to_json(rules)) == rules
