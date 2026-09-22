"""Jev judgments over the assembled state.

One request carries every question; they run in parallel and cannot see each other.
Code (policy.py) composes the answers. Questions are speculative where useful:
`entry_action` is always asked (even with a position open) because an opposite-side
entry signal is what tells code the position should be flipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from typesafe_sdk import Choice, Noul, Score, SystemOneResponse, TypeSafeClient

from .config import Settings

SETUP_LEVELS = [
    "no edge: the timeframes in `timeframes` disagree, or most of them read sideways / range-bound",
    "weak: only a few timeframes lean one way; momentum indicators (rsi, macd, stochastic) do not confirm",
    "decent: most timeframes agree on the direction and at least one momentum indicator confirms it",
    "strong: nearly all timeframes agree, momentum and volume confirm, and volatility is normal or high rather than squeezed",
]


def build_questions(has_position: bool, side: str | None = None, has_perspective: bool = False) -> dict:
    entry_guidance = [
        "Give the higher timeframes (`timeframe_groups.higher`) more weight for direction "
        "and the lower timeframes (`timeframe_groups.lower`) more weight for timing.",
        "A long needs bullish trend readings on the higher timeframes and momentum turning up on the lower ones.",
        "A short needs bearish trend readings on the higher timeframes and momentum turning down on the lower ones.",
        "Choose hold when timeframes conflict, when most read sideways / range-bound, "
        "or when momentum is already overbought (for long) or oversold (for short).",
    ]
    if has_perspective:
        entry_guidance.append(
            "`perspective` is today's macro and news view. Lean toward hold when `perspective.trading_stance` "
            "is 'stay flat' or `perspective.event_risk_level` is 'high', and prefer the side that "
            "`perspective.macro_bias` and `perspective.trading_stance` favor when the indicators also support it."
        )
    q: dict = {
        "entry_action": Choice(
            instructions={
                "premise": "Assume there is NO open position right now.",
                "question": (
                    "Using the indicator summaries in `timeframes` (and `market` and `perspective` if present), "
                    "which entry action fits best at this moment?"
                ),
                "guidance": entry_guidance,
            },
            criteria={
                "long": "open a new long (buy) position now: bullish readings dominate across timeframes and timing is favorable",
                "short": "open a new short (sell) position now: bearish readings dominate across timeframes and timing is favorable",
                "hold": "do not open a position now: readings are mixed, weak, sideways, or the move is already overextended",
            },
        ),
        "setup_quality": Score(
            instructions=(
                "How strong is the trade setup in `timeframes` for whichever direction the indicators favor? "
                "Judge agreement across timeframes, momentum confirmation, volume, and volatility."
            ),
            criteria=SETUP_LEVELS,
        ),
        "higher_lower_agree": Noul(
            instructions=(
                "Do the `trend` readings of the higher timeframes listed in `timeframe_groups.higher` "
                "and the `trend` readings of the lower timeframes listed in `timeframe_groups.lower` "
                "point in the same direction (both mostly up, or both mostly down)?"
            ),
            criteria={
                "true": "both groups are mostly uptrend, or both groups are mostly downtrend",
                "false": "one group is up while the other is down, or either group is mostly sideways / range-bound",
            },
        ),
        "choppy": Noul(
            instructions=(
                "Is the market described in `timeframes` currently sideways or range-bound, "
                "with no clear direction on most timeframes?"
            ),
            criteria={
                "true": "most `trend` readings say sideways / range-bound and most `adx` readings say no trend or weak trend",
                "false": "most timeframes show a clear uptrend or downtrend",
            },
        ),
        "overextended": Noul(
            instructions=(
                "Do the `rsi`, `stochastic`, and `bollinger` readings in `timeframes` indicate that the current move "
                "is overextended (overbought in an up move, or oversold in a down move) and likely to pause or pull back?"
            ),
            criteria={
                "true": "several timeframes read overbought / oversold, price is stretched outside the Bollinger bands, or bands are expanded after a big move",
                "false": "momentum readings are neutral or mid-range and price sits inside the bands",
            },
        ),
    }
    if has_perspective:
        for side_name in ("long", "short"):
            q[f"macro_against_{side_name}"] = Noul(
                instructions=(
                    f"Considering only `perspective` (its macro_bias, risk_appetite, today_view, event_risk_level, "
                    f"upcoming_events, and trading_stance), does it argue AGAINST opening a {side_name} position right now?"
                ),
                criteria={
                    "true": f"`perspective` leans the opposite way from a {side_name}, says to stay flat, or flags high "
                            "event risk within the next few hours",
                    "false": f"`perspective` is neutral, supports a {side_name}, or allows both sides with low or medium event risk",
                },
            )
    if has_position:
        q["position_action"] = Choice(
            instructions={
                "premise": f"There IS an open {side} position, described in `position`.",
                "question": (
                    "Considering `position` and the current indicator summaries in `timeframes`, "
                    "should this position be kept open or closed now?"
                ),
                "guidance": [
                    "Keep the position while the higher timeframes still support its direction, even if a lower timeframe pulls back.",
                    "Close it when the higher timeframes have turned against its direction, when momentum has clearly reversed, "
                    "or when the position shows a large profit and momentum is overextended.",
                    "Do not close only because of a small loss if the direction is still supported.",
                ],
            },
            criteria={
                "keep": f"keep the {side} position open: its direction is still supported by the indicators",
                "exit": f"close the {side} position now: its direction is no longer supported, momentum reversed, or profit should be protected",
            },
        )
        opposite = "bearish (downtrend, sellers dominate, negative momentum)" if side == "long" else "bullish (uptrend, buyers dominate, positive momentum)"
        q["thesis_invalidated"] = Noul(
            instructions=(
                f"The open position is {side}. Do the `trend`, `adx`, and `macd` readings in `timeframes` now read "
                f"{opposite} on most of the higher timeframes listed in `timeframe_groups.higher`?"
            ),
            criteria={
                "true": f"most higher timeframes now read {opposite}",
                "false": f"most higher timeframes still support the {side} position, or are merely sideways",
            },
        )
    return q


@dataclass
class Judgment:
    entry_action: str
    entry_confidence: float
    entry_probs: dict[str, float]
    setup_score: float
    setup_confidence: float
    higher_lower_agree: float
    choppy: float
    overextended: float
    position_action: str | None = None
    position_confidence: float | None = None
    position_probs: dict[str, float] | None = None
    thesis_invalidated: float | None = None
    macro_against_long: float | None = None
    macro_against_short: float | None = None
    model: str = ""
    input_tokens: int = 0
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        return d


def parse(resp: SystemOneResponse) -> Judgment:
    ea = resp.choices["entry_action"]
    sq = resp.scores["setup_quality"]
    j = Judgment(
        entry_action=ea.choice,
        entry_confidence=ea.confidence,
        entry_probs=dict(ea.probabilities),
        setup_score=sq.score,
        setup_confidence=sq.confidence,
        higher_lower_agree=resp.nouls["higher_lower_agree"].noul,
        choppy=resp.nouls["choppy"].noul,
        overextended=resp.nouls["overextended"].noul,
        model=resp.model,
        input_tokens=resp.usage.input_tokens,
        raw={k: v.model_dump() for k, v in resp.answers.items()},
    )
    if "position_action" in resp.choices:
        pa = resp.choices["position_action"]
        j.position_action = pa.choice
        j.position_confidence = pa.confidence
        j.position_probs = dict(pa.probabilities)
    if "thesis_invalidated" in resp.nouls:
        j.thesis_invalidated = resp.nouls["thesis_invalidated"].noul
    if "macro_against_long" in resp.nouls:
        j.macro_against_long = resp.nouls["macro_against_long"].noul
        j.macro_against_short = resp.nouls["macro_against_short"].noul
    return j


class Judge:
    def __init__(self, settings: Settings, client: TypeSafeClient | None = None):
        self.s = settings
        self.client = client or TypeSafeClient(api_key=settings.typesafe_api_key, model=settings.typesafe_model)

    def judge(self, state: dict, has_position: bool, side: str | None = None) -> Judgment:
        questions = build_questions(has_position, side, has_perspective="perspective" in state)
        resp = self.client.system_one(state, questions)
        return parse(resp)
