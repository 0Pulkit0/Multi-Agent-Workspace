"""Code-focused multi-agent pipeline.

Planner -> Executor -> Harness (-> Orchestrator), with the pipeline for each
mode declared explicitly in PIPELINES rather than sliced out of a list.

The verification stage is not an LLM. The Planner emits an acceptance suite of
plain asserts alongside the spec; `harness.py` extracts the code the Executor
produced and runs it *against that suite*. A step is APPROVED only when the
suite passes -- so code that runs but computes the wrong answer is caught. When
it fails, the real traceback (and the exact failing assertion) is what gets
handed back to the Executor as the fixes.

The suite is audited before it is trusted: a suite that passes against a stub
solution proves nothing, and gating APPROVED on it would rebuild the fake
verification this pipeline exists to remove.
"""

import hashlib
import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import harness

# How many times a single step may be sent back for revision.
MAX_REVISION_ROUNDS = 3

# What to do after the harness rejects a step, in order -- one rung per revision
# round, so len(ESCALATION) must equal MAX_REVISION_ROUNDS.
#
# "Try again" is not a strategy. Handing the same model the same traceback three
# times reliably produces three variations on one wrong idea, so each rung
# changes something structural: first the information available, then the model,
# then the approach. This replaces the Orchestrator, which was asked what to do
# next and had no way to act on the answer.
ESCALATION = ("repair", "alternate", "fresh")

ESCALATION_WHY = {
    "repair": "hand back the real traceback and let the same model fix it",
    "alternate": "same traceback, different model",
    "fresh": "start over from the spec on a different model, no failed code",
}

# Context budget. The rolling context used to be built by string concatenation
# with no ceiling, so it grew with every step and every revision until the
# provider rejected the request. It is now assembled from structured state and
# clamped at every level.
MAX_SPEC_CHARS = 3000
MAX_STEP_OUTPUT_CHARS = 1500
MAX_CONTEXT_CHARS = 8000
CONTEXT_RECENT_STEPS = 2

# Guard against a planner that emits a hundred "steps".
MAX_STEPS = 12

# ------------------------------------------------- providers, roles and models
#
# A provider is a `base_url` and a key. Nothing else is required to add one, so
# any OpenAI-compatible endpoint is a configuration entry rather than a code
# edit. `model` here is only the provider's default, used by a role that does
# not name its own; the mapping that actually decides what gets called is
# role -> (provider, model), resolved at run time by `resolved_roles()`.
#
# Why this does not weaken the pre-registration, since it moves a frozen-looking
# constant into configuration: D2 freezes which models a run *actually used*.
# That is a commitment about a run, not about a source constant. Recording a
# config resolved at run time is strictly more honest than reading a constant out
# of source, because today the frozen commit and the actual run can drift and
# nothing notices -- `--bad-slug` mutates `PROVIDERS` in place, and a manifest
# built by reading source would have reported the pristine slug. Every run JSON
# and both manifests now carry the resolved table, and the preflight refuses to
# start against a pair that does not answer.
PROVIDERS = {
    "gemini": {
        # Verified against ai.google.dev/gemini-api/docs/models (2026-08-28):
        # gemini-3.6-flash is a real, stable model ID. Use the rolling alias
        # "gemini-flash-latest" if you would rather not pin a generation.
        "model": "gemini-3.6-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "groq": {
        # PINNED 2026-09-01 as the Executor for all four arms, per D-6's method
        # and registered as D-13 in `eval/prereg/DECISIONS_R3_ADDENDUM_D.md`.
        # The previous slug here, `llama-3.3-70b-versatile`, 404s with
        # `model_not_found` for this project's key.
        #
        # What the number behind this is, so the constant does not read as more
        # than it is: 18 of 19 hidden-suite passes in the D-9 pin re-run, against
        # 16 of 19 for both `openai/gpt-oss-20b` and `qwen/qwen3.8-27b`. Paired
        # over the same 19 specs that is a 2-0 discordant split, exact McNemar
        # p = 0.50 -- the three candidates are *not* statistically distinguishable
        # at this n. Tier 3 of the task set was never reached, and this model
        # spends ~3.5x qwen's completion tokens. The addendum states all of that
        # and why the pin was still made; nothing here should be read as "the
        # best model won".
        "model": "openai/gpt-oss-120b",
        "base_url": "https://api.groq.com/openai/v1",
    },
}

# Only model-backed roles appear here. The harness runs code, so it has no
# provider and cannot be swapped for one.
ROLE_PROVIDER = {
    "planner": "gemini",
    "test_writer": "gemini",
    "executor": "groq",
}

# Role -> model, overriding the provider's default. Empty by default, which is
# what makes the defaults reproduce today's behaviour exactly: `model_for` falls
# through to `PROVIDERS[provider]["model"]` and nothing changes.
#
# The structural gap this closes: `ROLE_PROVIDER` maps a role to a provider and
# the model rode along from the provider, so a role could not have its own model.
# Two retirements have now cost this project a run -- `gemini-2.0-flash`, then
# `llama-3.3-70b-versatile` -- and each time the repair was a source edit, in a
# workspace whose whole claim is that any model can serve any role with just an
# API key. It is also why the escalation ladder's `alternate` rung had nowhere to
# go without spending the other provider: "a different model" and "a different
# provider" were the same axis.
ROLE_MODEL = {}

# Extra sampling parameters, per role, on top of temperature and top_p. Sent as
# given and recorded as sent, because a provider that needs a parameter the
# others do not -- a reasoning-effort knob, a max-tokens floor -- otherwise
# either cannot be configured or is sent without appearing in the record.
#
# The Executor entry INSTALLS D-6, which registers `reasoning_format="hidden"` on
# every Groq call. It was registered and not installed: this dict was `{}`, both
# sweep entry points resolve through `configure_models()`, and with no
# `models.json` and no `MAW_MODELS` present that left `sampling_for("executor")`
# as temperature and top_p alone. Only `eval/pin_executor.py` supplied the
# parameter, through the role's `params`, which is why the pin was measured with
# it and a calibration sweep would not have reproduced that.
#
# The reason is the gap and not a preference: `reasoning_format: "hidden"` is on
# 57 of 57 Executor calls in `eval/results/pin-executor/ledger.jsonl` and on 57 of
# 57 in `eval/results/pin-replay-1/ledger.jsonl` -- the run D-13 pinned from -- so
# a sweep without it measures `d_t` in an environment the pin was never measured
# in. D-6 also measured what unset costs: `gpt-oss-20b` with it unset returned
# zero fenced code blocks while 568 characters went to a separate `reasoning`
# field, so the Executor's extractor saw no code at all. Registered as D-14 in
# `eval/prereg/DECISIONS_R3_ADDENDUM_E.md`.
#
# One consequence, stated rather than left to be found: `sampling_for` is keyed by
# role and not by provider, so a *failover* to Gemini outside measurement mode
# would send a Groq extension to Google. Measurement mode has no failover -- the
# provider order is one long -- so no sweep can reach it; the Streamlit app can.
# Not repaired here, because making sampling provider-aware changes the contract
# that the record and the wire are the same dict.
ROLE_PARAMS = {"executor": {"reasoning_format": "hidden"}}

# Sampling is pinned here rather than left to the endpoint. Free tiers override
# unpinned parameters with their own defaults and change them without notice, so
# a run that does not state top_p is not reproducible even against the same
# model ID.
TEMPERATURE = 0.4
TOP_P = 1.0

# Where a configuration comes from. `MAW_MODELS` is either inline JSON or a path
# to a JSON file; `models.json` beside this module is the no-environment default.
# Absent both, the source defaults stand and behaviour is exactly what it was.
MODEL_CONFIG_ENV = "MAW_MODELS"
MODEL_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "models.json")

# What the resolution actually used, for the record. Not a claim about source.
_MODEL_CONFIG_SOURCE = "defaults"


def model_config_source():
    """Where the live role->model mapping came from. Recorded, not inferred."""
    return _MODEL_CONFIG_SOURCE


def key_env(provider):
    """The environment variable holding ``provider``'s key.

    Derived from the name -- `groq` -> `GROQ_API_KEY` -- unless the provider names
    its own `env`. Without this, "a provider is a base_url plus a key and nothing
    else" was not true: the key lookup was a hardcoded two-entry dict, so a
    configured provider had no way to be *given* a key and adding one meant a
    source edit after all. The default reproduces the two names the project has
    always used.
    """
    cfg = PROVIDERS.get(provider) or {}
    return cfg.get("env") or (provider.upper().replace("-", "_") + "_API_KEY")


def keys_from_env(env=None):
    """provider -> key, for every configured provider. Missing reads as "".

    Derived from `PROVIDERS`, so a provider added by configuration is reachable
    without touching code, and never logged: only the variable *names* are ever
    printed anywhere.
    """
    source = os.environ if env is None else env
    return dict((name, source.get(key_env(name), "").strip())
                for name in PROVIDERS)


def key_env_hint():
    """`GEMINI_API_KEY / GROQ_API_KEY`, built from the live configuration.

    The message that names the variables to set had them spelled as a literal,
    which goes stale the moment a provider is configured.
    """
    return " / ".join(key_env(name) for name in sorted(PROVIDERS))


def model_for(role, provider=None):
    """The model ``role`` will actually be called with, on ``provider``.

    ``provider`` defaults to the role's own. When it is *not* the role's own --
    the escalation ladder's `alternate` rung, or a failover outside measurement
    mode -- the role's override deliberately does not apply: an override names a
    model at the role's own endpoint, and asking a different endpoint for it
    would 404 in a way that reads as a provider outage.

    One function, so the record, the preflight and the call cannot disagree about
    what is being sent -- the failure mode of a manifest built by reading source.

    A same-provider alternate model is now *expressible* here, which is what the
    `alternate` rung wanted and could not have while the model rode along with
    the provider. It is deliberately not wired up: what `alternate` means is part
    of arm B's definition and changing it is a registration decision.
    """
    if provider is None:
        provider = ROLE_PROVIDER[role]
    if provider == ROLE_PROVIDER.get(role) and ROLE_MODEL.get(role):
        return ROLE_MODEL[role]
    return PROVIDERS[provider].get("model")


def sampling_for(role):
    """Every sampling parameter that will be sent for ``role``, resolved.

    `temperature` and `top_p` always, plus whatever `ROLE_PARAMS` adds. The
    return value is what goes on the wire *and* what goes in the record, so a
    parameter cannot be sent without appearing in the run's own account of it.
    """
    params = {"temperature": TEMPERATURE, "top_p": TOP_P}
    params.update(ROLE_PARAMS.get(role) or {})
    return params


def resolved_roles(roles=None):
    """The resolved role table: role, provider, model, base_url and sampling.

    This is the record. It is built by asking the same functions the call path
    asks, at the moment of recording, rather than by reading `PROVIDERS` out of
    source -- `--bad-slug` mutates `PROVIDERS` in place, and a manifest built
    from source would have reported the pristine slug while the run used the dead
    one. Sorted by role so two runs' tables compare directly.
    """
    names = sorted(ROLE_PROVIDER if roles is None else roles)
    table = []
    for role in names:
        provider = ROLE_PROVIDER[role]
        entry = {
            "role": role,
            "provider": provider,
            "model": model_for(role),
            "base_url": PROVIDERS[provider].get("base_url"),
        }
        entry.update(sampling_for(role))
        table.append(entry)
    return table


def resolved_pairs(roles=None):
    """The distinct ``(provider, model)`` pairs a run will call, with their roles.

    Distinct, because the preflight costs one call per pair and `planner` and
    `test_writer` are the same pair today: validating each role separately would
    spend three calls to learn two things.
    """
    pairs = {}
    for entry in resolved_roles(roles):
        pairs.setdefault((entry["provider"], entry["model"]), []).append(
            entry["role"])
    return [(provider, model, sorted(role_names))
            for (provider, model), role_names in sorted(
                pairs.items(), key=lambda item: item[0])]


class ConfigError(Exception):
    """A configuration that would not have done what it said. Refused, not fixed."""


def apply_model_config(config, source="explicit"):
    """Install a role/provider/model configuration. Returns the resolved table.

    Validated before anything is installed, and installed all at once, because a
    half-applied config is a run pointed somewhere nobody chose. Raises
    `ConfigError` on an unknown role, a provider without a `base_url`, a role
    naming a provider that does not exist, or a non-string model.
    """
    global _MODEL_CONFIG_SOURCE
    config = config or {}
    if not isinstance(config, dict):
        raise ConfigError("a model config must be a JSON object, got %s"
                          % type(config).__name__)
    unknown = sorted(set(config) - {"providers", "roles", "sampling"})
    if unknown:
        raise ConfigError("unknown model config section(s): %s"
                          % ", ".join(unknown))

    providers = dict((name, dict(cfg)) for name, cfg in PROVIDERS.items())
    for name, cfg in (config.get("providers") or {}).items():
        if not isinstance(cfg, dict):
            raise ConfigError("provider %r must be an object" % name)
        extra = sorted(set(cfg) - {"base_url", "model", "env"})
        if extra:
            raise ConfigError(
                "provider %r has field(s) %s; a provider is a base_url, a key "
                "and at most a default model -- sampling is per-role and belongs "
                "in that role's \"params\"" % (name, ", ".join(extra)))
        if "env" in cfg and not (isinstance(cfg["env"], str)
                                 and cfg["env"].strip()):
            raise ConfigError("provider %r has a non-string env: %r"
                              % (name, cfg["env"]))
        merged = dict(providers.get(name) or {})
        merged.update(cfg)
        if not merged.get("base_url"):
            raise ConfigError("provider %r has no base_url" % name)
        providers[name] = merged

    role_provider = dict(ROLE_PROVIDER)
    role_model = dict(ROLE_MODEL)
    role_params = dict(ROLE_PARAMS)
    for role, cfg in (config.get("roles") or {}).items():
        if role not in ROLE_PROVIDER:
            raise ConfigError(
                "unknown role %r; the model-backed roles are %s (the harness "
                "runs code and has no provider)"
                % (role, ", ".join(sorted(ROLE_PROVIDER))))
        if isinstance(cfg, str):
            cfg = {"model": cfg}
        if not isinstance(cfg, dict):
            raise ConfigError("role %r must be an object or a model string" % role)
        extra = sorted(set(cfg) - {"provider", "model", "params"})
        if extra:
            raise ConfigError("role %r has unknown field(s): %s"
                              % (role, ", ".join(extra)))
        if "provider" in cfg:
            if cfg["provider"] not in providers:
                raise ConfigError(
                    "role %r names provider %r, which is not configured; add it "
                    "under \"providers\" with a base_url"
                    % (role, cfg["provider"]))
            role_provider[role] = cfg["provider"]
        if "model" in cfg:
            if not isinstance(cfg["model"], str) or not cfg["model"].strip():
                raise ConfigError("role %r has a non-string model: %r"
                                  % (role, cfg["model"]))
            role_model[role] = cfg["model"].strip()
        if "params" in cfg:
            if not isinstance(cfg["params"], dict):
                raise ConfigError("role %r's params must be an object" % role)
            role_params[role] = dict(cfg["params"])

    sampling = config.get("sampling") or {}
    if not isinstance(sampling, dict):
        raise ConfigError("\"sampling\" must be an object")
    extra = sorted(set(sampling) - {"temperature", "top_p"})
    if extra:
        raise ConfigError(
            "\"sampling\" takes temperature and top_p; %s is per-role and "
            "belongs in that role's \"params\"" % ", ".join(extra))

    # Every role must resolve to a model, or the run would send `model=None` and
    # get a 400 several minutes in instead of a refusal now.
    for role, provider in role_provider.items():
        model = role_model.get(role) or providers[provider].get("model")
        if not model:
            raise ConfigError(
                "role %r resolves to no model: provider %r has no default and "
                "the role names none" % (role, provider))

    global TEMPERATURE, TOP_P
    PROVIDERS.clear()
    PROVIDERS.update(providers)
    ROLE_PROVIDER.clear()
    ROLE_PROVIDER.update(role_provider)
    ROLE_MODEL.clear()
    ROLE_MODEL.update(role_model)
    ROLE_PARAMS.clear()
    ROLE_PARAMS.update(role_params)
    if "temperature" in sampling:
        TEMPERATURE = float(sampling["temperature"])
    if "top_p" in sampling:
        TOP_P = float(sampling["top_p"])
    _MODEL_CONFIG_SOURCE = source
    return resolved_roles()


def load_model_config(env=None, path=None):
    """Read a config from the environment or a file. Returns ``(config, source)``.

    ``(None, "defaults")`` when neither is present, which is the day-one case and
    must leave behaviour untouched.
    """
    raw = (os.environ if env is None else env).get(MODEL_CONFIG_ENV, "").strip()
    if raw:
        if raw.startswith("{"):
            try:
                return json.loads(raw), "%s (inline)" % MODEL_CONFIG_ENV
            except ValueError as exc:
                raise ConfigError("%s is not valid JSON: %s"
                                  % (MODEL_CONFIG_ENV, exc))
        path = raw
    if path is None:
        path = MODEL_CONFIG_FILE
        if not os.path.exists(path):
            return None, "defaults"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), path
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc))
    except IOError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))


def configure_models(env=None, path=None):
    """Resolve and install the configuration. Returns the resolved role table.

    Called once by each entry point. Idempotent on the defaults: with no config
    present it installs nothing and the source stays ``"defaults"``.
    """
    config, source = load_model_config(env=env, path=path)
    if config is None:
        return resolved_roles()
    return apply_model_config(config, source=source)


# Roles whose models must differ for "the grader never wrote the code" to hold.
# `executor` writes the code; `test_writer` writes the suite that grades it.
INDEPENDENCE_PAIRS = (("executor", "test_writer"), ("executor", "planner"))


def independence_warnings():
    """Where the current configuration destroys grader independence.

    The claim this protects is "execution grades, and the grader never wrote the
    code". It is already only partly true: `planner` and `test_writer` are one
    model off one spec lineage, and the Planner writes the suite the Executor is
    graded against. A configuration that also points the Executor at that model
    makes the claim false outright, and that must not be silent -- so it is said
    at startup and written into the manifest, where a reader of the results can
    see it without re-deriving the configuration.
    """
    said = []
    for role, other in INDEPENDENCE_PAIRS:
        if role not in ROLE_PROVIDER or other not in ROLE_PROVIDER:
            continue
        if (ROLE_PROVIDER[role], model_for(role)) == (ROLE_PROVIDER[other],
                                                     model_for(other)):
            said.append(
                "grader independence: %s and %s are the same (provider, model) "
                "pair -- %s/%s -- so the model that %s is also the model that %s"
                % (role, other, ROLE_PROVIDER[role], model_for(role),
                   "wrote the code" if role == "executor" else "graded it",
                   "wrote the suite grading it" if other == "test_writer"
                   else "wrote the spec and the suite"))
    return said


# Silent failover is a convenience in a UI and a contaminant in a measurement.
# `call_role` walks every configured provider on any failure, so under 429
# pressure -- which arm B absorbs roughly ten times more of than a single-shot
# arm -- "arm B" quietly becomes a provider mixture whose composition tracks
# rate-limit pressure, and therefore time of day. Task-level pairing cannot
# cancel a mix that correlates with the arm. Worse, one wrong model slug is
# enough to route an entire grid to the other provider while the report still
# claims two.
#
# Measurement mode pins each call to exactly one provider and turns a dead
# provider into a failed task, which is loud. Read once at import so a run
# cannot change policy halfway through; `set_measurement_mode` exists for tests.
MEASUREMENT_MODE = os.environ.get("MAW_MEASUREMENT", "").strip().lower() not in (
    "", "0", "false", "no")

# Every model call, in order, for the caller to record. Bounded because a long
# UI session would otherwise accumulate one entry per call forever.
CALL_LOG = []
MAX_CALL_LOG = 200


def set_measurement_mode(enabled):
    """Turn silent provider failover off (True) or on (False). Returns the new
    value. Only the environment should set this in production; this exists so a
    check can exercise both policies in one process."""
    global MEASUREMENT_MODE
    MEASUREMENT_MODE = bool(enabled)
    return MEASUREMENT_MODE


def reset_call_log():
    del CALL_LOG[:]
    return CALL_LOG

# The pipeline for each mode, stated outright. Every stage listed here is a
# stage that actually runs.
PIPELINES = {
    2: ("planner", "executor"),
    3: ("planner", "executor", "harness"),
}
DEFAULT_MODE = 3

PIPELINE_LABELS = {
    2: "Planner -> Executor (no verification)",
    3: "Planner -> Executor -> Harness (code is run against the suite)",
}

# Mode 4 was Planner -> Executor -> Harness -> Orchestrator. The Orchestrator
# was a model asked to comment on progress: it cost one call per step, could not
# change any outcome, and its assessments regularly contradicted results the
# harness had already established by running the code. Deciding what to do after
# a failure is now deterministic Python (see ESCALATION) -- no model is asked
# what to try next, because that decision needs no judgement.
#
# The mapping is declared rather than left to resolve_mode's fallback, so a
# saved mode-4 config gets an explanation instead of silently changing pipeline.
RETIRED_MODES = {
    4: (3, "Mode 4 (the Orchestrator) has been retired. It spent a model call "
           "per step to narrate results the harness had already proven, and "
           "could not act on them. Escalation after repeated failure is now a "
           "deterministic policy. Running mode 3 instead."),
}


def retired_mode_note(mode):
    """The explanation for a retired mode, or "" for a live one."""
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        return ""
    entry = RETIRED_MODES.get(mode)
    return entry[1] if entry else ""


# Where a suite came from, and whether APPROVED may be gated on it.
TESTS_USER = "user"
TESTS_GENERATED = "generated"
TESTS_REGENERATED = "regenerated"
TESTS_VACUOUS = "vacuous"
TESTS_MISSING = "missing"
TESTS_UNUSABLE = "unusable"

TRUSTED_TESTS = (TESTS_USER, TESTS_GENERATED, TESTS_REGENERATED)


class ProviderError(Exception):
    """A provider call that failed, carrying *why* and not only that it did.

    ``status`` is the provider's HTTP status when there was one, else None.
    ``exc_class`` is the name of the exception the SDK actually raised.
    ``retry_after`` is the server's own ``Retry-After``, in seconds, when it sent
    one -- the only wait figure in this system that is not a guess.

    Both exist because the message alone is unusable for policy. Flattened to a
    string, a 429 (wait and try again), a 404 (the model slug is wrong and will
    stay wrong) and a `KeyError` in this module (our bug) are one indistinguishable
    line -- so retry cannot be written at all. Retrying a 404 burns the whole
    backoff budget on a dead slug; not retrying a 429 scores a rate limit as a
    model failure, which is the contamination the eval exists to avoid.
    """

    def __init__(self, message, status=None, exc_class="", retry_after=None):
        Exception.__init__(self, message)
        self.status = status
        self.exc_class = exc_class or ""
        self.retry_after = retry_after


class DailyQuotaExhausted(Exception):
    """A per-day quota is spent. Not retryable, and not one task's failure.

    Deliberately **not** a `ProviderError`. Every `except ProviderError` in this
    codebase means "this call failed, record it and carry on", which is the right
    posture for a 404 or a per-minute 429 and exactly the wrong one here: the
    counter resets on the provider's daily boundary, so carrying on spends the
    rest of the run's requests against a wall. The pin probe did that -- 17 units
    at 5 attempts each, 85 requests and nine minutes of backoff sleep against a
    20-request ceiling. Not being a `ProviderError` is what makes the abort
    reach the runner instead of being absorbed one unit at a time.
    """

    def __init__(self, message, quota_id="", model="", provider="", role="",
                 retry_after=None):
        Exception.__init__(self, message)
        self.quota_id = quota_id or ""
        self.model = model or ""
        self.provider = provider or ""
        self.role = role or ""
        self.retry_after = retry_after


class ModelMismatch(Exception):
    """The API answered as a model other than the one this call asked for.

    Also deliberately **not** a `ProviderError`, for the reason
    `DailyQuotaExhausted` gives above: every `except ProviderError` here means
    "this call failed, record it and carry on", and `calibrate.one_draw` would
    turn this into one `OUTCOME_INFRA_LOSS` and draw the next cell. A rate limit
    is a fact about a cell. This is a fact about the whole run -- the draws
    already on disk claim a model that did not answer them, so continuing adds
    more of them.

    Not retryable either, and for a stronger reason than a quota: a provider
    serving an alias will serve the same alias on the next attempt, so a retry
    spends a request to be told the same thing.

    Why this is a halt and not a warning: three models have been retired under
    this project mid-flight -- `gemini-2.0-flash`, `llama-3.3-70b-versatile`,
    `openai/gpt-oss-20b` -- and two of them cost a run. Every other member of
    that family announces itself as a 404 or an empty completion. A provider
    quietly serving a different model than the slug requested is the one that
    leaves no trace, and a warning line in a 108-cell grid is a line nobody
    reads. A run whose Executor was not the registered Executor is not the run
    D-16 describes, which is the same reasoning that makes a `prompt_sha256`
    mismatch halt under addendum I rather than redraw one task.
    """

    def __init__(self, message, requested="", returned="", provider="", role=""):
        Exception.__init__(self, message)
        self.requested = requested or ""
        self.returned = returned or ""
        self.provider = provider or ""
        self.role = role or ""


# Slug rewrites that are known to be cosmetic, as {returned: requested}. Empty,
# and empty on evidence rather than on optimism: across the 35 files in
# `eval/results/` there are 133 completions carrying both fields -- 114 from groq
# over `openai/gpt-oss-120b`, `openai/gpt-oss-20b` and `qwen/qwen3.8-27b`, and 19
# from gemini over `gemini-3.6-flash` -- and requested equals returned in all 133,
# including on Gemini's OpenAI-compatible endpoint, which is where a `models/`
# prefix would have shown up if it were coming.
#
# It exists empty so that the *shape* of any future loosening is fixed in advance:
# a decorated slug gets one entry here, in a diff that says which provider
# decorated what, and never a looser comparison at the call site. `startswith`, an
# `in`, or a strip of everything before the last `/` would each also accept a
# genuinely different model, which is the failure this is here to catch.
MODEL_SLUG_NORMALISATIONS = {}


def normalise_model_slug(slug):
    """A returned slug reduced to the form a requested slug is written in.

    The identity, until a provider is *observed* decorating one. See
    `MODEL_SLUG_NORMALISATIONS`.
    """
    if not slug:
        return slug
    return MODEL_SLUG_NORMALISATIONS.get(slug, slug)


def _status_of(exc):
    """The HTTP status on an SDK exception, or None.

    Three shapes, tried in order, because the OpenAI-compatible clients are not
    consistent about which one they expose. The SDK is deliberately not imported
    to do this: this module -- and both offline suites -- must load without it,
    so the status is read by attribute name and never by isinstance.
    """
    for attribute in ("status_code", "status"):
        value = getattr(exc, attribute, None)
        status = _as_status(value)
        if status is not None:
            return status
    response = getattr(exc, "response", None)
    if response is not None:
        return _as_status(getattr(response, "status_code", None))
    return None


RETRY_AFTER_HEADERS = ("retry-after", "Retry-After",
                       "x-ratelimit-reset-requests")

# A server that says "wait an hour" gets believed only up to this. Past it the
# run should stop and be restarted later, not hold a socket open for an hour and
# report the hour as part of a task's wall clock.
MAX_RETRY_AFTER_SECONDS = 120.0


def _retry_after_of(exc):
    """The server's ``Retry-After`` in seconds, or None.

    Preferred over our own backoff when present: the provider knows when its
    window resets and we are guessing. Clamped, and never negative -- a malformed
    header must not be able to park the run.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    for name in RETRY_AFTER_HEADERS:
        try:
            raw = headers.get(name)
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            seconds = float(str(raw).strip().rstrip("s"))
        except (TypeError, ValueError):
            continue
        if seconds < 0:
            continue
        return min(seconds, MAX_RETRY_AFTER_SECONDS)
    return None


def _as_status(value):
    """An HTTP status as an int, or None. A string ``"429"`` counts."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def resolve_mode(mode):
    """Coerce a UI value into a known pipeline mode.

    Retired modes map to their declared replacement rather than falling through
    to the default, so ``retired_mode_note`` can explain the substitution.
    """
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        return DEFAULT_MODE
    if mode in PIPELINES:
        return mode
    if mode in RETIRED_MODES:
        return RETIRED_MODES[mode][0]
    return DEFAULT_MODE


def pipeline_for(mode):
    return PIPELINES[resolve_mode(mode)]


def required_providers(mode):
    """Which API keys a given mode actually needs."""
    return sorted({ROLE_PROVIDER[stage] for stage in pipeline_for(mode)
                   if stage in ROLE_PROVIDER})


# Sampling parameters the OpenAI SDK accepts as typed keyword arguments. Anything
# else a role's `params` names is treated as a provider extension and rides in
# `extra_body`.
#
# The direction of the rule is deliberate: *unknown* goes to `extra_body`, not to
# a keyword argument. An unrecognised keyword is a TypeError raised inside the SDK
# and wrapped below as a ProviderError, so a configuration typo would be reported
# as "groq failed" -- indistinguishable from a provider outage, and retried as one.
# Through `extra_body` the same name reaches the server, which either honours it or
# says what is wrong with it.
#
# Measured 2026-08-30, and the reason this split exists at all: `reasoning_format`
# is a Groq extension. gpt-oss-20b with it unset returned zero fenced blocks and
# put 568 characters into a separate `reasoning` field, so the Executor's own
# extractor saw no code. `hidden` fixed it. Before this split the parameter could
# not be *sent*, because `sampling_for` fed `**params` straight into `create()`.
_SDK_KEYWORD_PARAMS = frozenset([
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "n", "seed",
    "stop", "presence_penalty", "frequency_penalty", "logprobs", "top_logprobs",
    "response_format", "stream",
])


def split_params(params):
    """``(keyword_arguments, extra_body)`` for one call's sampling parameters.

    The two dicts together are exactly ``params`` -- nothing dropped, nothing
    invented -- so the record `sampling_for` builds still describes the wire.
    """
    keywords, extra = {}, {}
    for name, value in (params or {}).items():
        (keywords if name in _SDK_KEYWORD_PARAMS else extra)[name] = value
    return keywords, extra


# The detail of the most recent completion `call_model` obtained, or `{}`.
#
# A side channel, and named as one rather than disguised. `call_model` returns a
# string because roughly two dozen stand-ins across the offline suites replace
# that exact symbol with a function that returns a string, so its return type
# cannot be widened to carry `finish_reason` without rewriting all of them -- and
# `_attempt_provider` calls `call_model`, not `call_model_detailed`, precisely so
# those stand-ins reach the retry layer.
#
# The staleness hazard is closed by clearing, not by trusting: `_attempt_provider`
# clears this before every attempt and reads it after, so `{}` means "no real
# completion was observed on this attempt" -- a stubbed call -- and never a value
# left behind by an earlier draw. That is the same rule `pin_executor.CaptureDetail`
# states for `replies[-1]`, which is not a retry's ghost because a failed attempt
# raises before it appends.
_LAST_COMPLETION = {}


def last_completion():
    """The most recent completion's detail, or ``{}``. A copy, not the dict."""
    return dict(_LAST_COMPLETION)


def note_completion(detail):
    """Record (or, with a falsy ``detail``, forget) the last completion."""
    _LAST_COMPLETION.clear()
    if detail:
        _LAST_COMPLETION.update(detail)


# The second side channel, and it exists for the same reason as the first: the
# thing that knows the answer sits *below* the thing that writes the record.
#
# `eval/run_eval.Instrument` replaces `call_model` -- the symbol `_attempt_provider`
# calls -- so every wrapper-level retry, and every second it sleeps between them,
# happens inside one `_attempt_provider` invocation. `_attempt_provider` then emits
# a record whose `seconds_backoff` is 0.0 and whose `attempts` is 1, because from
# where it stands that is true. On the 2026-09-02 tier-3 sweep that produced 152
# call events every one of which claimed zero waiting, across draws that took up
# to 8467.2s. The number was not wrong, it was answered by the wrong layer.
#
# Same staleness rule as `_LAST_COMPLETION`, for the same reason: cleared before
# the call and read after, so `{}` means "the layer below reported nothing" -- a
# stand-in, or an uninstrumented path -- and never a previous attempt's total.
_LAST_WAITS = {}

_WAIT_KEYS = ("paced", "backoff", "http_attempts", "seconds_http",
              "seconds_http_max")


def last_waits():
    """What the layer below waited and how often it went to the wire, or ``{}``."""
    return dict(_LAST_WAITS)


def note_waits(waits):
    """Record (or, with a falsy ``waits``, forget) the layer below's waiting.

    Keys are `_WAIT_KEYS`. Unknown keys raise rather than being dropped: this is
    a channel between two files, and a typo here would read downstream as "that
    layer waited zero", which is the exact failure it was added to fix.
    """
    _LAST_WAITS.clear()
    if not waits:
        return
    unknown = [name for name in waits if name not in _WAIT_KEYS]
    if unknown:
        raise ValueError("unknown wait key(s) %s; the vocabulary is %s"
                         % (", ".join(sorted(unknown)), ", ".join(_WAIT_KEYS)))
    _LAST_WAITS.update(waits)


def _fold_waits(record):
    """Move the layer below's waiting onto ``record``, in that layer's own terms.

    Additive on the two seconds fields, because `_attempt_provider` has its own
    pacing and its own backoff and both are real; this adds a second contributor
    rather than overwriting a first. `http_attempts` is assigned, not added: it
    is the count for this provider attempt, and the loop calls this once per
    attempt.

    Absent keys leave the record alone. An uninstrumented path -- the interactive
    app, or any of the two dozen `call_model` stand-ins -- then leaves
    `http_attempts` at 0, which reads as "nothing below reported" and is honest;
    a 1 written here would be this function inventing a measurement.
    """
    waits = _LAST_WAITS
    if not waits:
        return record
    if "paced" in waits:
        record["seconds_paced"] = round(
            record["seconds_paced"] + waits["paced"], 3)
    if "backoff" in waits:
        record["seconds_backoff"] = round(
            record["seconds_backoff"] + waits["backoff"], 3)
    for name in ("http_attempts", "seconds_http", "seconds_http_max"):
        if name in waits:
            record[name] = waits[name]
    return record


def _sdk_retry_kwargs():
    """``max_retries`` for a client on the measured path, or nothing at all.

    openai 2.48.0 defaults to ``DEFAULT_MAX_RETRIES = 2``, so three HTTP tries
    per request, and it retries on 408, 409, 429 and every status >= 500
    (`_base_client._should_retry`). None of those tries is visible to
    `Instrument.calls`, to the event log, or to the manifest's `spent`: the SDK
    makes them beneath the lowest layer this project instruments. On 2026-09-02
    the provider's own counter caught them -- Google recorded 20 requests against
    13 planner events, and two 503s alone account for up to six of the 20 against
    a 20-per-day quota.

    So under measurement mode the SDK's retry layer is turned off and the
    project's own layers do the retrying, where a request is counted before it is
    made. Outside measurement mode nothing is passed and the installed SDK's
    default stands: `agents_core.py:1198-1204` registers that the interactive app
    deliberately keeps retry coverage this eval gives up, and pinning a number
    here would freeze the app's robustness to whatever 2.48.0 happens to default
    to.
    """
    return {"max_retries": 0} if MEASUREMENT_MODE else {}


def _sdk_default_max_retries():
    """The installed SDK's own retry default, or None if openai is not importable.

    Read from the library rather than written down, because the whole point of the
    number on the manifest is to say what `{}` from `_sdk_retry_kwargs` meant on
    the day of the run. A literal 2 here would keep reading 2 after an upgrade
    changed it, which is the failure mode this field exists to close.

    `openai` is imported inside the function for the same reason every other
    import of it in this module is: the module has to import with the dependency
    absent, which is how both offline suites run.
    """
    try:
        import openai
    except Exception:
        return None
    return getattr(openai, "DEFAULT_MAX_RETRIES", None)


def call_model_detailed(provider, api_key, system, user, role=None):
    """One model call, with the accounting a bare string cannot carry.

    Returns a JSON-serialisable dict:

      ``text``             the reply, stripped -- byte-identical to what
                           `call_model` returns, so a probe measures the same
                           bytes the pipeline's extractor would see
      ``model_requested``  the slug this call asked for
      ``model_returned``   the slug the API says answered
      ``usage``            the provider's usage block, unedited
      ``finish_reason``    why generation stopped
      ``params``           every sampling parameter sent, by its own name
      ``extra_body``       which of those went through `extra_body`
      ``reasoning_chars``  length of a separate `reasoning` field, 0 if absent

    ``model_returned`` is recorded because "we asked for X" and "X answered" are
    different claims -- a provider may serve an alias or a dated snapshot -- and a
    run whose whole purpose is to pin a model has to make the second one.

    ``usage`` in full, because ``reasoning_format="hidden"`` suppresses the
    *reporting* of reasoning and not its computation. Those tokens are billed and
    are otherwise invisible in both the reply text and the record.

    No caching here or anywhere beneath it; see the standing prohibition in
    `_attempt_provider`.
    """
    cfg = PROVIDERS[provider]
    model = (model_for(role, provider) if role in ROLE_PROVIDER
             else cfg.get("model"))
    params = sampling_for(role)
    keywords, extra = split_params(params)
    try:
        # Imported lazily so this module (and the offline test suites) load
        # without the SDK installed.
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=cfg["base_url"], timeout=60,
                        **_sdk_retry_kwargs())
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **(dict(keywords, extra_body=extra) if extra else keywords))
        # Inside the try on purpose: an empty `choices` is an IndexError, and it
        # has always surfaced as a ProviderError so the retry layer can see it.
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        if usage is not None and hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        return {
            "text": (choice.message.content or "").strip(),
            "model_requested": model,
            "model_returned": getattr(resp, "model", None),
            "usage": dict(usage) if usage else {},
            "finish_reason": getattr(choice, "finish_reason", None),
            "params": params,
            "extra_body": extra,
            "reasoning_chars": len(getattr(choice.message, "reasoning", None)
                                   or ""),
        }
    except Exception as exc:
        # Message shape unchanged -- every existing check and log line reads it.
        # The status, the class and the server's own wait ride alongside it.
        raise ProviderError("%s failed: %s" % (provider, exc),
                            status=_status_of(exc),
                            exc_class=type(exc).__name__,
                            retry_after=_retry_after_of(exc))


def call_model(provider, api_key, system, user, role=None):
    """One model call, returning the reply text. ``role`` decides model and sampling.

    ``role`` is optional so that the four-positional shape every stand-in in the
    test suites was written against still works; without it the provider's
    default model is used, which is what this function did before roles could
    carry their own.

    The text-only face of `call_model_detailed`, and deliberately still the name
    the pipeline calls: roughly two dozen stand-ins across the offline suites
    replace *this* symbol with a function returning a string. A caller that wants
    the usage block or the model the API actually returned asks for the detail.

    The detail is also parked in `_LAST_COMPLETION` on the way past, so
    `_attempt_provider` can put `finish_reason` on the call record without this
    function's return type -- or those two dozen stand-ins -- having to change.
    """
    detail = call_model_detailed(provider, api_key, system, user, role=role)
    note_completion(detail)
    return detail["text"]


# ------------------------------------------------------------------- preflight

# The smallest thing that still proves a (provider, model) pair answers. It has
# to be a real completion: `models.list()` succeeds for keys that cannot call the
# model, and a 404 on the model is exactly the failure being looked for.
PREFLIGHT_SYSTEM = "Reply with the single character: 1"
PREFLIGHT_USER = "1"
PREFLIGHT_PARAMS = {"max_tokens": 1, "temperature": 0.0}


def preflight_params(params=None):
    """What one preflight call sends: the role's own parameters under a cheap floor.

    The role's parameters first, `PREFLIGHT_PARAMS` last, so `max_tokens=1` and
    `temperature=0` always win and a preflight cannot become expensive because a
    role was configured with a large budget. Everything else the role would send
    survives -- which is the point: `reasoning_format` is a provider extension
    that a provider is entitled to reject with a 400, and a preflight that omitted
    it would validate a call shape the run never makes.
    """
    sent = dict(params or {})
    sent.update(PREFLIGHT_PARAMS)
    return sent


def preflight_pair(provider, model, api_key, call=None, params=None):
    """Validate one ``(provider, model)`` pair with one cheap call.

    Returns ``(ok, status, detail)``. Any exception is a failure: this runs
    before anything has been spent, so the useful bias is to refuse.

    ``call`` takes ``(provider, api_key, model, system, user, params)`` -- the
    model explicitly, not a role, because a preflight validates a *pair* and an
    offline stand-in has to be able to check the exact slug that would go on the
    wire; and the resolved parameters explicitly, because the pair is only half of
    what a call can be rejected for. `params` is the merged dict, and the default
    implementation splits it into keyword arguments and `extra_body` exactly as
    `call_model_detailed` does, so what is validated is the call shape the run
    will actually send.
    """
    sent = preflight_params(params)
    if call is None:
        def call(provider_name, key, slug, system, user, sent_params):
            from openai import OpenAI

            # `max_retries=0` unconditionally, not under measurement mode: a
            # preflight runs *before* `set_measurement_mode(True)` -- see
            # `eval/calibrate.py:1122` against `:1139` -- so a conditional here
            # would never fire on the path that pays for it. Nothing is lost: no
            # caller outside `eval/` reaches this, the interactive app never runs
            # a preflight, and this function's whole documented posture is that
            # "any exception is a failure ... the useful bias is to refuse".
            # Three silent tries is the opposite of that bias, and the timed-out
            # preflight that refused run 1 on 2026-09-02 cost up to 3 of the day's
            # 20 Gemini requests to say so once.
            client = OpenAI(api_key=key,
                            base_url=PROVIDERS[provider_name]["base_url"],
                            timeout=30, max_retries=0)
            keywords, extra = split_params(sent_params)
            if extra:
                keywords["extra_body"] = extra
            resp = client.chat.completions.create(
                model=slug,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                **keywords)
            return (resp.choices[0].message.content or "")
    try:
        call(provider, api_key, model, PREFLIGHT_SYSTEM, PREFLIGHT_USER, sent)
    except Exception as exc:
        return False, _status_of(exc), _redact(exc, {provider: api_key})
    return True, None, ""


def preflight(keys, roles=None, call=None):
    """Validate every configured pair before a run spends anything.

    One call per distinct pair, not per role. Returns a list of records --
    ``role_names``, ``provider``, ``model``, ``ok``, ``status``, ``detail`` -- in
    the same order as `resolved_pairs`. The caller refuses on any ``ok`` false,
    exactly as it refuses on a `--verify-lock` mismatch: two calls turn a silent
    multi-hour loss into a one-second failure, and the loss this actually
    prevents already happened once, when Groq's slug retired under a frozen grid.

    A missing key is a failure here rather than a skip. A run that cannot call a
    role is not a run, and discovering that at task 1 of 144 is the whole point.

    Each pair is validated with the parameters its own roles will send. Where two
    roles share a pair -- `planner` and `test_writer` today -- their parameter sets
    are merged in role order, which is exact whenever they agree and is why a
    disagreement would show up here as a 400 rather than at task 1.
    """
    records = []
    for provider, model, role_names in resolved_pairs(roles):
        key = (keys or {}).get(provider)
        if not key:
            records.append({"roles": role_names, "provider": provider,
                            "model": model, "ok": False, "status": None,
                            "detail": "no API key for provider %r" % provider})
            continue
        params = {}
        for role in role_names:
            params.update(sampling_for(role))
        ok, status, detail = preflight_pair(provider, model, key, call=call,
                                            params=params)
        records.append({"roles": role_names, "provider": provider,
                        "model": model, "ok": ok, "status": status,
                        "detail": detail})
    return records


def preflight_failures(records):
    """The refusal lines for a preflight result. Empty when every pair answered.

    Each line names the role, the provider and the model, because "404" without
    those three is the message that cost this project a grid.
    """
    lines = []
    for record in records:
        if record["ok"]:
            continue
        lines.append(
            "%s -> %s/%s does not answer%s: %s"
            % ("+".join(record["roles"]), record["provider"], record["model"],
               "" if record["status"] is None else " (HTTP %s)" % record["status"],
               record["detail"]))
    return lines


def list_models(provider, api_key):
    """Model IDs the key can reach at ``provider``, sorted.

    "What can this key actually reach" should be a command, not a script written
    into /tmp when a slug retires. `eval/models.py --list <provider>` is that
    command; this is what it calls.

    ``max_retries=0`` for the same reason as `preflight_pair`'s client: this is a
    diagnostic, it is reached only from `eval/models.py`, and a diagnostic that
    silently makes three requests to answer "can this key reach anything" is
    spending a quota to hide the answer.
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=PROVIDERS[provider]["base_url"],
                    timeout=30, max_retries=0)
    return sorted(getattr(item, "id", str(item))
                  for item in client.models.list())


# ------------------------------------------------------ retry policy and pacing

# Which HTTP statuses are worth another attempt. 429 means "you asked too fast"
# and 5xx means the provider is unwell: both are transient by definition and the
# same request will plausibly succeed later. Everything else is a statement about
# the request itself, and repeating it just spends the budget more slowly.
#
# 401/403/404 are listed explicitly rather than left to fall through the default,
# because the case that matters is a wrong model slug: one 404 must end the task
# in one call, not after five sleeps. That is what makes the bad-slug scenario
# loud instead of slow, and it holds in both modes.
RETRY_STATUS = 429
RETRY_STATUS_FLOOR = 500
HARD_FAIL_STATUSES = (400, 401, 403, 404, 422)

# One initial attempt plus (MAX_PROVIDER_ATTEMPTS - 1) retries.
MAX_PROVIDER_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 30.0
BACKOFF_JITTER = 0.25


def is_retryable_status(status):
    """True for 429 and 5xx. False for everything else, including ``None``.

    ``None`` -- a failure we could not classify -- deliberately does not retry.
    That costs us the genuinely transient unclassified case (a reset connection
    carries no status), and the alternative costs more: `call_model` wraps *any*
    exception, so a `KeyError` in our own code would become five `KeyError`s and
    forty seconds of sleep, reported as a rate-limit loss. A bug in this module
    must not be able to present itself as infrastructure.
    """
    if status is None:
        return False
    if status == RETRY_STATUS:
        return True
    return status >= RETRY_STATUS_FLOOR


# A 429 says "wait"; it does not say "wait until tomorrow". Google's body does,
# in a `QuotaFailure` violation, and that string is the only place the window
# appears -- the status is 429 either way. Parsed out of the message text rather
# than off a typed SDK object for the same reason `_status_of` is: this module and
# both offline suites have to load without the SDK installed.
DAILY_QUOTA_MARK = "perday"
_QUOTA_ID = re.compile(r"""['"]?quotaId['"]?\s*[:=]\s*['"]([^'"]+)['"]""")
_RETRY_DELAY = re.compile(
    r"""['"]?retryDelay['"]?\s*[:=]\s*['"]?(\d+(?:\.\d+)?)\s*s""")


def quota_id_of(message):
    """The provider's own ``quotaId`` out of a 429 body, or ``""``."""
    found = _QUOTA_ID.search(message or "")
    return found.group(1) if found else ""


def retry_delay_of(message):
    """The body's ``retryDelay`` in seconds, or None. Not the header.

    `_retry_after_of` reads the header and clamps it to what we are willing to
    sleep. This one is unclamped on purpose: it is being used to decide whether
    the wait is longer than the whole retry budget, and clamping it first would
    hide exactly the case it is asked about.
    """
    found = _RETRY_DELAY.search(message or "")
    if not found:
        return None
    try:
        return float(found.group(1))
    except (TypeError, ValueError):
        return None


def daily_quota_violation(message, remaining_seconds=None):
    """``(quota_id, why)`` when a 429 body says a *per-day* quota is spent.

    ``("", "")`` otherwise, which includes every per-minute limit -- that layer
    works, the run should sleep and continue, and nothing here changes it.

    Two rules, in order:

    1. The `quotaId` names the window. `...PerDayPerProjectPerModel...` is a day.
       If a `quotaId` is present and does **not** say per-day, it is believed: the
       provider named its own window and a per-minute limit must keep retrying.
    2. With no `quotaId` at all, fall back to `RESOURCE_EXHAUSTED` plus a
       `retryDelay` longer than the backoff budget that is left. A wait we cannot
       outlast is not a wait, whatever the counter behind it is called.
    """
    text = message or ""
    quota_id = quota_id_of(text)
    if quota_id:
        if DAILY_QUOTA_MARK in quota_id.replace("-", "").replace("_", "").lower():
            return quota_id, "quotaId names a per-day quota"
        return "", ""
    if "RESOURCE_EXHAUSTED" not in text:
        return "", ""
    delay = retry_delay_of(text)
    if delay is None or remaining_seconds is None or delay <= remaining_seconds:
        return "", ""
    return "", ("RESOURCE_EXHAUSTED and a retryDelay of %gs, longer than the "
                "%.1fs of retry budget left" % (delay, remaining_seconds))


def backoff_schedule(run_id, attempts=MAX_PROVIDER_ATTEMPTS):
    """The sleep before each retry, in seconds, derived from the run id.

    Exponential, capped, with jitter -- and the jitter comes from a generator
    seeded on the run id, not from `random`'s global state. Unseeded jitter makes
    the sleep schedule unreproducible, and a run whose own waits cannot be
    reconstructed from its record is not a reproducible run: the wall-clock
    numbers are then unexplainable after the fact.
    """
    rng = random.Random(hashlib.sha256(
        ("backoff|%s" % run_id).encode("utf-8")).hexdigest())
    delays = []
    for attempt in range(max(0, int(attempts) - 1)):
        base = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))
        delays.append(round(base * (1.0 + BACKOFF_JITTER * (2.0 * rng.random() - 1.0)), 3))
    return delays


# The run id the backoff jitter is seeded from. Process-wide because `call_role`
# is called from a dozen places that have no business threading a run id through;
# the runner sets it once per run. The default is a literal so an unseeded process
# still has a *reproducible* schedule rather than a random one -- silently falling
# back to entropy is how a run ends up with waits nobody can reconstruct.
_BACKOFF_RUN_ID = "unseeded"


def set_backoff_run_id(run_id):
    """Seed the backoff jitter for this run. Returns the previous value."""
    global _BACKOFF_RUN_ID
    previous = _BACKOFF_RUN_ID
    _BACKOFF_RUN_ID = str(run_id or "unseeded")
    return previous


def backoff_run_id():
    return _BACKOFF_RUN_ID


# How many attempts `_attempt_provider` makes, as installed for this process.
# `MAX_PROVIDER_ATTEMPTS` above is the default; this is what is in force.
#
# The knob exists because there are two retry layers in this repository and only
# one of them may be active at a time. `eval/run_eval.py` wraps `call_model` in
# `Instrument`, whose own loop retries a 429 up to `MAX_429_RETRIES` times and
# honours `Retry-After` through `RateGovernor`. Leaving both bounds at their
# defaults multiplies them: 5 attempts here x 5 there is 25 requests to a
# provider that just said "slow down", which is how a rate limit becomes a ban.
# So the eval installs 1 here and keeps its own; the interactive app installs
# neither and uses this one. Whichever is in force is recorded in the manifest.
_RETRY_ATTEMPTS = MAX_PROVIDER_ATTEMPTS


def set_retry_attempts(attempts):
    """Install the attempt bound for this process. Returns the previous one.

    Floored at 1: a bound of 0 would mean "never call the provider", which is not
    a retry policy but a silently empty run.
    """
    global _RETRY_ATTEMPTS
    previous = _RETRY_ATTEMPTS
    _RETRY_ATTEMPTS = max(1, int(attempts))
    return previous


def retry_attempts():
    return _RETRY_ATTEMPTS


# The sleep the retry loop performs, injectable for the same reason `Pacer` takes
# one. A check that a 429 retries to its bound has to see the waits, and seeing
# them by performing them would put half a minute of real sleep into an offline
# suite -- 2 + 4 + 8 + 16 at the default bound. Nothing in production replaces
# this; it is `time.sleep` unless a check says otherwise, and `retry_snapshot`
# reports whether it is still the real one so a run cannot quietly claim waits it
# never took.
_RETRY_SLEEP = time.sleep


def set_retry_sleep(sleep):
    """Install the retry loop's sleep. Returns the previous one."""
    global _RETRY_SLEEP
    previous = _RETRY_SLEEP
    _RETRY_SLEEP = sleep or time.sleep
    return previous


def retry_snapshot():
    """The retry policy a run actually ran under, for the results manifest.

    A number in a report that cannot be traced to the constants that produced it
    is not reproducible. This is those constants, as installed, at run time.
    """
    return {"attempts": _RETRY_ATTEMPTS,
            "default_attempts": MAX_PROVIDER_ATTEMPTS,
            "retry_status": RETRY_STATUS,
            "retry_status_floor": RETRY_STATUS_FLOOR,
            "hard_fail_statuses": list(HARD_FAIL_STATUSES),
            "backoff_base_seconds": BACKOFF_BASE_SECONDS,
            "backoff_cap_seconds": BACKOFF_CAP_SECONDS,
            "backoff_jitter": BACKOFF_JITTER,
            "max_retry_after_seconds": MAX_RETRY_AFTER_SECONDS,
            "backoff_run_id": _BACKOFF_RUN_ID,
            "real_sleep": _RETRY_SLEEP is time.sleep,
            "schedule_seconds": backoff_schedule(_BACKOFF_RUN_ID,
                                                 _RETRY_ATTEMPTS),
            # The layer below this one. Every constant above describes retries
            # *this module* performs; the openai SDK performs its own beneath them,
            # and until it was pinned it was invisible to `calls`, to the event log
            # and to the manifest's `spent` alike -- so a run could make three
            # times the HTTP requests it reported and no field would show it.
            # `{"max_retries": 0}` is the measured configuration, one attempt per
            # request. `{}` means nothing is passed and openai 2.48.0's own default
            # of 2 retries applies, which is deliberate off the measured path: the
            # interactive app keeps its retry coverage.
            "sdk_retry_kwargs": _sdk_retry_kwargs(),
            "sdk_default_max_retries": _sdk_default_max_retries()}


# Minimum seconds between two calls to the same provider, enforced client-side
# before the call goes out. A 429 costs a round trip and a sleep and pollutes the
# wall-clock numbers, so the interesting place to prevent one is here -- retries
# are the backstop for the 429s pacing fails to prevent, not the mechanism.
#
# HONESTY ABOUT THESE NUMBERS: both are conservative guesses, not documented
# limits. Nothing in this repository records a provider's published free-tier RPM
# and this sandbox has no egress to go and read one, so neither value has been
# verified against a provider document. They are chosen low on purpose: pacing
# too slowly costs wall-clock time, pacing too fast costs arm-correlated data
# loss, and only one of those is recoverable. Raise them only against a quoted
# published limit, and re-record them in the manifest when you do.
MIN_CALL_INTERVAL_SECONDS = {
    # 6.0s = 10 calls/minute. The lowest free-tier RPM I have seen quoted for a
    # Gemini Flash model is 10; this sits at that floor rather than above it.
    # Gemini carries the Planner *and* the Test Writer, so it is the scarce one.
    "gemini": 6.0,
    # 2.4s = 25 calls/minute. Groq's free tier is quoted per model and is
    # generally looser than Gemini's; 25 is well under anything I have seen.
    # Groq carries the Executor, which is where arm B's call volume actually is.
    "groq": 2.4,
}

# A provider absent from the table above still gets paced. Defaulting to "no
# limit" would mean adding a provider silently disables pacing for it.
DEFAULT_MIN_CALL_INTERVAL_SECONDS = 6.0


class Pacer(object):
    """Per-provider minimum interval, measured on the monotonic clock.

    `time.monotonic` and not `time.time`: a wall-clock step -- NTP, a laptop
    waking from sleep -- can make the last call look like it happened in the
    future and park the run for hours, or like it happened long ago and release a
    burst straight into a rate limit.

    `wait_for` returns the seconds it slept, so the caller can add them to the
    task's wall clock. A benchmark that quietly excludes its own pacing sleep is
    reporting a throughput nobody can obtain.
    """

    def __init__(self, intervals=None, default=None, sleep=time.sleep,
                 clock=time.monotonic):
        self.intervals = dict(MIN_CALL_INTERVAL_SECONDS if intervals is None
                              else intervals)
        self.default = (DEFAULT_MIN_CALL_INTERVAL_SECONDS if default is None
                        else float(default))
        self._sleep = sleep
        self._clock = clock
        self.last = {}
        self.slept = {}

    @classmethod
    def disabled(cls, **kwargs):
        """A pacer that never sleeps, and says so in its snapshot."""
        return cls(intervals={}, default=0.0, **kwargs)

    @property
    def enabled(self):
        return self.default > 0 or any(value > 0 for value in self.intervals.values())

    def interval_for(self, provider):
        return self.intervals.get(provider, self.default)

    def wait_for(self, provider):
        """Sleep until this provider may be called again. Returns seconds slept."""
        interval = self.interval_for(provider)
        now = self._clock()
        previous = self.last.get(provider)
        slept = 0.0
        if previous is not None and interval > 0:
            remaining = interval - (now - previous)
            if remaining > 0:
                self._sleep(remaining)
                slept = remaining
                now = self._clock()
        self.last[provider] = now
        self.slept[provider] = self.slept.get(provider, 0.0) + slept
        return slept

    def snapshot(self):
        """The pacing a run actually ran under, for the results manifest."""
        return {"enabled": self.enabled,
                "min_call_interval_seconds": dict(self.intervals),
                "default_min_call_interval_seconds": self.default,
                "slept_seconds": dict((name, round(value, 2))
                                      for name, value in self.slept.items())}


# The process-wide pacer `call_role` consults. Module level so a UI session and
# an eval sweep pace against the same last-call times: two independent pacers
# would each believe itself within budget and together be over it.
#
# It starts DISABLED, like `MEASUREMENT_MODE`, and a runner turns it on. Two
# reasons, and the first is the load-bearing one:
#
#   1. `eval/run_eval.py` already paces, in `RateGovernor`, and `Instrument`
#      replaces `call_model` -- which lives *inside* the paced region here. An
#      enabled-by-default pacer would sleep once here and again in the governor,
#      double-charging every call and inflating exactly the wall-clock numbers the
#      sprint is trying to make honest.
#   2. Pacing is a property of a run, and a run has to be able to state the pacing
#      it ran under (`snapshot()` goes into the manifest). A module default nobody
#      declared is not stateable, and it silently costs any importer real seconds.
PACER = Pacer.disabled()


def set_pacing(on=True, intervals=None, default=None, sleep=time.sleep,
               clock=time.monotonic):
    """Install (or remove) client-side pacing process-wide.

    Returns the pacer that was previously installed, so a caller can restore it.
    """
    global PACER
    previous = PACER
    if on:
        PACER = Pacer(intervals=intervals, default=default, sleep=sleep,
                      clock=clock)
    else:
        PACER = Pacer.disabled(sleep=sleep, clock=clock)
    return previous


def set_pacer(pacer):
    """Install a specific pacer. Returns the previous one."""
    global PACER
    previous = PACER
    PACER = pacer
    return previous


class RoleCall(tuple):
    """``(text, provider_used)`` -- plus ``.record``, the call's provenance.

    A plain 2-tuple to every existing caller, so adding provenance did not
    change the shape of `call_role`'s contract or of the checks that pin it.

    (No ``__slots__``: a non-empty one is rejected on a variable-length builtin
    base, and an empty one would leave nowhere to hang the record.)
    """

    def __new__(cls, text, provider, record):
        self = tuple.__new__(cls, (text, provider))
        self.record = record
        return self


MIN_REDACTABLE_KEY = 4


def _redact(text, keys):
    """Never let a key value reach a log line, even inside an SDK error.

    Two limits, stated rather than hidden. The length floor exists so a stub key
    like `"k"` does not turn every `k` in a traceback into `<redacted>`; it is
    set low (4) because a short real key leaking is far worse than an
    over-redacted test message. And this is exact-substring matching, so an SDK
    that prints a key *elided* (`AIzaSy...9Qk`) defeats it -- the visible prefix
    is also scrubbed for that reason, but a middle-elided rendering with a short
    prefix can still get through. The load-bearing guarantee is `_child_env` and
    "keys are never put in a prompt"; this is the second layer, not the first.
    """
    out = str(text)
    for value in (keys or {}).values():
        value = str(value or "")
        if len(value) < MIN_REDACTABLE_KEY:
            continue
        out = out.replace(value, "<redacted>")
        if len(value) >= 12:
            # Truncated renderings: scrub the prefix an SDK would show.
            out = out.replace(value[:8], "<redacted>")
    return out


# Public name for the same thing. Every write site is supposed to redact for
# itself rather than trust a distant upstream one, and a write site in another
# module cannot be asked to do that while the only spelling is private.
redact = _redact


# --------------------------------------------------------------- event log (D3)

# Closed and enumerable on purpose. An open-ended `kind` string means a reader
# has to discover the vocabulary from the data, and a typo becomes a silently
# new event type that no analysis counts.
EVENT_CALL = "call"
EVENT_STEP = "step"
EVENT_CANDIDATE = "candidate"
EVENT_GATE = "gate"
EVENT_VERDICT = "verdict"
EVENT_INFRA_LOSS = "infra_loss"
EVENT_RESUME_SKIP = "resume_skip"
# One per HTTP request, where `call` is one per *provider attempt*. The two are
# not the same number and were being read as if they were: the 2026-09-02 store
# holds 227 wrapper attempts against 152 `call` events, because every retry
# `eval/run_eval.Instrument` makes happens inside one `_attempt_provider` call and
# therefore inside one `call` event.
#
# A separate kind rather than a field on `call`, so that no count computed from
# this vocabulary changes meaning: `kind == "call"` still means what it meant in
# every existing reader and in every file already on disk. It is also the marker
# that tells the two regimes apart in a file that spans the change -- a task whose
# events include `http_attempt` rows has complete HTTP accounting, and a task
# whose events do not is from before this existed and undercounts.
EVENT_HTTP_ATTEMPT = "http_attempt"

EVENT_KINDS = (EVENT_CALL, EVENT_STEP, EVENT_CANDIDATE, EVENT_GATE,
               EVENT_VERDICT, EVENT_INFRA_LOSS, EVENT_RESUME_SKIP,
               EVENT_HTTP_ATTEMPT)

EVENT_LOG_SUFFIX = ".events.jsonl"


def event_log_path(memory_path):
    """`runs/<run_id>.json` -> `runs/<run_id>.events.jsonl`.

    The suffix is `.jsonl`, and that is not cosmetic. Three existing checks glob
    `*.json` in the run directory and assert on the number of matches -- "the run
    wrote exactly one file", "two run files on disk" -- and one indexes
    `sorted(glob(...))[0]`. A sibling named `*.json` would break all four for no
    reason. Nor may it end in `.tmp`: "no leftover temp files" globs that too.
    """
    root = memory_path
    if root.endswith(".json"):
        root = root[:-len(".json")]
    return root + EVENT_LOG_SUFFIX


def _redact_deep(value, keys):
    """`_redact` over a whole nested structure, at the write site.

    The JSONL writer redacts everything it is handed rather than trusting the
    caller to have done it. A second writer relying on a distant first writer's
    hygiene is exactly how an unredacted `ProviderError` reached a log before:
    the guarantee has to live where the bytes are produced, because that is the
    only place that cannot be bypassed by a new caller.
    """
    if isinstance(value, str):
        return _redact(value, keys)
    if isinstance(value, dict):
        return dict((name, _redact_deep(item, keys))
                    for name, item in value.items())
    if isinstance(value, (list, tuple)):
        return [_redact_deep(item, keys) for item in value]
    return value


class EventLog:
    """Append-only JSONL, one JSON object per line, flushed after every event.

    Additive by construction: it is a *sibling* of the per-run JSON, never a
    replacement. The UI and every stored record read that JSON's shape, so this
    file adds a chronology without touching it.

    Flushed (and fsynced) per event because the whole point is the run that dies
    mid-grid. A buffered log of a crashed run is a log of everything except the
    part you needed.
    """

    def __init__(self, path, context=None, keys=None):
        self.path = path
        self.context = dict(context or {})
        self.keys = dict(keys or {})
        self.count = 0
        self.failed = 0

    def scope(self, **context):
        """Temporarily merge extra context (one cell of a sweep) into every event.

        A context manager rather than a second EventLog object, so the file, the
        event count and the redaction keys stay single. Two objects writing one
        file would each report a partial count.
        """
        return _EventScope(self, context)

    def emit(self, kind, **fields):
        """Append one event. Returns the dict written, or None if the write failed.

        Unknown kinds raise: the vocabulary is closed, and a typo that silently
        invents an event type is a hole in every count computed from this file.
        """
        if kind not in EVENT_KINDS:
            raise ValueError("unknown event kind %r; the vocabulary is %s"
                             % (kind, ", ".join(EVENT_KINDS)))
        event = {"kind": kind,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        event.update(self.context)
        event.update(fields)
        event = _redact_deep(event, self.keys)
        line = json.dumps(event, sort_keys=True, default=str)
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # Same policy as `Memory.save`: a read-only filesystem must not take
            # the run down. Counted, so a silent zero is visible as failures.
            self.failed += 1
            return None
        self.count += 1
        return event


class _EventScope(object):
    def __init__(self, log, context):
        self.log = log
        self.context = context
        self.previous = None

    def __enter__(self):
        self.previous = dict(self.log.context)
        self.log.context.update(self.context)
        return self.log

    def __exit__(self, *exc_info):
        self.log.context = self.previous
        return False


# The event log `_log_call` forwards to, or None. Module level because `call_role`
# is reached from call sites that have no business threading a log through;
# `set_event_log` returns the previous value so a caller restores rather than
# clears, and a nested runner cannot orphan its parent's log.
_EVENT_LOG = None


def set_event_log(log):
    """Install the process-wide event log. Returns the previous one."""
    global _EVENT_LOG
    previous = _EVENT_LOG
    _EVENT_LOG = log
    return previous


def event_log():
    return _EVENT_LOG


def emit_event(kind, **fields):
    """Write an event if a log is installed. A no-op otherwise."""
    log = _EVENT_LOG
    if log is None:
        return None
    return log.emit(kind, **fields)


def _log_call(record):
    CALL_LOG.append(record)
    if len(CALL_LOG) > MAX_CALL_LOG:
        del CALL_LOG[:len(CALL_LOG) - MAX_CALL_LOG]
    return record


def _emit_call_event(record):
    """The `call` event *is* the CALL_LOG record, so the two cannot drift.

    Emitted after the outcome is settled rather than when the record is created.
    `_log_call` runs before the provider is touched -- so the record it appends
    still says `ok: False` -- and an event log whose every `call` event reads as a
    failure would be worse than no event log.
    """
    return emit_event(EVENT_CALL, **record)


def call_role(role, keys, system, user, emit=None, prefer=None):
    """Call a role's provider. Returns ``(text, provider_used)``.

    Outside measurement mode this falls back to the other providers that have
    keys -- convenient in a UI, where any answer beats a stack trace. It also
    replaces the four copy-pasted try/except handoff blocks the pipeline used to
    carry.

    ``prefer`` puts one provider at the front of the order. The escalation
    ladder uses it to retry a stuck step on a different model.

    Under ``MEASUREMENT_MODE`` the order is exactly one provider -- ``prefer``
    when it is configured, otherwise the role's own -- and a failure raises.
    ``prefer`` still works, because the ladder's `alternate` rung is a
    deliberate, logged, recorded switch that is part of arm B's definition; it
    is only the *silent* substitution that is fatal to a measurement. Every
    attempt is logged with the provider requested and the provider used, so a
    run in which those two ever differ here is a bug in this function and not a
    finding about models.
    """
    primary = ROLE_PROVIDER[role]
    requested = prefer if prefer in PROVIDERS else primary
    if MEASUREMENT_MODE:
        order = [requested]
    else:
        order = [primary] + [name for name in PROVIDERS if name != primary]
        if prefer in PROVIDERS:
            order = [prefer] + [name for name in order if name != prefer]
    attempted, last_error = [], None

    for provider in order:
        key = (keys or {}).get(provider)
        if not key:
            continue
        attempted.append(provider)
        text, record, error = _attempt_provider(role, requested, provider, key,
                                                system, user, keys)
        if error is None:
            return RoleCall(text, provider, record)
        last_error = error
        if emit and not MEASUREMENT_MODE:
            emit("system", "%s failed for %s; trying next provider" % (provider, role))

    if MEASUREMENT_MODE:
        # Loud on purpose. A dead slug or a missing key must stop the task, not
        # reroute the grid to whichever provider happens to still answer.
        if not attempted:
            raise ProviderError(
                "measurement mode: no API key for %s (%s); failover is disabled"
                % (role, requested))
        # The status and the class are carried onto the wrapper. Flattening them
        # away here is what made a 429 and a 404 indistinguishable to the caller
        # deciding whether this was infrastructure or a model.
        raise ProviderError(
            "measurement mode: %s failed for %s and failover is disabled: %s"
            % (requested, role, _redact(last_error, keys)),
            status=getattr(last_error, "status", None),
            exc_class=getattr(last_error, "exc_class", ""),
            retry_after=getattr(last_error, "retry_after", None))
    if not attempted:
        raise ProviderError("no API key available for %s" % role)
    raise ProviderError("all providers failed for %s: %s"
                       % (role, _redact(last_error, keys)),
                       status=getattr(last_error, "status", None),
                       exc_class=getattr(last_error, "exc_class", ""),
                       retry_after=getattr(last_error, "retry_after", None))


def _attempt_provider(role, requested, provider, key, system, user, keys):
    """One provider, paced and retried. Returns ``(text, record, error)``.

    Retries are per provider and happen before failover moves on, because in
    measurement mode there is no failover: the order is one provider long, so a
    retry here is the only thing standing between a 429 and a lost task.

    `error` is None exactly when the call succeeded. The record is appended to
    CALL_LOG once, before the first attempt, and mutated in place -- the retry
    count belongs on the call, not as three near-identical log entries.
    """
    record = {
        "role": role,
        "requested": requested,
        "used": provider,
        # Resolved, not read off the provider: a role can carry its own model, and
        # the record has to say which one this call actually asked for. Resolved
        # against the provider being attempted, so a failover or an `alternate`
        # rung records the model that endpoint was actually asked for.
        "model": model_for(role, provider) if role in ROLE_PROVIDER
                 else PROVIDERS[provider].get("model"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "measurement_mode": MEASUREMENT_MODE,
        "ok": False,
        "error": None,
        "status": None,
        "exc_class": "",
        # Why generation stopped, as the provider said. `None` until a real
        # completion is observed, and `None` for a stubbed call, because the only
        # honest value for "nothing answered" is not "stop". A `length` here is
        # the difference between a model that answered wrongly and a model that
        # was cut off mid-answer, and without it the second is graded as the
        # first: 2 of the 57 draws in `eval/results/pin-replay-1/ledger.jsonl`
        # returned 0 characters at the endpoint's own 2048-token default, which is
        # the whole of D-13 caveat 4. Both were the runner-up `gpt-oss-20b`, not
        # the pinned Executor -- so what this records is a known behaviour of the
        # endpoint, not a known defect in the model the sweep will use.
        "finish_reason": None,
        # What answered, as against `model` above, which is what was asked for.
        # Declared here for the same reason `finish_reason` is: this record is
        # appended to CALL_LOG before the provider is touched and emitted on the
        # failure path too, so a key that only appeared on success would make
        # "absent" a third state that readers have to guess at. `None` means no
        # real completion was observed -- a call that failed, or a stand-in that
        # answered without one -- and never "the same as requested".
        "model_returned": None,
        "attempts": 0,
        "retried": 0,
        "seconds_paced": 0.0,
        "seconds_backoff": 0.0,
        # HTTP requests actually made for this provider attempt, and where their
        # seconds went. `attempts` above counts *this* loop, which under
        # `set_retry_attempts(1)` is always 1 while the layer below may have gone
        # to the wire five times; these three are that layer's own count, arriving
        # through `note_waits`. 0 means nothing below reported -- a stand-in, or
        # the interactive app, which does its own retrying in this loop -- and
        # never "one request was made".
        #
        # `seconds_http_max` is the field the 2026-09-02 sweep did not have and
        # needed: a draw that took 8467.2s at `calls: 2` is either one request
        # that stayed open for hours or two fast requests either side of a
        # `Retry-After` this project honoured without a cap, the remedies are
        # different, and nothing in the store distinguishes them.
        "http_attempts": 0,
        "seconds_http": 0.0,
        "seconds_http_max": 0.0,
    }
    # Every sampling parameter actually sent, by its own name, so `temperature`
    # and `top_p` keep the keys every existing reader uses and a provider-specific
    # parameter appears here too instead of going unrecorded.
    record.update(sampling_for(role))
    _log_call(record)
    attempts = retry_attempts()
    delays = backoff_schedule(_BACKOFF_RUN_ID, attempts)
    error = None
    for attempt in range(attempts):
        record["attempts"] = attempt + 1
        record["seconds_paced"] = round(
            record["seconds_paced"] + PACER.wait_for(provider), 3)
        try:
            # STANDING PROHIBITION, and this is the line someone would wrap.
            #
            # Do not add a disk or memory cache keyed on
            # (prompt, model, temperature, top_p, provider). It looks like a free
            # speedup and it silently destroys the measurement. The D8 calibration
            # sweep draws 10 independent A' samples per task; with a cache like
            # that, 36 tasks become 36 unique answers replayed ten times at zero
            # variance. k=3 becomes k=1. Every confidence interval collapses, the
            # repeats look perfectly reproducible, and nothing in the output says
            # a sample was replayed rather than drawn.
            #
            # If one is ever genuinely needed it needs (a) an attempt nonce in the
            # key, so a repeat is a miss by construction, and (b) an unconditional
            # bypass whenever MEASUREMENT_MODE is on. Prefer a content-addressed
            # artifact store: that gives resume without ever handing back an old
            # sample as if it had just been drawn.
            #
            # Cleared immediately before the call, never after: what is read below
            # is then this attempt's completion or nothing at all, and a stub that
            # sets nothing cannot inherit the previous attempt's `finish_reason`.
            note_completion(None)
            # Same rule, same reason, for the waiting the layer below did. Cleared
            # here and folded in on both exits, so an attempt that reports nothing
            # cannot inherit the previous attempt's seconds.
            note_waits(None)
            text = call_model(provider, key, system, user, role=role)
        except ProviderError as exc:
            _fold_waits(record)
            error = exc
            record["error"] = _redact(exc, keys)
            record["status"] = exc.status
            record["exc_class"] = exc.exc_class
            # The body is the only place the quota's name appears and it was
            # being dropped, so the last exhaustion had to be diagnosed from a
            # raw error string pasted into a report. Read off the *redacted*
            # message, so this cannot become a second path a key travels on.
            quota_id = quota_id_of(record["error"])
            if quota_id:
                record["quota_id"] = quota_id
            if exc.status == RETRY_STATUS:
                # What is left to sleep if we did keep retrying: the schedule
                # from here to the last attempt, which never sleeps after itself.
                remaining = sum(delays[attempt:max(attempt, attempts - 1)])
                named, why = daily_quota_violation(record["error"], remaining)
                if why:
                    record["quota_daily"] = True
                    record["quota_id"] = named or quota_id
                    _emit_call_event(record)
                    raise DailyQuotaExhausted(
                        "per-day quota exhausted on %s/%s for %s%s: %s. Not "
                        "retried -- the counter resets on the provider's daily "
                        "boundary, not after a backoff, so every further attempt "
                        "spends a request against a wall. Re-run when it has "
                        "reset." % (provider, record["model"], role,
                                    " (quota %s)" % record["quota_id"]
                                    if record["quota_id"] else "", why),
                        quota_id=record["quota_id"], model=record["model"],
                        provider=provider, role=role,
                        retry_after=getattr(exc, "retry_after", None))
            if not is_retryable_status(exc.status) or attempt == attempts - 1:
                break
            # The server's own number wins when it sent one; ours is a guess and
            # its is not. Ours is the fallback, not the policy.
            wait = getattr(exc, "retry_after", None)
            if wait is None:
                wait = delays[attempt]
            record["retried"] = attempt + 1
            record["seconds_backoff"] = round(record["seconds_backoff"] + wait, 3)
            _RETRY_SLEEP(wait)
            continue
        record["ok"] = True
        record["error"] = None
        record["status"] = None
        record["exc_class"] = ""
        _fold_waits(record)
        # One read of the side channel, not two: `finish_reason` and the returned
        # slug are facts about the same completion, and two `last_completion()`
        # calls could in principle straddle a `note_completion` from elsewhere.
        detail = last_completion()
        record["finish_reason"] = detail.get("finish_reason")
        record["model_returned"] = detail.get("model_returned")
        _emit_call_event(record)
        returned = normalise_model_slug(record["model_returned"])
        # Absence is not a mismatch. `{}` from the side channel is a stubbed call
        # -- roughly two dozen stand-ins across the offline suites replace
        # `call_model` with something that returns a string and notes no
        # completion -- and a provider that sends no `model` field at all is a
        # provider that made no claim. Neither is evidence that a different model
        # answered, and treating a falsy value as a failed comparison is how an
        # empty container comes to answer for a measurement that never ran.
        if returned and returned != record["model"]:
            raise ModelMismatch(
                "%s answered %s as %r when this call asked for %r. Not retried "
                "and not scored as one cell's failure: a provider serving an "
                "alias will serve it again, and a run whose %s was not the "
                "registered model is not the run that was registered. Resolve "
                "the slug -- or register the substitution -- before drawing "
                "again. Draws already written claim %r."
                % (provider, role, record["model_returned"], record["model"],
                   role, record["model"]),
                requested=record["model"], returned=record["model_returned"],
                provider=provider, role=role)
        return text, record, None
    _emit_call_event(record)
    return None, record, error


def alternate_provider(role, keys):
    """A provider for ``role`` other than its usual one, if a key exists.

    Returns ``None`` when only the primary is configured -- the caller then
    knows the "try a different model" rung is unavailable rather than silently
    re-running the same one and calling it an escalation.
    """
    primary = ROLE_PROVIDER[role]
    for name in PROVIDERS:
        if name != primary and (keys or {}).get(name):
            return name
    return None


# The rules the acceptance suite must obey. Shared between the Planner prompt
# and the regeneration prompt so they cannot drift apart.
_TEST_RULES = (
    "Rules for the TESTS block:\n"
    "- Exactly one ```python fenced block.\n"
    "- The Executor's program is saved as `solution.py`, so import from it: "
    "`from solution import <names>`.\n"
    "- Plain module-level `assert` statements only. NO pytest and NO unittest "
    "-- they are not installed and must not be a dependency. No test "
    "functions, no test classes; the asserts run when the file runs.\n"
    "- Assert concrete computed VALUES, e.g. `assert median([1,2,3,4]) == 2.5`. "
    "Never write assertions that would pass against an unimplemented stub -- "
    "`assert callable(f)`, `assert f is not None` and `assert hasattr(...)` are "
    "rejected, because they prove nothing.\n"
    "- Coverage scales with the SPEC, not with a constant: at least one "
    "assertion per behaviour the SPEC states, plus the boundaries those "
    "behaviours imply -- empty input, one element, ties, zero, negatives, "
    "duplicates.\n"
    "- Error behaviour is counted separately and must be EXERCISED, not "
    "described. For every error the SPEC states, reach it:\n"
    "    try:\n"
    "        median([])\n"
    "        assert False, 'expected ValueError on empty input'\n"
    "    except ValueError:\n"
    "        pass\n"
    "  Name the exception in the `except`. A suite that reaches no stated error "
    "path is rejected and regenerated, because the SPEC's error rules are the "
    "half of the behaviour a passing run would otherwise never test.\n"
    "- Standard library only. No network, no file I/O, no subprocesses, no "
    "input(). Must finish within %d seconds.\n"
    % harness.EXEC_TIMEOUT_SECONDS
)

_INTERFACE_RULE = (
    "CRITICAL: the SPEC must name the exact public function names and their "
    "signatures that the TESTS block imports -- for example \"exposes "
    "median(values: list) -> float\". The Executor only sees the SPEC and the "
    "step, never the tests. If the SPEC does not pin the interface, the "
    "Executor and the tests will disagree on names and every run will fail on "
    "import.\n"
    "Annotate those signatures with 3.9-legal syntax or omit the annotations "
    "entirely -- the names are what must be pinned, and a 3.10-only annotation "
    "in the SPEC causes the same import failure this rule exists to prevent.\n"
)

# The runtime the child interpreter actually is. Stated because it was stated
# nowhere: the Planner wrote PEP 604 unions into specs, Executors copied them or
# invented them unprompted, and six of 57 pin-probe draws died at import on a
# language version rather than on the task. Shared by every prompt that asks for
# code, including the repair rungs, which reuse `PROMPTS["executor"]`.
#
# This is the instruction half of the fix and model compliance with it is
# unmeasured by construction; `harness._SOURCE_PROLOGUE` is the half that does
# not depend on a model obeying anything. Neither one covers `match` or a runtime
# `isinstance(x, int | str)`, which is why both clauses below are spelled out.
_RUNTIME_RULE = (
    "RUNTIME: the code runs on CPython 3.9. Anything newer is a syntax or "
    "runtime error, not a style choice.\n"
    "- No PEP 604 unions: `int | None` is 3.10+. It fails at `def` time as a "
    "TypeError in an annotation, and at call time in `isinstance(x, int | str)`. "
    "Use `typing.Optional[int]` / `typing.Union[int, str]`, or no annotation.\n"
    "- No `match`/`case` statements.\n"
    "- No 3.10+ standard library additions.\n"
    "- `list[int]`, `dict[str, int]` and `tuple[int, ...]` are fine (3.9, PEP 585).\n"
)

# Why the Planner is asked to name its assumptions: a vague prompt
# underdetermines the spec, so every gap the Planner closes is a decision the
# user never made -- units, tie-breaking, what counts as invalid input, whether
# an empty collection is an error or an identity. Those decisions were previously
# visible only as whatever the spec happened to say, which is the one place a
# reader is least likely to notice them.
#
# Deliberately prose and deliberately not machine-readable. Nothing downstream
# parses this section, nothing gates on it, and no assumption is checked against
# the code. Asking for a format would buy a schema to violate and would suggest a
# guarantee that does not exist; asking for sentences buys the one thing wanted,
# which is that a wrong turn is legible before the code is read.
_ASSUMPTIONS_RULE = (
    "Before the spec, name the decisions the prompt left open and you had to "
    "make anyway -- units, tie-breaking, empty or malformed input, output "
    "shape, anything you invented. Prose, one per line or one paragraph; no "
    "particular format. If the prompt really settled everything, write the "
    "literal line `ASSUMPTIONS: none` rather than inventing an assumption to "
    "fill the section.\n"
)

PROMPTS = {
    "planner": (
        "You are the Planner for a pipeline that EXECUTES the code it writes "
        "and grades it against tests you write now.\n"
        "The user gives a vague prompt. Rewrite it into a clear spec, break it "
        "into numbered steps, then write the acceptance tests.\n\n"
        + _ASSUMPTIONS_RULE + "\n"
        + _INTERFACE_RULE +
        "\nRequirements for the steps:\n"
        "- Each step must be deliverable as a single self-contained Python "
        "program that runs on its own.\n"
        "- No step may require network access, subprocesses, stdin, or "
        "third-party packages beyond the standard library.\n"
        "- Prefer few, substantial steps (2-4). A later step may restate "
        "earlier code; it must not import from it.\n"
        "- Every step's program must expose the full interface named in the "
        "SPEC, because the same acceptance suite is run against each step.\n\n"
        + _TEST_RULES +
        "\n" + _RUNTIME_RULE +
        "\nOutput format, with all four headers present:\n"
        "ASSUMPTIONS: <the decisions you had to make that the prompt did not, "
        "in prose -- or the literal line `ASSUMPTIONS: none`>\n"
        "SPEC: <one paragraph, naming the exact functions and signatures>\n"
        "STEPS:\n1. ...\n2. ...\n"
        "TESTS:\n```python\nfrom solution import ...\nassert ...\n```\n"
    ),
    "test_writer": (
        "You are the Test Writer. You are given a spec and, possibly, a "
        "rejected previous attempt at an acceptance suite. Produce a suite "
        "that genuinely verifies the spec.\n\n"
        + _TEST_RULES +
        "\n" + _RUNTIME_RULE +
        "\nOutput ONLY the ```python block. No prose, no headers.\n"
    ),
    "executor": (
        "You are the Executor. You are given a spec and one step.\n\n"
        "Your output MUST contain exactly one ```python fenced block holding "
        "the complete, self-contained program for this step. It is saved as "
        "solution.py and executed automatically, so:\n"
        "- It must run top-to-bottom on a bare interpreter.\n"
        "- Define every function named in the spec at module level, with the "
        "exact names and signatures given. A hidden acceptance suite imports "
        "them via `from solution import ...`.\n"
        "- Importing your file must not fail or block.\n"
        "- It must exit with status 0.\n"
        "- Do NOT write tests. A fixed acceptance suite grades you, and any "
        "test file you emit is discarded.\n\n"
        # Taken from the harness rather than restated, so the prompt cannot
        # promise a limit the sandbox does not enforce or omit one it does.
        + harness._SANDBOX_RULES + "\n\n"
        + _RUNTIME_RULE + "\n"
        "Do not ask questions. Do not explain at length. Produce the program."
    ),
}


UNION_FLAG = "pep604-union"
MATCH_FLAG = "match-statement"
RUNTIME_SYNTAX_KINDS = (UNION_FLAG, MATCH_FLAG)

# Base names on both sides of a `|` before it counts as a PEP 604 union. Prose
# contains bare pipes -- tables, alternatives, shell snippets -- and a set
# expression `a | b` is legal 3.9. Requiring a type name on each side is what
# separates `int | None` from both of those without needing to parse the spec.
_TYPE_NAMES = frozenset((
    "int", "float", "complex", "bool", "str", "bytes", "bytearray",
    "list", "tuple", "dict", "set", "frozenset", "range",
    "None", "NoneType", "object", "type", "callable",
    # typing spellings, which are 3.9-legal on their own but not either side of
    # a `|`: `Optional[int] | str` is still a 3.10 construct.
    "Any", "Optional", "Union", "List", "Tuple", "Dict", "Set", "FrozenSet",
    "Sequence", "Mapping", "MutableMapping", "Iterable", "Iterator",
    "Callable", "Literal",
))

# One level of subscript, so `list[int] | None` and `dict[str, int] | None` are
# seen. Nested past one level is not, and that is a stated limit rather than a
# claim of completeness -- the inner `int | float` of
# `dict[str, int | float | None]` is what this catches there, which is enough to
# flag the spec.
_ATOM = r"[A-Za-z_][\w.]*(?:\[[^\[\]]*\])?"
_UNION_PAIR = re.compile(r"(%s)[ \t]*\|[ \t]*(%s)" % (_ATOM, _ATOM))
_MATCH_LINE = re.compile(r"(?m)^[ \t`]*match[ \t]+(?P<subject>[^\n:]+?)[ \t]*:")
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def _base_name(atom):
    """`dict[str, int]` -> `dict`. The subscript is 3.9-legal; the base decides."""
    return atom.split("[", 1)[0].strip()


def _is_expression_like(text):
    """Whether `match X:`'s X reads as an expression rather than as prose.

    `match len(values):` is a statement. "match numbers to names:" is a
    sentence that happens to start with the soft keyword. The distinguishing
    property available without a parser is whitespace outside brackets.
    """
    if not text:
        return False
    if not (text[0].isalpha() or text[0] in "_(["):
        return False
    depth = 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char.isspace() and depth <= 0:
            return False
    return True


def scan_runtime_syntax(text):
    """3.10-only Python syntax found in ``text``. Report-only, never a gate.

    A pure function over a string: it costs nothing, has no side effects, and
    can be run over a ledger that was written months ago, which is the point.
    It never rejects a spec and never triggers regeneration -- rejection was
    considered as the fix for the PEP 604 artifact and deliberately not adopted,
    because a rejected spec is a re-planned spec and Planner calls are the
    binding constraint. `harness._SOURCE_PROLOGUE` is what actually removes the
    failure mode; this only measures how often the Planner needed it.

    Returns a list of ``{"kind", "snippet"}`` dicts, JSON-ready, empty when
    clean. `kind` is one of `RUNTIME_SYNTAX_KINDS`.

    Deliberately a heuristic over prose, not a parse: a spec is not a Python
    file. It answers "did the Planner write 3.10 syntax into the interface it is
    pinning", and it cannot see syntax an Executor invents on its own -- that is
    the majority case and only the harness prologue covers it.
    """
    text = text or ""
    found = []
    for match in _UNION_PAIR.finditer(text):
        left, right = match.group(1), match.group(2)
        if _base_name(left) in _TYPE_NAMES and _base_name(right) in _TYPE_NAMES:
            found.append({"kind": UNION_FLAG, "snippet": match.group(0).strip()})

    # Line-anchored, plus the backticked spans hoisted onto lines of their own so
    # an inline `match x:` inside a sentence is still seen.
    seen = set()
    hoisted = "\n".join(_BACKTICKED.findall(text))
    for candidate in (text, hoisted):
        for match in _MATCH_LINE.finditer(candidate):
            subject = match.group("subject")
            snippet = "match %s:" % subject.strip()
            if _is_expression_like(subject.strip()) and snippet not in seen:
                seen.add(snippet)
                found.append({"kind": MATCH_FLAG, "snippet": snippet})
    return found


def runtime_syntax_note(found):
    """One line for a human, or "" when the scan was clean."""
    if not found:
        return ""
    kinds = []
    for flag in found:
        if flag["kind"] not in kinds:
            kinds.append(flag["kind"])
    return "3.10-only syntax in the spec (%s): %s" % (
        ", ".join(kinds),
        "; ".join(flag["snippet"] for flag in found[:4]))


def clamp(text, limit):
    """Head+tail truncation with an explicit marker, so nothing grows without
    bound and the elision is visible to whoever reads it."""
    text = text or ""
    if len(text) <= limit:
        return text
    keep = max(limit - 60, 40)
    head = keep * 2 // 3
    tail = keep - head
    return "%s\n[... %d characters elided ...]\n%s" % (
        text[:head], len(text) - head - tail, text[-tail:])


class Memory:
    """One file per run. Never reads prior state.

    The old implementation loaded ``memory.json`` on construction and appended
    to it, so every run inherited the log of every previous run and the file
    grew forever. Runs are now isolated by construction.
    """

    def __init__(self, prompt="", mode=DEFAULT_MODE, dirpath="runs", run_id=None):
        self.run_id = run_id or "%s-%s" % (
            time.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])
        self.dirpath = dirpath
        self.path = os.path.join(dirpath, "%s.json" % self.run_id)
        self.data = {
            "run_id": self.run_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "prompt": prompt,
            "mode": resolve_mode(mode),
            "pipeline": list(pipeline_for(mode)),
            "log": [],
            "steps": [],
            "tests": "",
            "tests_status": "",
            "deliverable": "",
        }
        self.save()

    def add(self, role, content):
        self.data["log"].append({"role": role, "content": content})
        self.save()

    def add_step(self, record):
        self.data["steps"].append(record)
        self.save()

    def set_tests(self, source, status, summary=""):
        self.data["tests"] = source or ""
        self.data["tests_status"] = status
        self.data["tests_audit"] = summary
        self.save()

    def set_deliverable(self, text):
        self.data["deliverable"] = text
        self.data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save()

    def save(self):
        try:
            os.makedirs(self.dirpath, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            # A read-only filesystem should not take the run down with it.
            pass


_STEP_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.*)")
# ASSUMPTIONS is listed here as well as parsed, because `_section` cuts a body at
# the next *known* header: an unlisted section placed after SPEC would be read as
# part of the spec and fed to the Executor as requirements.
_HEADERS = ("ASSUMPTIONS", "SPEC", "STEPS", "TESTS")


def _section(text, name):
    """Body of a ``NAME:`` section, ending at the next known header.

    Sections are sliced before parsing so that, for instance, numbered lines
    inside the TESTS code block cannot be mistaken for plan steps.
    """
    text = text or ""
    match = re.search(r"(?:^|\n)\s*%s\s*:" % name, text)
    if not match:
        return ""
    body = text[match.end():]
    others = [h for h in _HEADERS if h != name]
    cut = len(body)
    for header in others:
        found = re.search(r"(?:^|\n)\s*%s\s*:" % header, body)
        if found:
            cut = min(cut, found.start())
    return body[:cut].strip()


def extract_steps(planner_output):
    """Numbered steps, scoped to the STEPS section when one is present."""
    scope = _section(planner_output, "STEPS") or (planner_output or "")
    steps = []
    for line in scope.splitlines():
        match = _STEP_LINE.match(line)
        if match:
            step = match.group(2).strip()
            if step:
                steps.append(step)
    return steps[:MAX_STEPS]


def extract_spec(planner_output):
    """Pull the SPEC paragraph out, falling back to the whole plan."""
    spec = _section(planner_output, "SPEC")
    return spec or (planner_output or "").strip()


def extract_assumptions(planner_output):
    """The Planner's stated assumptions, or "" when it stated none.

    `ASSUMPTIONS: none` is the Planner saying the prompt settled everything, and
    it reads back as "" -- the same as an absent section, because both mean there
    is nothing for a reader to check. The distinction that matters is between "no
    assumptions to show" and "here they are", not between two spellings of the
    first. A section that is only punctuation ("none.", "None") collapses the
    same way.
    """
    body = _section(planner_output, "ASSUMPTIONS")
    if not body:
        return ""
    if body.strip().strip(".").strip().lower() in ("none", "n/a", "nothing"):
        return ""
    return body


def extract_tests(planner_output):
    """Pull the acceptance suite out of the TESTS section."""
    scope = _section(planner_output, "TESTS")
    if not scope:
        return None
    source, _ = harness.extract_code_block(scope, allow_tests=True)
    return source


def build_context(spec, completed):
    """Assemble a bounded context from structured state.

    ``completed`` is a list of ``(number, step, output)``. Only the most recent
    few carry their full output; older ones degrade to titles.
    """
    parts = ["SPEC:\n%s" % clamp(spec, MAX_SPEC_CHARS)]

    if len(completed) > CONTEXT_RECENT_STEPS:
        older = completed[:-CONTEXT_RECENT_STEPS]
        parts.append("Earlier steps (already done, titles only):\n%s" % "\n".join(
            "%d. %s" % (number, step) for number, step, _ in older))
        recent = completed[-CONTEXT_RECENT_STEPS:]
    else:
        recent = completed

    for number, step, output in recent:
        parts.append("Completed step %d: %s\nOutput:\n%s" % (
            number, step, clamp(output, MAX_STEP_OUTPUT_CHARS)))

    return clamp("\n\n".join(parts), MAX_CONTEXT_CHARS)


@dataclass
class StepResult:
    number: int
    step: str
    output: str = ""
    verdict: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    rounds: int = 0
    code: str = ""
    failure_kind: str = ""
    failed_assertion: str = ""
    # The line the failing assert sat on. Only `ExecResult` carried this before;
    # the candidate ranking in `_verify_step` needs it on the step too.
    failed_assertion_line: Optional[int] = None
    # The retained candidate's per-check tally, from the report its suite printed.
    # Descriptive: `verdict` is decided by the harness's exit code and stderr and
    # is never derived from these, and no threshold over them exists anywhere.
    # `checks_trusted` False means the report was absent or incoherent, in which
    # case `checks_passed` is 0 and means "unknown", not "none passed".
    checks_passed: int = 0
    checks_total: int = 0
    checks_trusted: bool = False
    tested: bool = False
    note: str = ""
    # The last rung of ESCALATION used on this step, or "exhausted" if the
    # ladder ran out. "" means the step never needed a revision.
    escalation: str = ""
    sandbox_layers: List[str] = field(default_factory=list)
    # Which provider produced the retained output.
    provider: str = ""
    # Which revision round produced the candidate above. 1-based, so it is
    # comparable with `rounds` (which counts rounds *spent*, not the round the
    # kept answer came from -- with retention those are no longer the same
    # number, and conflating them is what hid the bug).
    retained_round: int = 0
    # True when the last round the ladder ran was strictly worse *on merit*
    # (`_candidate_merit`, i.e. rank without the round tie-break) than the one
    # retained: the direct measurement of the ladder being net-harmful, which
    # was invisible while the last attempt always overwrote the record.
    final_round_worse: bool = False
    # Every attempt, kept whether or not it was returned. Stored, never graded
    # here: hidden-grading candidates inline would put the answer key inside the
    # pipeline being measured.
    candidates: List[dict] = field(default_factory=list)

    @property
    def verified(self):
        return self.verdict == harness.VERDICT_APPROVED


@dataclass
class RunResult:
    run_id: str = ""
    memory_path: str = ""
    mode: int = DEFAULT_MODE
    pipeline: tuple = ()
    plan: str = ""
    spec: str = ""
    assumptions: str = ""
    tests: str = ""
    tests_status: str = TESTS_MISSING
    tests_summary: str = ""
    steps: List[StepResult] = field(default_factory=list)
    deliverable: str = ""

    # Report-only flags from `scan_runtime_syntax(spec)`. Empty means the scan
    # was clean, which is a measurement and not an assumption.
    spec_runtime_syntax: List[dict] = field(default_factory=list)

    @property
    def verified_count(self):
        return sum(1 for step in self.steps if step.verified)

    @property
    def tests_trusted(self):
        return self.tests_status in TRUSTED_TESTS and bool(self.tests)


def run_workspace(user_prompt, keys, mode=DEFAULT_MODE, on_event=None,
                  user_tests=None, plan=None, tests=None):
    """Run the pipeline. ``on_event(role, content)`` is called as each entry is
    produced, so a UI can render the feed incrementally instead of waiting for
    the whole run.

    ``user_tests`` is an optional acceptance suite supplied by the user. It
    always wins over anything the Planner generates.

    ``plan`` is an optional Planner output to use verbatim, skipping the Planner
    call. Everything downstream is unchanged -- the text still goes through
    `extract_spec`, `extract_steps` and `_resolve_tests`. It exists so several
    eval arms can be compared against *the same* plan: a per-arm Planner call
    would make the plan a source of variance inside the contrast it is supposed
    to hold fixed.

    ``tests`` is an *already-resolved* suite (see `resolved_tests`), which skips
    `_resolve_tests` entirely. `plan` alone was not enough: resolving the same
    plan twice agrees only while the audit accepts the plan's own TESTS block.
    When it does not, `_resolve_tests` regenerates via the Test Writer, and two
    independent generations from one plan are two different suites -- so the arms
    gated on different bytes on exactly the tasks where the suite was weakest,
    and paid for a second Test Writer call to do it.

    Precedence: ``user_tests`` beats ``tests`` beats resolving from the plan. The
    user's own suite winning is not a rule that bends for a measurement.
    """
    if tests is not None and plan is None:
        # Injecting a suite resolved from a *different* plan is the exact
        # divergence `tests=` exists to close, and nothing downstream could
        # detect it. Programming error, not a runtime condition.
        raise ValueError("run_workspace(tests=...) requires the plan those "
                         "tests were resolved from; pass plan= as well")
    mode = resolve_mode(mode)
    stages = pipeline_for(mode)
    memory = Memory(prompt=user_prompt, mode=mode)

    # An ambient log wins. `eval/run_eval.py` installs one per cell carrying the
    # task id, arm and repeat, and arm B reaches this function from inside that
    # scope; installing a second log here would send the run's events to a file
    # that has no idea which cell produced them. When nothing is installed -- the
    # interactive app, a direct caller -- the run opens its own beside its JSON.
    events = event_log()
    installed = events is None
    if installed:
        events = EventLog(event_log_path(memory.path), keys=keys)
        set_event_log(events)
    scope = events.scope(run_id=memory.run_id, mode=mode)
    scope.__enter__()
    previous_run_id = set_backoff_run_id(memory.run_id)
    try:
        return _run_workspace(user_prompt, keys, mode, stages, memory, on_event,
                              user_tests, plan, tests)
    finally:
        # Teardown in a `finally` because `ProviderError` propagates out of here.
        # A leaked backoff run id would seed the *next* task's jitter from this
        # task's id, and a leaked log would keep writing into a finished run.
        set_backoff_run_id(previous_run_id)
        scope.__exit__(None, None, None)
        if installed:
            set_event_log(None)


def _run_workspace(user_prompt, keys, mode, stages, memory, on_event,
                   user_tests, plan, tests):
    """`run_workspace`'s body, with the event log and the pacer already set up.

    Split out only so the setup has a `finally` to be torn down in; every
    argument is already validated and resolved by the caller.
    """
    def emit(role, content):
        memory.add(role, content)
        if on_event:
            on_event(role, content)

    run = RunResult(run_id=memory.run_id, memory_path=memory.path, mode=mode,
                    pipeline=stages)

    emit("system", "Run `%s` started - %s" % (memory.run_id, PIPELINE_LABELS[mode]))

    # ---- Planner -------------------------------------------------------
    if plan is None:
        plan, _ = call_role("planner", keys, PROMPTS["planner"], user_prompt, emit)
    else:
        emit("system", "Planner call skipped; a supplied plan was used verbatim")
    emit("planner", plan)
    run.plan = plan
    run.spec = extract_spec(plan)
    run.assumptions = extract_assumptions(plan)
    if run.assumptions:
        # Above the spec, because it is the spec these decisions were folded
        # into. A reader who disagrees with one of them has learned it before
        # reading the paragraph that already assumes it.
        emit("assumptions", run.assumptions)
    # Report-only. Recorded, said once in the transcript, and it changes nothing
    # about what runs next -- see `scan_runtime_syntax` for why rejecting was not
    # chosen.
    run.spec_runtime_syntax = scan_runtime_syntax(run.spec)
    if run.spec_runtime_syntax:
        emit("system", runtime_syntax_note(run.spec_runtime_syntax)
             + " -- recorded, not rejected; the harness neutralises annotations")
    steps = extract_steps(plan) or [user_prompt]
    if len(steps) == 1 and steps[0] == user_prompt:
        emit("system", "No numbered steps found in the plan; treating the "
                       "prompt as a single step")

    verify = "harness" in stages

    # ---- Acceptance suite ----------------------------------------------
    if verify:
        if user_tests and user_tests.strip():
            _resolve_tests(run, plan, keys, emit, user_tests)
        elif tests is not None:
            _adopt_tests(run, tests, emit)
        else:
            _resolve_tests(run, plan, keys, emit, None)
        memory.set_tests(run.tests, run.tests_status, run.tests_summary)

    # ---- Executor (+ Harness) per step ---------------------------------
    completed = []

    for number, step in enumerate(steps, 1):
        context = build_context(run.spec, completed)
        task = "%s\n\nSTEP %d of %d: %s" % (context, number, len(steps), step)

        output, provider = call_role("executor", keys, PROMPTS["executor"], task, emit)
        emit("executor", output)

        record = StepResult(number=number, step=step, output=output,
                            provider=provider)

        if not verify:
            record.verdict = harness.VERDICT_UNVERIFIED
            record.note = "mode %d has no harness stage" % mode
            emit("system", "Step %d not verified - %s" % (number, record.note))
        else:
            record = _verify_step(record, task, keys, emit, run)

        run.steps.append(record)
        completed.append((number, step, record.output))
        step_log = {
            "number": record.number,
            "step": record.step,
            "verdict": record.verdict,
            "exit_code": record.exit_code,
            "rounds": record.rounds,
            "failure_kind": record.failure_kind,
            "failed_assertion": record.failed_assertion,
            "failed_assertion_line": record.failed_assertion_line,
            "checks_passed": record.checks_passed,
            "checks_total": record.checks_total,
            "checks_trusted": record.checks_trusted,
            "tested": record.tested,
            "note": record.note,
            "escalation": record.escalation,
            "retained_round": record.retained_round,
            "final_round_worse": record.final_round_worse,
            "candidates": _candidates_for_log(record.candidates),
            "provider": record.provider,
            "stdout": record.stdout,
            "stderr": record.stderr,
            "code": record.code,
            "sandbox_layers": record.sandbox_layers,
        }
        memory.add_step(step_log)
        _emit_step_events(step_log)

    run.deliverable = _assemble_deliverable(run)
    memory.set_deliverable(run.deliverable)
    emit("system", "Run complete - %d/%d step(s) verified against the "
                   "acceptance suite" % (run.verified_count, len(run.steps)))
    return run


def _emit_step_events(step_log):
    """`step`, one `candidate` per attempt, and `verdict`, from the step's log.

    Derived from the dict that goes into the run JSON rather than from the
    `StepResult`, so the JSONL and the JSON cannot disagree about what happened.
    The bodies -- code, stdout, stderr -- are deliberately left out: they are
    already in the JSON in full, and an append-only log meant to survive a kill
    is worth less the larger each line is.
    """
    emit_event(EVENT_STEP,
               number=step_log["number"],
               provider=step_log["provider"],
               rounds=step_log["rounds"],
               escalation=step_log["escalation"],
               retained_round=step_log["retained_round"],
               final_round_worse=step_log["final_round_worse"],
               candidates=len(step_log["candidates"] or []))
    for candidate in (step_log["candidates"] or []):
        emit_event(EVENT_CANDIDATE,
                   number=step_log["number"],
                   round=candidate.get("round"),
                   verdict=candidate.get("verdict"),
                   exit_code=candidate.get("exit_code"),
                   failure_kind=candidate.get("failure_kind"),
                   escalation=candidate.get("escalation"),
                   checks_passed=candidate.get("checks_passed"),
                   checks_total=candidate.get("checks_total"),
                   checks_trusted=candidate.get("checks_trusted"),
                   code_sha256=sha256_of(candidate.get("code") or ""))
    emit_event(EVENT_VERDICT,
               number=step_log["number"],
               verdict=step_log["verdict"],
               exit_code=step_log["exit_code"],
               tested=step_log["tested"],
               failure_kind=step_log["failure_kind"],
               failed_assertion=step_log["failed_assertion"],
               failed_assertion_line=step_log["failed_assertion_line"],
               checks_passed=step_log["checks_passed"],
               checks_total=step_log["checks_total"],
               checks_trusted=step_log["checks_trusted"],
               code_sha256=sha256_of(step_log["code"] or ""))


def sha256_of(text):
    """The digest of a string, or "" for an empty one.

    Events carry digests where the JSON carries bodies. A digest is enough to
    prove the JSON and the JSONL describe the same bytes, which is the only
    question the event log has to answer about code it is not storing.
    """
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidates_for_log(candidates):
    """The candidate list as it goes into the run JSON.

    Drops the raw model output and the captured streams -- those exist to
    restore and explain the retained attempt, and duplicating them per round
    would treble the log for no later use. `code` is kept **whole**: the
    deferred cold grading pass grades exactly this text, and a truncated
    candidate is an ungradable one.
    """
    keep = ("round", "provider", "code", "verdict", "exit_code",
            "failure_kind", "failed_assertion", "failed_assertion_line",
            "checks_passed", "checks_total", "checks_trusted")
    return [{name: cand.get(name) for name in keep} for cand in candidates]


def _coverage_gap(audit):
    """The stated errors the suite never reaches, as one prose clause.

    Empty string when there is no gap, so the caller can treat it as the second
    rejection reason without a second boolean. The quoted sentence is what makes
    the banner actionable: naming `ValueError` alone does not say which of the
    spec's rules went untested.
    """
    if audit is None or not audit.uncovered:
        return ""
    parts = ["`%s`, from \"%s\"" % (name, clamp(text, 160))
             for name, text in audit.uncovered]
    return ("the SPEC states error behaviour the suite never reaches: %s"
            % "; ".join(parts))


def _resolve_tests(run, plan, keys, emit, user_tests):
    """Settle on an acceptance suite, and refuse to trust a vacuous one.

    User-supplied tests always win. A generated suite is audited; if it proves
    nothing it is regenerated exactly once, and if it still proves nothing the
    run is left UNVERIFIED rather than reporting a false APPROVED.

    An error-coverage gap spends the same single regeneration and then *stops*
    blocking: a suite that asserts real values but skips a stated error path is
    used, loudly, with the gap named in the feed and in `tests_summary`. That is
    a deliberate asymmetry against the vacuity gate. Vacuity means the suite
    cannot support APPROVED at all; a coverage gap means it supports a narrower
    claim than the spec, which is worth stating rather than worth refusing.
    """
    if user_tests and user_tests.strip():
        run.tests = user_tests if user_tests.endswith("\n") else user_tests + "\n"
        run.tests_status = TESTS_USER
        audit = harness.audit_tests(run.tests, spec=run.spec)
        run.tests_summary = audit.summary()
        # The user's own suite wins even if the audit dislikes it -- but say so.
        if not audit.ok:
            emit("system", "Using your suite despite the audit: %s" % audit.reason)
        gap = _coverage_gap(audit)
        if gap:
            emit("system", "Using your suite as given, and noting that %s" % gap)
        emit("tests", _tests_entry(run, audit))
        return

    candidate = extract_tests(plan)
    if candidate:
        audit = harness.audit_tests(candidate, spec=run.spec)
        reason = audit.reason if not audit.ok else _coverage_gap(audit)
        if not reason:
            run.tests, run.tests_status = candidate, TESTS_GENERATED
            run.tests_summary = audit.summary()
            emit("tests", _tests_entry(run, audit))
            return
        emit("system", "Rejected the Planner's acceptance suite: %s" % reason)
    else:
        emit("system", "The Planner emitted no TESTS block; asking for one.")

    # One regeneration attempt, told exactly why the last one was rejected -- and
    # for a coverage gap, told to keep what it had. A suite that asserts real
    # values must not be thrown away to buy a `try/except`.
    request = "SPEC:\n%s" % clamp(run.spec, MAX_SPEC_CHARS)
    if candidate:
        fix = ("Keep every assertion that already asserts a real value and ADD "
               "one `try`/`except` per error the SPEC states."
               if audit.ok else
               "Write a suite that asserts real computed values.")
        request += ("\n\nYour previous suite was REJECTED because: %s\n\n"
                    "Rejected suite:\n```python\n%s```\n%s"
                    % (reason, clamp(candidate, MAX_SPEC_CHARS), fix))
    try:
        raw, _ = call_role("test_writer", keys, PROMPTS["test_writer"], request, emit)
    except ProviderError as exc:
        run.tests_status = TESTS_MISSING
        # Redacted here, at the write site, not on the strength of `call_role`
        # having done it upstream. `tests_summary` is persisted to the run JSON
        # and rendered in the UI, and this exception can be a raw SDK error whose
        # message quotes the request -- key included.
        run.tests_summary = ("could not generate a suite: %s"
                             % _redact(exc, keys))
        emit("tests", _tests_entry(run, None))
        return

    retry = harness.extract_code_block(raw, allow_tests=True)[0]
    audit2 = harness.audit_tests(retry, spec=run.spec) if retry else None
    if audit2 is not None and audit2.ok:
        run.tests, run.tests_status = retry, TESTS_REGENERATED
        run.tests_summary = audit2.summary()
        emit("tests", _tests_entry(run, audit2))
        gap = _coverage_gap(audit2)
        if gap:
            # Loud, and the run continues. Blocking here would trade a suite that
            # tests most of the spec for no suite at all, and the honest thing is
            # to narrow the claim rather than withdraw it.
            emit("system",
                 "**The suite gating this run has a coverage gap.** One "
                 "regeneration was spent trying to close it and did not: %s. "
                 "APPROVED below therefore means \"passed the suite that "
                 "exists\", not \"implements the SPEC\" -- a program that skips "
                 "the stated error handling can still reach it." % gap)
        return

    reason = (audit2.reason if audit2 is not None
              else "the regenerated output contained no code block")
    run.tests = ""
    run.tests_status = (TESTS_VACUOUS if audit2 is not None and audit2.vacuous
                        else TESTS_UNUSABLE)
    run.tests_summary = reason
    emit("tests", _tests_entry(run, audit2))
    emit("system",
         "No trustworthy acceptance suite after one regeneration (%s). Steps "
         "will be marked UNVERIFIED -- a passing exit status alone cannot show "
         "the output is correct, and reporting APPROVED here would be exactly "
         "the false confidence this stage exists to prevent." % reason)


RESOLVED_TESTS_FIELDS = ("tests", "tests_status", "tests_summary",
                         "tests_trusted")


def resolved_tests(run):
    """An already-resolved suite, packaged for ``run_workspace(tests=...)``.

    Source, status, audit summary and the trust flag. Whoever resolved the suite
    hands the *bytes* over, so a second consumer of the same plan gates on the
    same suite instead of generating its own from the same text.
    """
    return {"tests": run.tests, "tests_status": run.tests_status,
            "tests_summary": run.tests_summary,
            "tests_trusted": run.tests_trusted}


def _adopt_tests(run, payload, emit):
    """Take a resolved suite verbatim: no audit, no Test Writer call.

    The status is *carried*, never rewritten. It is deliberately not turned into
    `TESTS_USER`: that status means "the human supplied this and the audit was
    overridden", it bypasses the audit, and stamping it on an eval-injected suite
    would corrupt the suite-validity rate for every run that used one -- the
    direct quality measure of the weakest model's most important output.

    `tests_trusted` is a derived property of the status and the source, so the
    incoming flag is *checked*, not assigned. Assigning it would create a second,
    forgeable source of truth for whether a suite may gate APPROVED, which is
    precisely what the audit exists to prevent.
    """
    missing = [name for name in RESOLVED_TESTS_FIELDS if name not in payload]
    if missing:
        raise ValueError("resolved suite is missing %s; build it with "
                         "resolved_tests()" % ", ".join(missing))
    if payload["tests_status"] == TESTS_USER:
        raise ValueError("a user suite cannot be injected as a resolved one; "
                         "pass it as user_tests= so it is recorded as such")
    run.tests = payload["tests"] or ""
    run.tests_status = payload["tests_status"] or TESTS_MISSING
    run.tests_summary = payload["tests_summary"] or ""
    if bool(payload["tests_trusted"]) != run.tests_trusted:
        raise ValueError(
            "resolved suite claims tests_trusted=%r but status %r with %d "
            "chars of source derives %r"
            % (bool(payload["tests_trusted"]), run.tests_status,
               len(run.tests), run.tests_trusted))
    # Display only -- it names the interface for the feed entry and cannot
    # change any field. `audit_tests` runs the suite against a stub locally; it
    # is not a model call, so this costs nothing that `_resolve_tests` would.
    audit = harness.audit_tests(run.tests) if run.tests else None
    emit("tests", _tests_entry(run, audit))
    emit("system", "Acceptance suite supplied already resolved (`%s`); no suite "
                   "was generated for this run" % run.tests_status)


def _tests_entry(run, audit):
    """The feed entry showing what APPROVED is measured against."""
    lines = ["**Acceptance suite** - source: `%s`" % run.tests_status]
    if run.tests_summary:
        lines.append("Audit: %s" % run.tests_summary)
    if audit is not None and audit.names:
        lines.append("Interface under test: %s" % ", ".join(
            "`%s`" % name for name in audit.names))
    if run.tests:
        lines.append("\n```python\n%s```" % run.tests)
    else:
        lines.append("\n_No suite will be used; steps cannot be APPROVED._")
    return "\n".join(lines)


def _candidate_checks(candidate):
    """How many of the suite's checks this candidate passed, or `-1` for unknown.

    `-1` sorts below a measured zero, which is the conservative direction and not
    an accident. An unknown count almost always means the suite never produced a
    report, and the dominant reason for that is that it never ran -- a missing
    entry point, a syntax error, a timeout. Ranking "we did not measure it" above
    "we measured it and nothing passed" would let an absent measurement promote a
    candidate, which is the one thing the count must never do.
    """
    if not candidate.get("checks_trusted"):
        return -1
    passed = candidate.get("checks_passed")
    return passed if isinstance(passed, int) else -1


def _candidate_rank(candidate):
    """How far a candidate got, as an ordinal tuple. Higher is better.

    1. APPROVED beats everything.
    2. More checks passed beats fewer. Unknown sorts below a measured zero; see
       `_candidate_checks`.
    3. An assertion failure beats anything else: a wrong answer to a check is
       further along than an import error, a timeout or a `NameError`, and the
       per-check report records those apart precisely so this does not have to
       treat them alike.
    4. Earliest round wins ties.
    5. Anything still tied is broken by the digest of the candidate's own code.

    Element 2 replaced the failing assert *line*, which was a heuristic standing
    in for "got further" and read a position in a list whose order the suite
    never promised. Counting checks measures the same intuition directly, and the
    reorder battery in `eval/rank_battery.py` is what establishes that the count
    does not move when the suite's checks are permuted.

    What element 5 is for: rounds can contain more than one candidate -- arm
    `a_prime3` draws three -- so `-round` does not settle every tie, and `max`
    would otherwise keep whichever draw happened to be first. Draw order is
    exactly what a retention rule must not depend on, so the last word goes to
    something the draw order cannot influence. It is an arbitrary rule among
    candidates the key has already found indistinguishable, not a quality claim:
    the only properties asked of it are that it is total and that it is a
    function of the candidate alone. Two candidates with identical code remain
    tied, and are interchangeable in every field this retains.

    One honest limit remains: everything that is not an assertion failure and
    produced no report -- import errors, timeouts, runaway output, no code block,
    harness failure -- still collapses to one rank, so among those the retained
    one is settled by round and then by digest. That is a refusal to guess
    between a round-1 timeout and a round-3 import error, not a claim about them.
    """
    return (
        1 if candidate.get("verdict") == harness.VERDICT_APPROVED else 0,
        _candidate_checks(candidate),
        1 if candidate.get("failure_kind") == harness.FAIL_ASSERTION else 0,
        -candidate.get("round", 0),
        hashlib.sha256((candidate.get("code") or "").encode("utf-8")).hexdigest(),
    )


def _candidate_merit(candidate):
    """`_candidate_rank` without the round tie-break, or the digest under it.

    Comparing full ranks would make "the final round was worse" fire on every
    exhausted step whose candidates all failed the same way, since the later
    round always loses the tie-break. Being later is not being worse -- and
    neither is having a code digest that sorts lower.
    """
    return _candidate_rank(candidate)[:3]


def _retain_best(record, candidates):
    """Put the best candidate back on the record, and say which one it was.

    The loop used to overwrite the record every round, so the *last* attempt
    won by accident: a round-3 answer that dies on import replaced a round-1
    answer that failed one late assert. That is not "the best effort", it is
    "the most recent effort", and freezing it as the measurement convention
    would pre-register the bug.

    Retention reads `_candidate_rank`, so what "best" means is documented there.
    The property this function owns is that the answer is a function of the *set*
    of candidates and not of the order they arrived in: every element of the key
    is computed from one candidate, the last element is total over distinct code,
    and `max`'s own first-wins bias is only reachable between candidates whose
    code is byte-identical.
    """
    if not candidates:
        return record
    best = max(candidates, key=_candidate_rank)
    last = candidates[-1]
    record.output = best["output"]
    record.verdict = best["verdict"]
    record.exit_code = best["exit_code"]
    record.stdout = best["stdout"]
    record.stderr = best["stderr"]
    record.code = best["code"]
    record.failure_kind = best["failure_kind"]
    record.failed_assertion = best["failed_assertion"]
    record.failed_assertion_line = best["failed_assertion_line"]
    record.checks_passed = best["checks_passed"]
    record.checks_total = best["checks_total"]
    record.checks_trusted = best["checks_trusted"]
    record.sandbox_layers = list(best["sandbox_layers"])
    record.provider = best["provider"]
    record.retained_round = best["round"]
    record.final_round_worse = _candidate_merit(last) < _candidate_merit(best)
    return record


def _verify_step(record, task, keys, emit, run):
    """Execute the step's code; on failure escalate deterministically, up to
    MAX_REVISION_ROUNDS times.

    Anti-cheating: the suite passed to the harness is always ``run.tests``, the
    stored source, re-read every round. Nothing the Executor emits can weaken
    it -- a test file in its output is discarded by the extractor.

    What to try next is decided by ESCALATION, in Python. No model is consulted
    about it: the inputs are a verdict, a failure kind and a round number, and
    picking a branch from those needs no judgement.

    Every attempt is kept (`record.candidates`) and the *best* one is returned,
    ranked by `_candidate_rank`. Attempts are stored, never hidden-graded here.
    """
    tests = run.tests if run.tests_trusted else None
    record.tested = bool(tests)
    candidates = record.candidates

    for attempt in range(MAX_REVISION_ROUNDS + 1):
        verdict, result = harness.verify_output(record.output, tests=tests)

        # Without a trustworthy suite, exit status alone cannot establish
        # correctness, so APPROVED is not available. A crash is still a real
        # defect, so REVISE still applies and the loop still runs.
        if verdict == harness.VERDICT_APPROVED and not record.tested:
            verdict = harness.VERDICT_UNVERIFIED
            record.note = ("ran cleanly, but there is no trustworthy "
                           "acceptance suite (%s)" % run.tests_status)

        record.verdict = verdict
        record.exit_code = result.exit_code
        record.stdout = result.stdout
        record.stderr = result.stderr
        record.code = result.source
        record.failure_kind = result.failure_kind
        record.failed_assertion = result.failed_assertion
        record.failed_assertion_line = result.failed_assertion_line
        record.checks_passed = result.checks_passed
        record.checks_total = result.checks_total
        record.checks_trusted = result.checks_trusted
        record.sandbox_layers = list(result.sandbox_layers)
        record.retained_round = attempt + 1
        candidates.append({
            "round": attempt + 1,
            "provider": record.provider,
            "output": record.output,
            "code": result.source,
            "verdict": verdict,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "failure_kind": result.failure_kind,
            "failed_assertion": result.failed_assertion,
            "failed_assertion_line": result.failed_assertion_line,
            "checks_passed": result.checks_passed,
            "checks_total": result.checks_total,
            "checks_trusted": result.checks_trusted,
            "sandbox_layers": list(result.sandbox_layers),
        })

        report = harness.format_report(verdict, result)
        if record.note:
            report += "\n%s" % record.note
        emit("harness", report)

        if verdict == harness.VERDICT_APPROVED:
            # Retention is a no-op here in effect: APPROVED outranks every
            # other candidate, so this returns the round that just passed.
            return _retain_best(record, candidates)
        if verdict == harness.VERDICT_UNVERIFIED:
            # Either the harness could not run, or there is no suite to gate
            # on. Revising the code cannot change either. Deliberately *not*
            # retained-best: UNVERIFIED here means the harness itself
            # malfunctioned, and retaining an earlier REVISE would report a
            # code defect while hiding a harness bug.
            emit("system", "Step %d left UNVERIFIED: %s"
                 % (record.number, record.note or result.reason))
            return record
        if attempt == MAX_REVISION_ROUNDS:
            record.escalation = "exhausted"
            _retain_best(record, candidates)
            record.note = _exhausted_note(record)
            emit("system", "Step %d: %s" % (record.number, record.note))
            if record.final_round_worse:
                emit("system", "Step %d: round %d was worse than round %d; the "
                               "earlier candidate was kept"
                     % (record.number, candidates[-1]["round"],
                        record.retained_round))
            return record

        rung = ESCALATION[attempt]
        fixes = harness.format_fixes(verdict, result)
        prompt, prefer = _revision_request(rung, fixes, task, record, keys,
                                           attempt + 1)

        emit("system", "Step %d failed; escalating to `%s` (round %d of %d) - %s"
             % (record.number, rung, attempt + 1, MAX_REVISION_ROUNDS,
                ESCALATION_WHY[rung]))
        try:
            record.output, provider = call_role(
                "executor", keys, PROMPTS["executor"], prompt, emit, prefer=prefer)
        except ProviderError as exc:
            # Redacted at the write site. `emit` feeds the UI transcript *and*
            # `Memory`, so an unredacted exception here is a key written to disk;
            # relying on `call_role`'s redaction is how one got there before.
            emit("system", "Revision failed: %s" % _redact(exc, keys))
            return _retain_best(record, candidates)
        record.provider = provider
        record.rounds = attempt + 1
        record.escalation = rung
        emit("executor", record.output)

    return _retain_best(record, candidates)


def _revision_request(rung, fixes, task, record, keys, failures):
    """The prompt and provider preference for one rung of the ladder.

    Returns ``(prompt, prefer)``. ``failures`` is how many attempts the harness
    has already executed and rejected. The Executor's own failed code is never
    sent back on any rung -- it already has it, and re-quoting it wastes budget
    and anchors the model to the broken shape.
    """
    spec = clamp(task, MAX_SPEC_CHARS)

    if rung == "fresh":
        # Deliberately withholds the traceback. Feedback-driven repair has
        # already failed more than once, which is evidence the approach is
        # wrong rather than the details -- more detail about the same wrong
        # approach is what keeps a model circling it. The failed assertion
        # stays because it is a fact about the spec, not about the attempt.
        lines = [
            "%d previous attempt%s at this step %s executed against a hidden "
            "acceptance suite and every one of them failed."
            % (failures, "" if failures == 1 else "s",
               "was" if failures == 1 else "were"),
        ]
        if record.failed_assertion:
            lines.append("The last one failed this check: %s"
                         % clamp(record.failed_assertion, 200))
        lines += [
            "",
            "Ignore any approach you have taken so far and start over. Write a "
            "fresh, independent implementation, choosing a different strategy "
            "-- a plainer or more direct one is usually correct here. Do not "
            "patch the earlier attempt.",
            "",
            "Original task:",
            spec,
        ]
        return "\n".join(lines), alternate_provider("executor", keys)

    prompt = "%s\n\nOriginal task:\n%s" % (fixes, spec)
    if rung == "alternate":
        # Same information, different model. The traceback was clear enough
        # that a second reader may simply get it right; if no other key is
        # configured, alternate_provider returns None and this degrades to
        # another repair round, which the note records honestly.
        return prompt, alternate_provider("executor", keys)
    return prompt, None


def _exhausted_note(record):
    """Why a step is being abandoned, in terms of what was actually tried."""
    tried = ", ".join(ESCALATION[:MAX_REVISION_ROUNDS])
    note = ("still failing after %d escalation round(s) (%s); the harness ran "
            "the code every round and it never passed the suite"
            % (MAX_REVISION_ROUNDS, tried))
    if record.failed_assertion:
        note += ". Last failing check: %s" % clamp(record.failed_assertion, 200)
    return note


def _assemble_deliverable(run):
    blocks = ["# %s" % (run.spec.splitlines()[0] if run.spec else "Deliverable"),
              "",
              "Run `%s` - %s" % (run.run_id, PIPELINE_LABELS[run.mode]),
              "Verified against the acceptance suite: %d/%d step(s)"
              % (run.verified_count, len(run.steps)),
              ""]
    if run.assumptions:
        # First, above every claim about the code. The deliverable is what gets
        # read and kept, and a decision the prompt never made is the thing most
        # worth disagreeing with before reading anything that assumes it.
        blocks.append("## Assumptions the plan made\n\n%s\n" % run.assumptions)
    if run.tests:
        blocks.append("## Acceptance suite (`%s`)\n\n%s\n\n```python\n%s```\n"
                      % (run.tests_status, run.tests_summary, run.tests))
    else:
        blocks.append("> No trustworthy acceptance suite was available (%s), so "
                      "no step could be APPROVED.\n" % run.tests_status)

    for record in run.steps:
        status = record.verdict or harness.VERDICT_UNVERIFIED
        if record.exit_code is not None:
            status += " (exit %s)" % record.exit_code
        blocks.append("---\n")
        blocks.append("## Step %d: %s\n\n_%s_\n" % (record.number, record.step, status))
        if record.failed_assertion:
            blocks.append("Failing assertion: `%s`\n" % record.failed_assertion)
        if record.note:
            blocks.append("%s\n" % record.note)
        if record.code:
            blocks.append("```python\n%s```\n" % record.code)
        else:
            blocks.append(record.output + "\n")
        if record.stdout.strip():
            blocks.append("Output when run:\n```\n%s\n```\n" % record.stdout.rstrip())
        if record.stderr.strip() and not record.verified:
            blocks.append("Still failing with:\n```\n%s\n```\n" % record.stderr.rstrip())
    return "\n".join(blocks)
