"""Black-box Critical User Journey evaluation through the portal API.

The evaluation vocabulary is:

``Agent + Persona + Scenario + Goals -> Run -> Assertions``

Goals describe expected tool evidence, response meaning, or soft response
quality. Assertions are the observed pass/fail/inconclusive results from one
Run; they are never declarations embedded in the target agent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class GoalKind(StrEnum):
    TOOL = "tool"
    MESSAGE = "message"
    SOFT = "soft"


class AssertionOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Agent:
    id: str
    agent_id: str
    endpoint: str
    profile: str = "default"


@dataclass(frozen=True)
class Persona:
    """The complete simulated user, including identity and behavior policy."""

    id: str
    name: str
    user_role: str
    description: str = ""
    principal: str = ""
    credential_env: str = ""
    approval_policy: str = "deny"


@dataclass(frozen=True)
class ToolGoal:
    id: str
    description: str
    tool_names: tuple[str, ...]
    minimum_calls: int = 1
    kind: GoalKind = GoalKind.TOOL


@dataclass(frozen=True)
class MessageGoal:
    id: str
    description: str
    required_phrases: tuple[str, ...] = ()
    forbidden_phrases: tuple[str, ...] = ()
    rubric: str = ""
    sources: tuple[str, ...] = ("response",)
    kind: GoalKind = GoalKind.MESSAGE


@dataclass(frozen=True)
class SoftGoal:
    id: str
    description: str
    rubric: str
    max_words: int = 0
    forbidden_phrases: tuple[str, ...] = ()
    kind: GoalKind = GoalKind.SOFT


Goal = ToolGoal | MessageGoal | SoftGoal


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    prompt: str
    goals: tuple[Goal, ...]
    timeout_seconds: float = 1_800
    poll_interval_seconds: float = 2


@dataclass(frozen=True)
class Assertion:
    id: str
    goal_id: str
    outcome: AssertionOutcome
    summary: str
    evidence: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeDecision:
    passed: bool
    summary: str
    evidence: tuple[str, ...] = ()


class SemanticJudge(Protocol):
    def evaluate(
        self,
        *,
        rubric: str,
        response: str,
        context: dict[str, Any],
    ) -> JudgeDecision: ...


@dataclass(frozen=True)
class Run:
    id: str
    agent_id: str
    persona_id: str
    scenario_id: str
    interaction_id: str
    status: str
    started_at: float
    finished_at: float
    conversation: tuple[dict[str, str], ...]
    interaction: dict[str, Any]
    assertions: tuple[Assertion, ...]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["assertions"] = [
            {
                **asdict(assertion),
                "outcome": assertion.outcome.value,
            }
            for assertion in self.assertions
        ]
        return payload


class PortalTransportError(RuntimeError):
    pass


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PortalTransport:
    def __init__(self, endpoint: str, *, token: str = "", timeout: float = 30) -> None:
        endpoint = endpoint.rstrip("/")
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("portal endpoint must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("portal endpoint cannot contain credentials, query, or fragment")
        if token and parsed.scheme != "https":
            raise ValueError("credentialed portal endpoints must use HTTPS")
        self.endpoint = endpoint
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_RejectRedirects())
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload)

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/{path.lstrip('/')}",
            data=data,
            headers=self.headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read(8_192).decode("utf-8", errors="replace")
            try:
                error = json.loads(raw)
            except json.JSONDecodeError:
                error = {}
            detail = error.get("detail", error) if isinstance(error, dict) else {}
            nested = detail.get("error", detail) if isinstance(detail, dict) else {}
            message = nested.get("message") if isinstance(nested, dict) else ""
            raise PortalTransportError(
                str(message or f"portal returned HTTP {exc.code}")
            ) from exc
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise PortalTransportError(
                f"portal request failed ({type(exc).__name__})"
            ) from exc
        if not isinstance(result, dict):
            raise PortalTransportError("portal returned a non-object response")
        return result


class CUJEvaluator:
    def __init__(self, *, judge: SemanticJudge | None = None) -> None:
        self.judge = judge

    def run(self, agent: Agent, persona: Persona, scenario: Scenario) -> Run:
        started = time.time()
        token = os.environ.get(persona.credential_env, "") if persona.credential_env else ""
        transport = PortalTransport(agent.endpoint, token=token)
        session_id = f"portal_eval_{uuid.uuid4().hex}"
        interaction = transport.post(
            "interactions",
            {
                "agentId": agent.agent_id,
                "profile": agent.profile,
                "sessionId": session_id,
                "input": {"text": scenario.prompt},
                "history": [],
            },
        )
        interaction_id = str(interaction.get("interactionId") or "")
        if not interaction_id:
            raise PortalTransportError("portal accepted no interaction identifier")

        deadline = time.monotonic() + max(1, scenario.timeout_seconds)
        while not interaction.get("terminal"):
            if interaction.get("status") == "waiting_for_approval":
                choice = "once" if persona.approval_policy == "approve_once" else "deny"
                interaction = transport.post(
                    f"interactions/{urllib.parse.quote(interaction_id, safe='')}/approval",
                    {"choice": choice},
                )
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                interaction = {
                    **interaction,
                    "status": "evaluator_timed_out",
                    "terminal": False,
                    "error": "Portal interaction did not become terminal before the scenario deadline.",
                    "diagnostics": [
                        "Inspect the interaction and its delegated tasks; do not score the acknowledgement."
                    ],
                }
                break
            time.sleep(max(0, min(scenario.poll_interval_seconds, remaining)))
            interaction = transport.get(
                f"interactions/{urllib.parse.quote(interaction_id, safe='')}"
            )

        return self.evaluate_observed(
            agent,
            persona,
            scenario,
            interaction,
            started_at=started,
        )

    def evaluate_observed(
        self,
        agent: Agent,
        persona: Persona,
        scenario: Scenario,
        interaction: dict[str, Any],
        *,
        started_at: float | None = None,
    ) -> Run:
        """Score a captured portal projection without re-running the agent."""
        interaction_id = str(interaction.get("interactionId") or "")
        if not interaction_id:
            raise ValueError("observed interaction has no interactionId")
        response = str(interaction.get("output") or "")
        conversation = (
            {"role": "user", "content": scenario.prompt},
            {"role": "assistant", "content": response},
        )
        completion = self._completion_assertion(interaction)
        goal_assertions: dict[str, Assertion] = {}
        for goal in scenario.goals:
            if not isinstance(goal, SoftGoal):
                goal_assertions[goal.id] = self._goal_assertion(
                    goal,
                    response=response,
                    interaction=interaction,
                )
        functional_passed = completion.outcome == AssertionOutcome.PASSED and all(
            assertion.outcome == AssertionOutcome.PASSED
            for assertion in goal_assertions.values()
        )
        for goal in scenario.goals:
            if not isinstance(goal, SoftGoal):
                continue
            if functional_passed:
                goal_assertions[goal.id] = self._soft_assertion(
                    goal,
                    response,
                    interaction,
                )
            else:
                goal_assertions[goal.id] = Assertion(
                    f"goal:{goal.id}",
                    goal.id,
                    AssertionOutcome.INCONCLUSIVE,
                    "Soft goal was not evaluated because a functional assertion failed.",
                    (),
                    ("Fix completion, Tool, and Message assertions before judging style.",),
                )
        assertions = [completion]
        assertions.extend(goal_assertions[goal.id] for goal in scenario.goals)
        passed = all(
            assertion.outcome == AssertionOutcome.PASSED for assertion in assertions
        )
        return Run(
            id=f"eval_{uuid.uuid4().hex}",
            agent_id=agent.id,
            persona_id=persona.id,
            scenario_id=scenario.id,
            interaction_id=interaction_id,
            status=str(interaction.get("status") or "unknown"),
            started_at=started_at if started_at is not None else time.time(),
            finished_at=time.time(),
            conversation=conversation,
            interaction=interaction,
            assertions=tuple(assertions),
            passed=passed,
        )

    @staticmethod
    def _completion_assertion(interaction: dict[str, Any]) -> Assertion:
        if interaction.get("terminal") and interaction.get("status") == "completed":
            return Assertion(
                "interaction_completed",
                "interaction_completed",
                AssertionOutcome.PASSED,
                "The full interaction reached completed state.",
                (str(interaction.get("interactionId") or ""),),
            )
        diagnostics = tuple(str(item) for item in interaction.get("diagnostics", []))
        return Assertion(
            "interaction_completed",
            "interaction_completed",
            AssertionOutcome.FAILED,
            f"Interaction ended as {interaction.get('status', 'unknown')}.",
            tuple(filter(None, (str(interaction.get("error") or ""),))),
            diagnostics
            or ("Inspect the root run and delegated tasks before evaluating goals.",),
        )

    def _goal_assertion(
        self,
        goal: Goal,
        *,
        response: str,
        interaction: dict[str, Any],
    ) -> Assertion:
        if isinstance(goal, ToolGoal):
            return self._tool_assertion(goal, interaction)
        if isinstance(goal, MessageGoal):
            return self._message_assertion(goal, response, interaction)
        return self._soft_assertion(goal, response, interaction)

    @staticmethod
    def _tool_assertion(goal: ToolGoal, interaction: dict[str, Any]) -> Assertion:
        calls = interaction.get("toolCalls", [])
        calls = calls if isinstance(calls, list) else []
        actual = [
            str(call.get("name") or "")
            for call in calls
            if isinstance(call, dict) and call.get("status") == "completed"
        ]
        matched = [name for name in actual if name in goal.tool_names]
        if len(matched) >= goal.minimum_calls:
            return Assertion(
                f"goal:{goal.id}",
                goal.id,
                AssertionOutcome.PASSED,
                f"Observed {len(matched)} required tool call(s).",
                tuple(matched),
            )
        return Assertion(
            f"goal:{goal.id}",
            goal.id,
            AssertionOutcome.FAILED,
            "Required tool execution was not observed.",
            tuple(actual),
            (
                "Agent promises and response prose are not tool evidence. Ensure the tool is "
                "actually called and that the portal exposes its trusted audit evidence.",
            ),
        )

    def _message_assertion(
        self,
        goal: MessageGoal,
        response: str,
        interaction: dict[str, Any],
    ) -> Assertion:
        evidence_parts: list[str] = []
        if "response" in goal.sources:
            evidence_parts.append(response)
        if "task_results" in goal.sources:
            for task in interaction.get("tasks", []):
                if not isinstance(task, dict):
                    continue
                evidence_parts.extend(
                    str(task.get(field) or "") for field in ("summary", "error")
                )
        evidence_text = "\n".join(filter(None, evidence_parts))
        folded = evidence_text.casefold()
        missing = [phrase for phrase in goal.required_phrases if phrase.casefold() not in folded]
        forbidden = [phrase for phrase in goal.forbidden_phrases if phrase.casefold() in folded]
        if missing or forbidden:
            problems = [*(f"missing: {item}" for item in missing)]
            problems.extend(f"forbidden: {item}" for item in forbidden)
            return Assertion(
                f"goal:{goal.id}",
                goal.id,
                AssertionOutcome.FAILED,
                "Response content did not satisfy the message goal.",
                tuple(problems),
                ("Revise the final response to cover the missing meaning explicitly.",),
            )
        if goal.rubric:
            return self._judge_assertion(
                goal.id,
                goal.rubric,
                evidence_text,
                interaction,
            )
        return Assertion(
            f"goal:{goal.id}",
            goal.id,
            AssertionOutcome.PASSED,
            "Response contained every required message signal.",
            goal.required_phrases,
        )

    def _soft_assertion(
        self,
        goal: SoftGoal,
        response: str,
        interaction: dict[str, Any],
    ) -> Assertion:
        words = re.findall(r"\b\w+\b", response)
        forbidden = [
            phrase
            for phrase in goal.forbidden_phrases
            if phrase.casefold() in response.casefold()
        ]
        if goal.max_words and len(words) > goal.max_words:
            return Assertion(
                f"goal:{goal.id}",
                goal.id,
                AssertionOutcome.FAILED,
                f"Response used {len(words)} words; limit is {goal.max_words}.",
                (),
                ("Make the final response more concise.",),
            )
        if forbidden:
            return Assertion(
                f"goal:{goal.id}",
                goal.id,
                AssertionOutcome.FAILED,
                "Response used a prohibited style phrase.",
                tuple(forbidden),
                ("Remove the listed phrase and state the outcome directly.",),
            )
        return self._judge_assertion(goal.id, goal.rubric, response, interaction)

    def _judge_assertion(
        self,
        goal_id: str,
        rubric: str,
        response: str,
        interaction: dict[str, Any],
    ) -> Assertion:
        if self.judge is None:
            return Assertion(
                f"goal:{goal_id}",
                goal_id,
                AssertionOutcome.INCONCLUSIVE,
                "A semantic judge is required for this rubric.",
                (),
                ("Configure a judge; never convert an unevaluated soft goal to pass.",),
            )
        decision = self.judge.evaluate(
            rubric=rubric,
            response=response,
            context=interaction,
        )
        return Assertion(
            f"goal:{goal_id}",
            goal_id,
            AssertionOutcome.PASSED if decision.passed else AssertionOutcome.FAILED,
            decision.summary,
            decision.evidence,
        )


def _tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("goal phrase and tool lists must contain only strings")
    return tuple(raw)


def load_matrix(path: Path) -> tuple[Agent, Persona, Scenario]:
    raw = os.path.expandvars(path.read_text(encoding="utf-8"))
    payload = json.loads(raw)
    agent_data = payload["agent"]
    persona_data = payload["persona"]
    scenario_data = payload["scenario"]
    goals: list[Goal] = []
    for goal in scenario_data.get("goals", []):
        kind = GoalKind(goal["type"])
        common = {"id": str(goal["id"]), "description": str(goal["description"])}
        if kind == GoalKind.TOOL:
            goals.append(
                ToolGoal(
                    **common,
                    tool_names=_tuple(goal.get("toolNames")),
                    minimum_calls=int(goal.get("minimumCalls", 1)),
                )
            )
        elif kind == GoalKind.MESSAGE:
            goals.append(
                MessageGoal(
                    **common,
                    required_phrases=_tuple(goal.get("requiredPhrases")),
                    forbidden_phrases=_tuple(goal.get("forbiddenPhrases")),
                    rubric=str(goal.get("rubric") or ""),
                    sources=_tuple(goal.get("sources")) or ("response",),
                )
            )
        else:
            goals.append(
                SoftGoal(
                    **common,
                    rubric=str(goal["rubric"]),
                    max_words=int(goal.get("maxWords", 0)),
                    forbidden_phrases=_tuple(goal.get("forbiddenPhrases")),
                )
            )
    agent = Agent(
        id=str(agent_data["id"]),
        agent_id=str(agent_data["agentId"]),
        endpoint=str(agent_data["endpoint"]),
        profile=str(agent_data.get("profile", "default")),
    )
    persona = Persona(
        id=str(persona_data["id"]),
        name=str(persona_data["name"]),
        user_role=str(persona_data["userRole"]),
        description=str(persona_data.get("description", "")),
        principal=str(persona_data.get("principal", "")),
        credential_env=str(persona_data.get("credentialEnv", "")),
        approval_policy=str(persona_data.get("approvalPolicy", "deny")),
    )
    scenario = Scenario(
        id=str(scenario_data["id"]),
        name=str(scenario_data["name"]),
        prompt=str(scenario_data["prompt"]),
        goals=tuple(goals),
        timeout_seconds=float(scenario_data.get("timeoutSeconds", 1_800)),
        poll_interval_seconds=float(scenario_data.get("pollIntervalSeconds", 2)),
    )
    return agent, persona, scenario


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--observed",
        type=Path,
        help="score a prior run or interaction JSON without contacting the agent",
    )
    arguments = parser.parse_args()
    agent, persona, scenario = load_matrix(arguments.matrix)
    evaluator = CUJEvaluator()
    if arguments.observed:
        observed = json.loads(arguments.observed.read_text(encoding="utf-8"))
        interaction = observed.get("interaction", observed)
        if not isinstance(interaction, dict):
            raise ValueError("observed JSON must contain an interaction object")
        run = evaluator.evaluate_observed(agent, persona, scenario, interaction)
    else:
        run = evaluator.run(agent, persona, scenario)
    rendered = json.dumps(run.to_dict(), indent=2, sort_keys=True)
    print(rendered)
    if arguments.output:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if run.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
