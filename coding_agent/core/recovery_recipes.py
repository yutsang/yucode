"""Known failure auto-recovery recipes.

Port of ``claw-code-main/rust/crates/runtime/src/recovery_recipes.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FailureScenario(str, Enum):
    TRUST_PROMPT_UNRESOLVED = "trust_prompt_unresolved"
    PROMPT_MISDELIVERY = "prompt_misdelivery"
    STALE_BRANCH = "stale_branch"
    COMPILE_RED_CROSS_CRATE = "compile_red_cross_crate"
    MCP_HANDSHAKE_FAILURE = "mcp_handshake_failure"
    PARTIAL_PLUGIN_STARTUP = "partial_plugin_startup"
    PROVIDER_FAILURE = "provider_failure"

    @classmethod
    def all(cls) -> list[FailureScenario]:
        return list(cls)

    @classmethod
    def from_worker_failure_kind(cls, kind: str) -> FailureScenario:
        mapping = {
            "trust_gate": cls.TRUST_PROMPT_UNRESOLVED,
            "prompt_delivery": cls.PROMPT_MISDELIVERY,
            "protocol": cls.PROVIDER_FAILURE,
            "provider": cls.PROVIDER_FAILURE,
        }
        return mapping.get(kind, cls.PROVIDER_FAILURE)


class RecoveryStep(str, Enum):
    ACCEPT_TRUST_PROMPT = "accept_trust_prompt"
    REDIRECT_PROMPT_TO_AGENT = "redirect_prompt_to_agent"
    REBASE_BRANCH = "rebase_branch"
    CLEAN_BUILD = "clean_build"
    RETRY_MCP_HANDSHAKE = "retry_mcp_handshake"
    RESTART_PLUGIN = "restart_plugin"
    RESTART_WORKER = "restart_worker"
    ESCALATE_TO_HUMAN = "escalate_to_human"


class EscalationPolicy(str, Enum):
    ALERT_HUMAN = "alert_human"
    LOG_AND_CONTINUE = "log_and_continue"
    ABORT = "abort"


class RecoveryResultKind(str, Enum):
    RECOVERED = "recovered"
    PARTIAL = "partial"
    ESCALATION_REQUIRED = "escalation_required"


@dataclass
class RecoveryRecipe:
    scenario: FailureScenario
    steps: list[RecoveryStep]
    max_attempts: int = 1
    escalation_policy: EscalationPolicy = EscalationPolicy.ALERT_HUMAN


@dataclass
class RecoveryEvent:
    event_type: str
    scenario: FailureScenario | None = None
    detail: str = ""


@dataclass
class RecoveryResult:
    kind: RecoveryResultKind
    steps_taken: list[RecoveryStep] = field(default_factory=list)
    remaining: list[RecoveryStep] = field(default_factory=list)
    reason: str = ""


class RecoveryContext:
    def __init__(self) -> None:
        self._attempts: dict[str, int] = {}
        self.events: list[RecoveryEvent] = []
        self._fail_at_step: int | None = None

    def with_fail_at_step(self, index: int) -> RecoveryContext:
        self._fail_at_step = index
        return self

    def attempt_count(self, scenario: FailureScenario) -> int:
        return self._attempts.get(scenario.value, 0)


def recipe_for(scenario: FailureScenario) -> RecoveryRecipe:
    recipes: dict[FailureScenario, RecoveryRecipe] = {
        FailureScenario.TRUST_PROMPT_UNRESOLVED: RecoveryRecipe(scenario, [RecoveryStep.ACCEPT_TRUST_PROMPT]),
        FailureScenario.PROMPT_MISDELIVERY: RecoveryRecipe(scenario, [RecoveryStep.REDIRECT_PROMPT_TO_AGENT, RecoveryStep.RESTART_WORKER], max_attempts=2),
        FailureScenario.STALE_BRANCH: RecoveryRecipe(scenario, [RecoveryStep.REBASE_BRANCH]),
        FailureScenario.COMPILE_RED_CROSS_CRATE: RecoveryRecipe(scenario, [RecoveryStep.CLEAN_BUILD]),
        FailureScenario.MCP_HANDSHAKE_FAILURE: RecoveryRecipe(scenario, [RecoveryStep.RETRY_MCP_HANDSHAKE], max_attempts=2),
        FailureScenario.PARTIAL_PLUGIN_STARTUP: RecoveryRecipe(scenario, [RecoveryStep.RESTART_PLUGIN]),
        FailureScenario.PROVIDER_FAILURE: RecoveryRecipe(scenario, [RecoveryStep.RESTART_WORKER], escalation_policy=EscalationPolicy.ABORT),
    }
    return recipes.get(scenario, RecoveryRecipe(scenario, [RecoveryStep.ESCALATE_TO_HUMAN]))


def attempt_recovery(scenario: FailureScenario, ctx: RecoveryContext) -> RecoveryResult:
    recipe = recipe_for(scenario)
    attempts = ctx._attempts.get(scenario.value, 0)

    if attempts >= recipe.max_attempts:
        ctx.events.append(RecoveryEvent("escalation_required", scenario, f"Max attempts ({recipe.max_attempts}) reached"))
        return RecoveryResult(kind=RecoveryResultKind.ESCALATION_REQUIRED, reason=f"Max attempts ({recipe.max_attempts}) reached")

    ctx._attempts[scenario.value] = attempts + 1
    steps_taken: list[RecoveryStep] = []

    for i, step in enumerate(recipe.steps):
        if ctx._fail_at_step is not None and i >= ctx._fail_at_step:
            remaining = recipe.steps[i:]
            ctx.events.append(RecoveryEvent("partial_recovery", scenario, f"Failed at step {i}"))
            return RecoveryResult(kind=RecoveryResultKind.PARTIAL, steps_taken=steps_taken, remaining=remaining)
        steps_taken.append(step)

    ctx.events.append(RecoveryEvent("recovery_succeeded", scenario))
    return RecoveryResult(kind=RecoveryResultKind.RECOVERED, steps_taken=steps_taken)
