import streamlit as st

import agents_core
import harness
from agents_core import (
    PIPELINE_LABELS,
    PIPELINES,
    PROVIDERS,
    ProviderError,
    pipeline_for,
    redact,
    required_providers,
    run_workspace,
)

st.set_page_config(page_title="Multi-Agent Code Workspace", page_icon="🤖")
st.title("🤖 Multi-Agent Code Workspace")
st.caption("The verification stage runs the code against an acceptance suite. "
           "APPROVED means the asserts passed, not that nothing crashed.")

# Client-side pacing, on for this process. `agents_core` ships it disabled so an
# eval sweep -- which paces in its own `RateGovernor` around a replaced
# `call_model` -- is not charged for the same wait twice. Nothing else paces the
# interactive app, so the app opts in.
#
# Guarded: Streamlit re-executes this script top to bottom on every interaction,
# and re-installing the pacer would throw away the last-call times, which is
# precisely the state that prevents a burst.
if not agents_core.PACER.enabled:
    agents_core.set_pacing(True)

# The role -> (provider, model) mapping, resolved from configuration rather than
# read out of source. Re-resolved on every rerun, which is free and idempotent:
# Streamlit re-executes this script top to bottom, and a config edited while the
# app is open should take effect on the next interaction rather than needing a
# restart. Choosing a model is still not done here -- the picker UI is out of
# scope -- but *reading* the choice is the point of the configuration layer.
try:
    RESOLVED_ROLES = agents_core.configure_models()
    CONFIG_ERROR = ""
except agents_core.ConfigError as exc:
    RESOLVED_ROLES, CONFIG_ERROR = [], str(exc)

# ---------------------------------------------------------------- sidebar
st.sidebar.header("🔑 API Keys")
if "stored_keys" not in st.session_state:
    st.session_state.stored_keys = {name: "" for name in PROVIDERS}

stored_keys = st.session_state.stored_keys
for provider in PROVIDERS:
    val = st.sidebar.text_input(
        "%s API key" % provider.capitalize(),
        value=stored_keys.get(provider, ""),
        type="password",
        key="key_%s" % provider,
    )
    if val:
        stored_keys[provider] = val

mode = st.sidebar.selectbox(
    "Pipeline",
    sorted(PIPELINES),
    index=sorted(PIPELINES).index(agents_core.DEFAULT_MODE),
    format_func=lambda m: "%d agents - %s" % (m, PIPELINE_LABELS[m]),
)
st.sidebar.caption(
    "Stages: %s" % " → ".join(pipeline_for(mode))
)
st.sidebar.caption(
    "Models (%s): %s" % (
        agents_core.model_config_source(),
        " · ".join("%s=%s/%s" % (entry["role"], entry["provider"],
                                 entry["model"])
                   for entry in RESOLVED_ROLES))
)
if CONFIG_ERROR:
    st.sidebar.error("Model configuration ignored: %s" % CONFIG_ERROR)
# Said out loud, at startup, not left for a reader to derive from the table.
# "Execution grades and the grader never wrote the code" is already only partly
# true -- the Planner and the Test Writer are one model off one spec lineage --
# and a configuration that also points the Executor there makes it false.
for _warning in agents_core.independence_warnings():
    st.sidebar.warning(_warning)
st.sidebar.caption(
    "Harness: %ss timeout, no network, no subprocesses, no stdin."
    % harness.EXEC_TIMEOUT_SECONDS
)

# ---------------------------------------------------------------- main
prompt = st.text_area(
    "Your (vague) prompt:",
    height=100,
    placeholder="e.g. make me a simple snake game in python",
)

with st.expander("🧪 Acceptance tests (optional — yours override the Planner's)"):
    st.caption(
        "Plain asserts importing from `solution`, e.g. "
        "`from solution import median` then `assert median([1,2,3,4]) == 2.5`. "
        "No pytest. Leave this empty and the Planner writes the suite itself — "
        "it is then audited against a stub, and rejected if it would pass "
        "against unimplemented functions."
    )
    user_tests = st.text_area(
        "Your suite:",
        height=150,
        label_visibility="collapsed",
        placeholder="from solution import median\n\nassert median([1, 3, 2]) == 2\nassert median([1, 2, 3, 4]) == 2.5",
    )

if "log" not in st.session_state:
    st.session_state.log = []
if "final" not in st.session_state:
    st.session_state.final = ""
if "run_meta" not in st.session_state:
    st.session_state.run_meta = None

ROLE_STYLE = {
    "planner": ("🟢", "planner"),
    "tests": ("🧾", "acceptance suite · what APPROVED is measured against"),
    "executor": ("🔵", "executor"),
    "harness": ("🧪", "harness · executed"),
    "system": ("⚙️", "system"),
}

# These carry their own fenced blocks, so they must render as markdown rather
# than being clipped as plain text.
FULL_RENDER_ROLES = {"harness", "executor", "planner", "tests"}


def render_entry(container, role, content):
    emoji, label = ROLE_STYLE.get(role, ("⚪", role))
    with container:
        st.markdown("**%s %s**" % (emoji, label))
        if role == "harness" and content.startswith("VERDICT: APPROVED"):
            st.success(content)
        elif role == "harness":
            st.warning(content)
        elif role in FULL_RENDER_ROLES:
            st.markdown(content)
        else:
            st.markdown(content[:2000])
        st.divider()


run_clicked = st.button("🚀 Run workspace", type="primary")

tab_feed, tab_final = st.tabs(["📜 Agent feed", "📦 Final deliverable"])
with tab_feed:
    feed = st.container()
with tab_final:
    final_slot = st.container()

missing = []
if run_clicked:
    missing = [p for p in required_providers(mode) if not stored_keys.get(p)]
    if missing:
        st.sidebar.error("Missing key(s): %s" % ", ".join(missing))
    elif not prompt.strip():
        st.warning("Enter a prompt first.")

should_run = run_clicked and not missing and prompt.strip()

if should_run:
    st.session_state.log = []
    st.session_state.final = ""
    st.session_state.run_meta = None

    status = feed.empty()
    status.info("Agents working… entries appear below as they happen.")

    def on_event(role, content):
        # Render immediately. run_workspace is synchronous inside this script
        # run, so writing into the container that already exists above puts
        # each entry on screen the moment it is produced instead of after the
        # whole pipeline returns.
        st.session_state.log.append((role, content))
        render_entry(feed, role, content)

    try:
        run = run_workspace(prompt, stored_keys, mode, on_event,
                            user_tests=user_tests)
        st.session_state.final = run.deliverable
        st.session_state.run_meta = {
            "run_id": run.run_id,
            "memory_path": run.memory_path,
            "verified": run.verified_count,
            "total": len(run.steps),
            "tests_status": run.tests_status,
            "tests_summary": run.tests_summary,
            "tests_trusted": run.tests_trusted,
        }
        status.empty()
    except ProviderError as exc:
        status.empty()
        # Redacted at the write site: the keys are in this process's session
        # state, and an SDK error can quote the request that carried one.
        feed.error("Provider error: %s" % redact(exc, stored_keys))
    except Exception as exc:
        status.empty()
        feed.error("Workspace crashed:\n\n```\n%s: %s\n```"
                   % (type(exc).__name__, redact(exc, stored_keys)))
elif st.session_state.log:
    # Replay a previous run's feed on plain reruns (widget changes, tab clicks).
    for role, content in st.session_state.log:
        render_entry(feed, role, content)
else:
    feed.info("Run the workspace to see agents collaborate here.")

with final_slot:
    meta = st.session_state.run_meta
    if meta:
        cols = st.columns(3)
        cols[0].metric("Steps passing the suite",
                       "%d / %d" % (meta["verified"], meta["total"]))
        cols[1].metric("Acceptance suite", meta.get("tests_status", "n/a"))
        cols[2].caption("Run `%s`\n\nLog: `%s`" % (meta["run_id"], meta["memory_path"]))
        if meta.get("tests_summary"):
            if meta.get("tests_trusted"):
                st.caption("Suite audit: %s" % meta["tests_summary"])
            else:
                st.warning(
                    "No trustworthy acceptance suite (%s: %s), so no step could "
                    "be APPROVED. A clean exit alone does not show the output "
                    "is correct." % (meta.get("tests_status"), meta["tests_summary"]))
    if st.session_state.final:
        st.markdown(st.session_state.final)
        st.download_button("⬇️ Download deliverable", st.session_state.final,
                           file_name="deliverable.md")
    else:
        st.info("Final deliverable appears here after a run.")
