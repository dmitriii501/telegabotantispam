from core.normalizer import is_obfuscated, normalize


def test_latin_letters_inside_russian_word():
    assert normalize("зaрaбoтaть") == "заработать"


def test_digits_between_letters():
    assert normalize("к0кс") == "кокс"


def test_numbers_are_kept():
    assert normalize("50к в день") == "50к в день"


def test_pure_latin_words_are_kept():
    assert normalize("t.me/mychan iPhone 15") == "t.me/mychan iPhone 15"


def test_invisible_characters_removed():
    assert normalize("ка​зи‍но") == "казино"


def test_spaced_letters_joined():
    assert normalize("к а з и н о тут") == "казино тут"


def test_is_obfuscated():
    assert is_obfuscated("Зaклaдки")
    assert not is_obfuscated("Спасибо за пост")
