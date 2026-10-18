"""Plans, the free trial and what a chat may use right now.

Prices are in Telegram Stars (1 Star is roughly 1.3-2 US cents). They are set here and can be changed with
environment variables, so the owner of the service tunes them without touching the code.
One "check" is one comment that the AI really read: spam answered from memory or by a link list costs nothing.
"""

import os
import time
from dataclasses import dataclass

DAY = 24 * 3600
PERIOD_DAYS = 30  # a month of access, and the window in which checks are counted


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    stars: int  # per 30 days
    checks: int  # AI-read comments per 30 days
    pitch: str
    rub: int = 0  # the same plan in roubles, paid by card or SBP

    @property
    def per_day(self) -> int:
        return self.checks // PERIOD_DAYS


def load_plans() -> dict[str, Plan]:
    start = Plan("start", "Старт", _env_int("PRICE_START", 399), _env_int("CHECKS_START", 20000), "небольшой канал или чат", _env_int("PRICE_START_RUB", 790))
    pro = Plan("pro", "Про", _env_int("PRICE_PRO", 990), _env_int("CHECKS_PRO", 100000), "большой канал, много комментариев", _env_int("PRICE_PRO_RUB", 1990))
    return {p.key: p for p in (start, pro)}


PLANS = load_plans()
TRIAL_DAYS = _env_int("TRIAL_DAYS", 14)
TRIAL_CHECKS = _env_int("CHECKS_TRIAL", 10000)
REFUND_DAYS = _env_int("REFUND_DAYS", 7)  # money back, no questions, within this many days of a payment
# months -> price multiplier: the longer the term, the cheaper a month
TERMS = {1: 1.0, 3: 0.9, 12: 0.75}


def price(plan: Plan, months: int) -> int:
    if months not in TERMS:
        raise ValueError(f"unknown term {months}")
    return round(plan.stars * months * TERMS[months])


def price_rub(plan: Plan, months: int) -> int:
    if months not in TERMS:
        raise ValueError(f"unknown term {months}")
    return round(plan.rub * months * TERMS[months])


@dataclass(frozen=True)
class Access:
    state: str  # "trial" | "paid" | "expired"
    plan: str  # plan key; "trial" while on the trial
    until: int  # unix time when the current state ends
    limit: int
    used: int
    now: int

    @property
    def days_left(self) -> int:
        return max(0, -(-(self.until - self.now) // DAY))  # rounded up: "1 day" until the last second

    @property
    def over_limit(self) -> bool:
        return self.state != "expired" and self.used >= self.limit

    @property
    def ai_allowed(self) -> bool:
        return self.state != "expired" and self.used < self.limit

    @property
    def title(self) -> str:
        if self.state == "trial":
            return "Пробный период"
        plan = PLANS.get(self.plan)
        return plan.title if plan else self.plan


def access(row, used: int, now: float | None = None) -> Access:
    """What the chat row says about access. `used` is the number of AI checks in the last PERIOD_DAYS."""
    now = int(now if now is not None else time.time())
    paid_until = _col(row, "paid_until") or 0
    trial_until = _col(row, "trial_until")
    if paid_until > now:
        key = _col(row, "plan") or "start"
        plan = PLANS.get(key) or PLANS["start"]
        return Access("paid", key, paid_until, plan.checks, used, now)
    if trial_until is None or trial_until > now:  # a row without a date has not started its trial yet
        return Access("trial", "trial", trial_until or now + TRIAL_DAYS * DAY, TRIAL_CHECKS, used, now)
    return Access("expired", _col(row, "plan") or "", max(paid_until, trial_until), 0, used, now)


def _col(row, name: str):
    if row is None:
        return None
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def extend(row, plan_key: str, months: int, now: float | None = None) -> int:
    """New `paid_until` after buying `months` of a plan. Renewing the same plan adds to the time left;
    switching plans starts the new term today, so a cheap long term cannot be turned into a dear plan."""
    now = int(now if now is not None else time.time())
    current = _col(row, "paid_until") or 0
    same = (_col(row, "plan") or "start") == plan_key
    base = current if current > now and same else now
    return base + months * PERIOD_DAYS * DAY
