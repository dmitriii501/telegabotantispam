"""Channel rules written by the admin in plain language.

Jev does not generate text, so the rules are never "compiled" by a text model.
Instead every sentence the admin writes is classified by Jev twice:

* what the sentence *is*: a prohibition, a permission, plain context, or a
  blanket instruction ("удаляй всё");
* what the admin wants *done* about violations: ban, mute, warn, delete, send
  to the admin, or nothing stated (then the bot decides itself).

The classification is shown to the admin for confirmation before it is saved,
and the actions are executed by ordinary code.
"""

import json
import re
from dataclasses import asdict, dataclass

PROHIBITION = "prohibition"
PERMISSION = "permission"
CONTEXT = "context"
EVERYTHING = "everything"  # applies to every comment: "удаляй всё"

ACTIONS = ("ban", "mute", "warn", "delete", "review")


@dataclass
class Rule:
    text: str
    kind: str = PROHIBITION
    action: str | None = None  # None: the bot picks by confidence and author type


# Always on, whatever the admin wrote.
BASE_RULE = Rule("Запрещён спам: реклама заработка, казино, наркотиков, мошенничество, массовые рассылки.")

KIND_CRITERIA = {
    PROHIBITION: "Запрещает что-то конкретное или требует удалять, наказывать, банить за что-то конкретное",
    PERMISSION: "Разрешает что-то, например «мат можно» или «ссылки разрешены»",
    CONTEXT: "Только описывает канал, тему или аудиторию и ничего не запрещает и не разрешает",
    EVERYTHING: "Требует применить действие ко всем комментариям без исключения, например «удаляй всё»",
}

ACTION_CRITERIA = {
    "ban": "Забанить, заблокировать, исключить нарушителя навсегда",
    "mute": "Замутить нарушителя, ограничить ему возможность писать на время",
    "warn": "Удалить комментарий и предупредить или объяснить автору",
    "delete": "Просто удалить комментарий, без предупреждения и без бана",
    "review": "Не удалять самому, а показать админу на проверку",
    "unspecified": "Действие с нарушителем в правиле не названо",
}

# Splits "мат можно, рекламу нельзя — бан" into separate rules, but keeps
# "ссылки нельзя, кроме t.me/my" together.
_SPLIT = re.compile(
    r"[\n;.!]+|,\s*(?=[^,]*\b(?:можно|нельзя|запрещ|разреш|удал|бан|мут|предупре|присыл))",
    re.IGNORECASE,
)


def split_rules(text: str) -> list[str]:
    parts = [p.strip(" -•*\t") for p in _SPLIT.split(text)]
    return [p for p in parts if len(p) >= 3]


def rules_from_text(text: str) -> list[Rule]:
    """Fallback without Jev: every sentence is a prohibition with an automatic action."""
    return [Rule(s) for s in split_rules(text)]


def build_parse_questions(sentences: list[str]) -> dict:
    questions = {}
    for i, sentence in enumerate(sentences):
        questions[f"k{i}"] = {
            "type": "choice",
            "instructions": {"sentence": sentence, "question": "Что делает правило в `sentence`?"},
            "criteria": KIND_CRITERIA,
        }
        questions[f"a{i}"] = {
            "type": "choice",
            "instructions": {
                "sentence": sentence,
                "question": "Какое действие с нарушителем названо в `sentence`?",
            },
            "criteria": ACTION_CRITERIA,
        }
    return questions


def parse_rules_response(sentences: list[str], response: dict) -> list[Rule]:
    answers = response["answers"]
    rules = []
    for i, sentence in enumerate(sentences):
        kind = answers[f"k{i}"]["choice"]
        if kind not in KIND_CRITERIA:
            kind = PROHIBITION
        action = answers[f"a{i}"]["choice"]
        if action not in ACTIONS or kind not in (PROHIBITION, EVERYTHING):
            action = None
        rules.append(Rule(sentence, kind, action))
    return rules


def rules_to_json(rules: list[Rule]) -> str:
    return json.dumps([asdict(r) for r in rules], ensure_ascii=False)


def rules_from_json(data: str) -> list[Rule]:
    return [Rule(**item) for item in json.loads(data)]
