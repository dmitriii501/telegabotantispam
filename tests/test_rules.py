from core.rules import BASE_RULE, rule_list, split_rules


def test_split_by_sentences_and_commas():
    assert split_rules("Канал про крипту, мат можно, рекламу нельзя") == [
        "Канал про крипту",
        "мат можно",
        "рекламу нельзя",
    ]


def test_split_by_lines():
    assert split_rules("- без политики\n- ссылки запрещены") == ["без политики", "ссылки запрещены"]


def test_base_rule_first():
    assert rule_list("")[0] == BASE_RULE
    assert rule_list("") == [BASE_RULE]
