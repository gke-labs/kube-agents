from collections.abc import Awaitable, Callable

from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox


def answer_lines(text: str) -> set[str]:
    """The names in a one-per-line answer, tolerating `-`/`*` bullets, backticks and bold."""
    return {line.strip().lstrip("-*").strip().strip("`*").strip() for line in text.splitlines()} - {""}


@scorer(metrics=[accuracy()])
def names_exactly(answer_path: str, expected: Callable[[], Awaitable[list[str]]] | None = None):
    """CORRECT when answer_path lists exactly the expected names (default: the target), one per line."""

    async def score(state: TaskState, target: Target) -> Score:
        try:
            answer = await sandbox().read_file(answer_path)
        except FileNotFoundError:
            return Score(value=INCORRECT, explanation=f"no {answer_path}")
        got, want = answer_lines(answer), set(await expected() if expected else target.target)
        return Score(
            value=CORRECT if got == want else INCORRECT,
            answer=answer,
            explanation=f"missing: {sorted(want - got)}, unexpected: {sorted(got - want)}",
        )

    return score
