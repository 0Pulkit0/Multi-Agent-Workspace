# Multi-Agent Workspace

A Python and Streamlit coding prototype: a planner writes a specification and tests, an executor generates code, and an execution harness checks candidates and supplies repair feedback. Passing the available tests is not proof of complete correctness.

## Study 1: paper and reproducibility package

[**When the Baseline Changes the Experiment: A Calibration Study of Verification-First Code Generation** — Study 1 v1.0.0](https://github.com/0Pulkit0/Multi-Agent-Workspace/releases/tag/study-1-v1.0.0)

The release contains the paper and **`REPRODUCIBILITY_PACKAGE.zip`**, the authoritative snapshot of the retained research evidence and offline reproduction tools. This repository branch contains the earlier application and experimental code. GitHub's automatically generated source archives are not the research package.

To reproduce the paper's checks, extract the attached `REPRODUCIBILITY_PACKAGE.zip` and run these commands from its top-level directory with Python 3.9 or newer:

```sh
python3 -B verify_package.py
python3 -B reproduce/verify.py
```

The package workflow makes no model calls. It recounts stored results and re-executes the candidates that were retained; it does not regenerate historical model responses. The planned repair-versus-resampling treatment comparison was not run.

## Run the application

From this repository's root:

```sh
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

Configure provider keys in the app. Generation uses the configured providers and their quotas; provider/model settings belong to this development snapshot and may need updating for current availability.

The application's existing offline checks can be run separately:

```sh
python3 -B test_harness.py
python3 -B test_pipeline.py
```

## Documentation and reuse

- [Documentation index](docs/README.md): pipeline details, experiment protocol and historical path map.
- [Historical development documents](docs/history/README.md): the former root briefs and handoffs, preserved without rewriting them.
- [Licensing scope](docs/LICENSING.md): author-owned code uses [MIT](LICENSE); author-owned documentation and data use [CC BY 4.0](LICENSES/CC-BY-4.0.txt), with third-party exceptions.
