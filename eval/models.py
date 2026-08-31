"""What can this key actually reach, and will this configuration run.

Three questions this project has had to answer by hand at least twice, each time
with a script written into /tmp and then lost:

    python3 eval/models.py --resolved            what each role will call
    python3 eval/models.py --preflight           will every pair answer (1 call each)
    python3 eval/models.py --list groq           what the key can reach

`--resolved` needs no key and no network. `--preflight` spends one call per
distinct (provider, model) pair -- two, on the default configuration, because
`planner` and `test_writer` share a pair. `--list` spends none: `models.list()`
is free, and it is also why it is not enough on its own -- a key can list a model
it cannot call, so `--preflight` makes a real completion.

Configuration comes from `MAW_MODELS` (inline JSON, or a path) or `models.json`
beside `agents_core.py`. With neither, the source defaults stand. Example, and
the model ID here is a placeholder rather than a recommendation -- the Executor
slug is pinned in the source defaults by D-13, and overriding it here is a
departure from the registration, not a configuration convenience:

    MAW_MODELS='{"roles": {"executor": {"model": "example-model-id"}}}'
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents_core  # noqa: E402
import run_eval  # noqa: E402


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Resolve, validate and list the configured models.")
    parser.add_argument("--resolved", action="store_true",
                        help="print the resolved role -> (provider, model) table")
    parser.add_argument("--preflight", action="store_true",
                        help="one cheap call per distinct pair; exit 1 on any "
                             "failure, naming the role, provider and model")
    parser.add_argument("--list", dest="list_provider", default=None,
                        help="model IDs the key can reach at this provider")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not (args.resolved or args.preflight or args.list_provider):
        args.resolved = True

    try:
        resolved = agents_core.configure_models()
    except agents_core.ConfigError as exc:
        print("model configuration: %s" % exc, file=sys.stderr)
        return 2

    payload = {"source": agents_core.model_config_source()}
    if args.resolved:
        payload["resolved"] = resolved
        payload["independence_warnings"] = agents_core.independence_warnings()
        if not args.json:
            print("models (%s):" % payload["source"])
            for entry in resolved:
                print("  %-12s %-10s %-32s %s"
                      % (entry["role"], entry["provider"], entry["model"],
                         " ".join("%s=%r" % pair for pair in sorted(entry.items())
                                  if pair[0] not in ("role", "provider", "model",
                                                     "base_url"))))
            for line in payload["independence_warnings"]:
                print("  WARNING: %s" % line)
            print("  pairs to validate: %s"
                  % "; ".join("%s/%s (%s)" % (provider, model,
                                              "+".join(role_names))
                              for provider, model, role_names
                              in agents_core.resolved_pairs()))

    keys = run_eval._keys_from_env()
    rc = 0

    if args.list_provider:
        provider = args.list_provider
        if provider not in agents_core.PROVIDERS:
            print("unknown provider %r; configured: %s"
                  % (provider, ", ".join(sorted(agents_core.PROVIDERS))),
                  file=sys.stderr)
            return 2
        if not keys.get(provider):
            print("no API key for %s" % provider, file=sys.stderr)
            return 2
        try:
            models = agents_core.list_models(provider, keys[provider])
        except Exception as exc:
            # Redacted: the key is in scope here and a client's error text has
            # been known to echo the request it was built from.
            print("listing %s failed: %s"
                  % (provider, agents_core._redact(exc, keys)), file=sys.stderr)
            return 1
        payload["models"] = {provider: models}
        if not args.json:
            print("%s: %d model(s) reachable with this key" % (provider,
                                                              len(models)))
            for name in models:
                print("  %s" % name)

    if args.preflight:
        records = agents_core.preflight(keys)
        payload["preflight"] = records
        problems = agents_core.preflight_failures(records)
        if not args.json:
            for record in records:
                print("  %-4s %s -> %s/%s%s"
                      % ("OK" if record["ok"] else "FAIL",
                         "+".join(record["roles"]), record["provider"],
                         record["model"],
                         "" if record["ok"] else "  " + record["detail"]))
        if problems:
            rc = 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
