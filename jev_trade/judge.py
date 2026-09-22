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
        "Trade horizon: intraday, the next one to several hours. The 15m, 30m and 1h readings decide the "
        "direction; the 1m, 5m and 10m readings decide the timing.",
        "The higher timeframes (5h, 1d, 1w) and `timeframes.1y` are context, not a veto: they raise or lower "
        "conviction (asked separately) but a trade against them is allowed when the 15m-1h direction and the "
        "lower-timeframe timing agree.",
        "Valid long setups: a pullback toward EMA20/EMA50 inside a 15m-1h uptrend with 1m-10m momentum turning up; "
        "or a breakout above a swing high with rising volume. Valid short setups are the mirror image.",
        "Choose hold only when the 15m-1h readings are sideways / range-bound with no trend, when the lower "
        "timeframes give no timing signal, or when the move is already extreme (RSI over 80 or under 20 on the "
        "timing timeframes, price far outside the Bollinger bands).",
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
                "long": "open a long (buy) now: the 15m-1h direction is up (or a pullback inside an uptrend is ending) "
                        "and the 1m-10m timing readings are turning up",
                "short": "open a short (sell) now: the 15m-1h direction is down (or a bounce inside a downtrend is ending) "
                         "and the 1m-10m timing readings are turning down",
                "hold": "no tradeable intraday setup: the 15m-1h readings are sideways with no trend, the lower "
                        "timeframes give no timing signal, or the move is already extreme",
            },
        ),
        "setup_quality": Score(
            instructions=(
                "How strong is the trade setup in `timeframes` for whichever direction the indicators favor? "
                "Judge agreement across timeframes, momentum confirmation, volume, and volatility."
            ),
            criteria=SETUP_LEVELS,
        ),
        "conviction": Score(
            instructions=(
                "Assume a position will be opened in the direction the indicators favor. How much conviction does "
                "the whole picture give for that trade? Judge agreement across `timeframes`, momentum and volume "
                "confirmation, whether `timeframes.1y` (long-term regime) and `perspective` (if present) favor the "
                "same side, and whether volatility is tradeable. Code maps this to position size and leverage."
            ),
            criteria=[
                "weak: only a few timeframes lean the same way, momentum is unconfirmed, or the long-term regime or "
                "perspective points the other way",
                "moderate: most timeframes agree and at least one momentum indicator confirms; nothing important "
                "argues against the trade",
                "strong: nearly all timeframes agree, momentum and volume confirm, and the long-term regime and "
                "perspective favor the same side",
                "very strong: everything above holds and a fresh trigger (breakout of a swing level, MACD cross, "
                "band expansion after a squeeze) just happened on the lower timeframes with normal, not extreme, volatility",
            ],
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
        "overbought": Noul(
            instructions=(
                "Do the `rsi`, `stochastic`, and `bollinger` readings on the timing and direction timeframes "
                "(1m, 5m, 10m, 15m, 30m, 1h in `timeframes`) show the market is OVERBOUGHT right now, so that a new "
                "long would be buying into an exhausted up move?"
            ),
            criteria={
                "true": "most of those timeframes read overbought (RSI 70+, stochastic 80+) or price is above the upper Bollinger band",
                "false": "momentum on those timeframes is neutral, mid-range, or oversold; a pullback has already reset it",
            },
        ),
        "oversold": Noul(
            instructions=(
                "Do the `rsi`, `stochastic`, and `bollinger` readings on the timing and direction timeframes "
                "(1m, 5m, 10m, 15m, 30m, 1h in `timeframes`) show the market is OVERSOLD right now, so that a new "
                "short would be selling into an exhausted down move?"
            ),
            criteria={
                "true": "most of those timeframes read oversold (RSI 30-, stochastic 20-) or price is below the lower Bollinger band",
                "false": "momentum on those timeframes is neutral, mid-range, or overbought; a bounce has already reset it",
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
    conviction_score: float = 1.0  # 0 weak .. 3 very strong (expected level)
    conviction_confidence: float = 0.0
    higher_lower_agree: float = 0.5
    choppy: float = 0.0
    overextended: float = 0.0  # max(overbought, oversold), kept for logs / older records
    overbought: float = 0.0
    oversold: float = 0.0
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
    cv = resp.scores.get("conviction")
    j = Judgment(
        entry_action=ea.choice,
        entry_confidence=ea.confidence,
        entry_probs=dict(ea.probabilities),
        setup_score=sq.score,
        setup_confidence=sq.confidence,
        conviction_score=cv.score if cv else 1.0,
        conviction_confidence=cv.confidence if cv else 0.0,
        higher_lower_agree=resp.nouls["higher_lower_agree"].noul,
        choppy=resp.nouls["choppy"].noul,
        overbought=resp.nouls["overbought"].noul,
        oversold=resp.nouls["oversold"].noul,
        overextended=max(resp.nouls["overbought"].noul, resp.nouls["oversold"].noul),
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
