#!/usr/bin/env python3
"""Falsify a 20-requests-per-day cap on a candidate Gemini slug.

Throwaway diagnostic, not part of the pipeline. It answers exactly one question:
does slug X allow more than 20 generate requests in a day? Nothing here changes
the measured environment -- the slug is passed explicitly to `preflight_pair`,
so `models.json` / `MAW_MODELS` are untouched and the Planner keeps calling
whatever it calls today.

Three properties that make the count trustworthy:

1. **One HTTP request per attempt.** `agents_core.preflight_pair` has no retry
   layer, unlike the pipeline's call path -- which is why the last exhaustion
   spent 104 requests to make 19 plans. Here "21 requests" means 21.
2. **Paced.** A per-minute 429 is not the measurement. Default gap 5s = 12/min,
   under the 15 RPM the table claims for the candidate slugs.
3. **Stops on the first refusal**, and records the provider's own `quotaId` and
   `quotaValue` rather than inferring a number from a failure count.

The key is read with `getpass`, never from argv and never from the environment,
and is asserted absent from every record before anything is written.

    python3 probe_rpd.py --self-test
    python3 probe_rpd.py --model gemini-2.0-flash
"""

import argparse
import datetime
import getpass
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents_core as core

# `agents_core.quota_id_of` exists; the value does not have a reader there, and
# adding one would collide with an in-flight sprint editing that file.
_QUOTA_VALUE = re.compile(r"""['"]?quotaValue['"]?\s*[:=]\s*['"]?(\d+)""")

VERDICT_FALSIFIED = "CAP_ABOVE_PROBE"
VERDICT_PER_DAY = "PER_DAY_CAP_HIT"
VERDICT_RATE = "RATE_LIMIT_NOT_PER_DAY"
VERDICT_SLUG = "SLUG_UNAVAILABLE"
VERDICT_AUTH = "AUTH_REJECTED"
VERDICT_OTHER = "OTHER_FAILURE"

# Google answers an unknown model on the OpenAI-compatible endpoint with 400
# INVALID_ARGUMENT, and answers a bad key with 400 INVALID_ARGUMENT too. The
# status alone cannot tell them apart, so the body is read. Getting this wrong
# once already produced "the slug is not available to this key" for what was
# actually a rejected key -- an over-claim in the direction that wastes a day.
_AUTH_MARKS = ("api key", "api_key_invalid", "unauthenticated",
               "permission_denied", "credential")
_SLUG_MARKS = ("model", "not found", "is not supported")


def quota_value_of(message):
    """The provider's own ``quotaValue`` out of a 429 body, or None."""
    found = _QUOTA_VALUE.search(message or "")
    return int(found.group(1)) if found else None


def classify(status, detail):
    """``(verdict, quota_id, quota_value)`` for one refusal."""
    quota_id = core.quota_id_of(detail)
    quota_value = quota_value_of(detail)
    text = (detail or "").lower()
    if status == 429:
        daily, _why = core.daily_quota_violation(detail)
        if daily or (quota_id and "perday" in
                     quota_id.replace("-", "").replace("_", "").lower()):
            return VERDICT_PER_DAY, quota_id, quota_value
        return VERDICT_RATE, quota_id, quota_value
    if status in (400, 401, 403) and any(m in text for m in _AUTH_MARKS):
        return VERDICT_AUTH, quota_id, quota_value
    if status == 404 or (status == 400 and any(m in text for m in _SLUG_MARKS)):
        return VERDICT_SLUG, quota_id, quota_value
    return VERDICT_OTHER, quota_id, quota_value


def key_shape(key):
    """What can be said about a key without revealing it.

    The length and the `AIza` prefix are public format facts, not secrets, and a
    mangled paste is invisible without them. Interior whitespace is the failure
    `strip()` cannot catch.
    """
    raw = key or ""
    stripped = raw.strip()
    return {"length": len(raw),
            "aiza_prefix": raw.startswith("AIza"),
            "interior_whitespace": any(ch.isspace() for ch in stripped)}


def probe(provider, model, key, requests, gap, call=None, sleep=time.sleep,
          now=None):
    """Fire up to ``requests`` minimal calls, stopping at the first refusal."""
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    records = []
    verdict = VERDICT_FALSIFIED
    for attempt in range(1, requests + 1):
        ok, status, detail = core.preflight_pair(provider, model, key,
                                                 call=call)
        row = {"attempt": attempt, "at": now(), "ok": bool(ok),
               "status": status}
        if ok:
            records.append(row)
            if attempt < requests:
                sleep(gap)
            continue
        verdict, quota_id, quota_value = classify(status, detail)
        row.update({"verdict": verdict, "quota_id": quota_id,
                    "quota_value": quota_value, "detail": detail})
        records.append(row)
        break
    return {"provider": provider, "model": model, "requested": requests,
            "gap_seconds": gap, "attempted": len(records),
            "succeeded": sum(1 for r in records if r["ok"]),
            "verdict": verdict, "attempts": records}


def _assert_no_key(result, key):
    """Refuse to write anything containing the key. Second layer, not the first."""
    if key and len(key) >= 4 and key in json.dumps(result):
        raise SystemExit("ABORT: key value found in the probe record; not written")


def _report(result):
    print("")
    print("slug      : %s (%s)" % (result["model"], result["provider"]))
    print("attempted : %d of %d" % (result["attempted"], result["requested"]))
    print("succeeded : %d" % result["succeeded"])
    print("verdict   : %s" % result["verdict"])
    last = result["attempts"][-1] if result["attempts"] else {}
    if not last.get("ok", True):
        print("status    : %s" % last.get("status"))
        print("quotaId   : %s" % (last.get("quota_id") or "(none in body)"))
        print("quotaValue: %s" % last.get("quota_value"))
    if result["verdict"] == VERDICT_FALSIFIED:
        print("\n=> %d requests accepted. A 20/day cap on this slug is "
              "FALSIFIED." % result["succeeded"])
        print("   This is a quota result only. It says nothing about whether "
              "the slug writes usable specs -- that is step 4.")
    elif result["verdict"] == VERDICT_PER_DAY:
        print("\n=> Per-day cap hit after %d accepted requests. Believe the "
              "quotaValue above, not this count." % result["succeeded"])
    elif result["verdict"] == VERDICT_RATE:
        print("\n=> Refused, but NOT by a per-day quota. The probe was too "
              "fast. Re-run with a larger --gap; today's count is not spent by "
              "a per-minute rejection.")
    elif result["verdict"] == VERDICT_AUTH:
        print("\n=> The KEY was rejected, not the slug. Nothing was learned "
              "about the quota and nothing was spent. Check the key shape line "
              "above, then re-run against the known-good slug as a control.")
    elif result["verdict"] == VERDICT_SLUG:
        print("\n=> The slug was rejected by name. It is not available to this "
              "key. No quota conclusion.")
    elif result["verdict"] == VERDICT_OTHER:
        print("\n=> Unclassified refusal. Read `detail` in the record before "
              "concluding anything.")


def _self_test():
    """Prove the classifier and the stop logic offline. Zero requests."""

    class FakeError(Exception):
        def __init__(self, message, status):
            Exception.__init__(self, message)
            self.status_code = status

    per_day = ("Error code: 429 - {'error': {'code': 429, 'status': "
               "'RESOURCE_EXHAUSTED', 'details': [{'violations': [{'quotaId': "
               "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
               "'quotaValue': '20'}], 'retryDelay': '42s'}]}}")
    per_minute = ("Error code: 429 - {'error': {'code': 429, 'status': "
                  "'RESOURCE_EXHAUSTED', 'details': [{'violations': ["
                  "{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-"
                  "FreeTier', 'quotaValue': '15'}], 'retryDelay': '4s'}]}}")
    not_found = "Error code: 404 - {'error': {'message': 'models/x not found'}}"
    bad_key = ("Error code: 400 - [{'error': {'code': 400, 'message': 'Please "
               "pass a valid API key', 'status': 'INVALID_ARGUMENT'}}]")
    bad_slug = ("Error code: 400 - [{'error': {'code': 400, 'message': "
                "'models/nope is not found for API version v1beta', 'status': "
                "'INVALID_ARGUMENT'}}]")

    def maker(fail_at, message, status):
        state = {"n": 0}

        def call(provider, key, slug, system, user, params):
            state["n"] += 1
            if fail_at is not None and state["n"] == fail_at:
                raise FakeError(message, status)
            return "1"
        return call

    cases = [
        ("all 21 accepted", maker(None, "", 0), 21, VERDICT_FALSIFIED, 21),
        ("per-day 429 at #6", maker(6, per_day, 429), 6, VERDICT_PER_DAY, 5),
        ("per-minute 429 at #3", maker(3, per_minute, 429), 3, VERDICT_RATE, 2),
        ("404 at #1", maker(1, not_found, 404), 1, VERDICT_SLUG, 0),
        ("bad key at #1", maker(1, bad_key, 400), 1, VERDICT_AUTH, 0),
        ("bad slug 400 at #1", maker(1, bad_slug, 400), 1, VERDICT_SLUG, 0),
    ]
    failures = 0
    for name, call, attempted, verdict, succeeded in cases:
        got = probe("gemini", "fake-slug", "AIzaSy-not-a-real-key-00112233",
                    21, 0.0, call=call, sleep=lambda _s: None)
        bad = []
        if got["attempted"] != attempted:
            bad.append("attempted %d != %d" % (got["attempted"], attempted))
        if got["verdict"] != verdict:
            bad.append("verdict %s != %s" % (got["verdict"], verdict))
        if got["succeeded"] != succeeded:
            bad.append("succeeded %d != %d" % (got["succeeded"], succeeded))
        print("%-22s %s%s" % (name, "PASS" if not bad else "FAIL",
                              "" if not bad else "  " + "; ".join(bad)))
        failures += 1 if bad else 0
    quota = probe("gemini", "fake-slug", "AIzaSy-not-a-real-key-00112233", 21,
                  0.0, call=maker(6, per_day, 429), sleep=lambda _s: None)
    value = quota["attempts"][-1]["quota_value"]
    ok = value == 20
    print("%-22s %s (read %s)" % ("quotaValue parsed", "PASS" if ok else "FAIL",
                                  value))
    failures += 0 if ok else 1
    sample = "AIzaSy-not-a-real-key-00112233"
    shape = key_shape(sample)
    shape_ok = (shape["length"] == len(sample) and shape["aiza_prefix"] is True
                and shape["interior_whitespace"] is False)
    mangled = key_shape("AIzaSy not-a-real")["interior_whitespace"] is True
    tabbed = key_shape("AIza\tabc")["interior_whitespace"] is True
    wrong_prefix = key_shape("gsk_abc")["aiza_prefix"] is False
    good = shape_ok and mangled and tabbed and wrong_prefix
    print("%-22s %s" % ("key shape", "PASS" if good else "FAIL"))
    failures += 0 if good else 1
    print("\n%s" % ("all offline checks pass" if not failures
                    else "%d offline check(s) FAILED" % failures))
    return 0 if not failures else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="candidate slug, e.g. gemini-2.0-flash")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--requests", type=int, default=21,
                        help="21 is one more than the measured cap")
    parser.add_argument("--gap", type=float, default=5.0,
                        help="seconds between requests; 5.0 = 12/min")
    parser.add_argument("--out", default=None, help="JSON record path")
    parser.add_argument("--self-test", action="store_true",
                        help="offline checks only, zero requests")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.model:
        parser.error("--model is required (or use --self-test)")
    if args.provider not in core.PROVIDERS:
        parser.error("unknown provider %r; configured: %s"
                     % (args.provider, ", ".join(sorted(core.PROVIDERS))))

    key = getpass.getpass("%s (not echoed): " % core.key_env(args.provider))
    key = key.strip()
    if not key:
        print("no key entered")
        return 2

    shape = key_shape(key)
    print("key shape : %d chars, AIza prefix %s, interior whitespace %s"
          % (shape["length"], "yes" if shape["aiza_prefix"] else "no",
             "YES" if shape["interior_whitespace"] else "no"))
    if shape["interior_whitespace"]:
        print("            ^ interior whitespace almost always means a mangled "
              "paste. Check before spending requests.")
    print("%d requests at %.1fs spacing, about %.1f minutes. Ctrl-C is safe."
          % (args.requests, args.gap, args.requests * args.gap / 60.0))
    started = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        result = probe(args.provider, args.model, key, args.requests, args.gap)
    except KeyboardInterrupt:
        print("\ninterrupted; nothing written")
        return 130
    result["started"] = started
    result["key_shape"] = shape
    result["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    _assert_no_key(result, key)

    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "probe_rpd_%s_%s.json" % (args.model.replace("/", "_"),
                                  datetime.date.today().isoformat()))
    with open(out, "w") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    _report(result)
    print("\nrecord    : %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
