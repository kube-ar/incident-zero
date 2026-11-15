"""Milestone 0's incident: the files match their generator, the truth matches the files, and the
story the page tells (a guilty deploy) is one the logs can refute."""

import importlib.util
import json
import re
from datetime import timedelta

from oncall import INCIDENTS, ROOT
from oncall.incident import load_incident

FOLDER = INCIDENTS / "zero"


def generator():
    spec = importlib.util.spec_from_file_location("build_incident_zero", ROOT / "tools" / "build_incident_zero.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iso(s: str):
    from datetime import datetime

    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_the_files_are_what_the_generator_writes(tmp_path):
    generator().write(tmp_path)
    for path in sorted(FOLDER.rglob("*")):
        if path.is_file():
            assert (tmp_path / path.relative_to(FOLDER)).read_bytes() == path.read_bytes(), path


def test_a_few_hundred_lines_in_real_formats():
    incident = load_incident()
    assert 400 <= len(incident.lines) <= 1000
    nginx = re.compile(r'^[\d.]+ - - \[\d{2}/Nov/2026:\d{2}:\d{2}:\d{2} \+0000\] "(GET|POST) \S+ HTTP/1.1" \d{3} \d+ "-" '
                       r'"[^"]+" rt=\d+\.\d{3} urt=\d+\.\d{3} upstream=[a-z-]+ request_id=[0-9a-f]{32}$')  # fmt: skip
    for line in incident.lines:
        if line.source == "gateway":
            assert nginx.match(line.text), line.text
        else:
            record = json.loads(line.text)
            assert record["service"] == line.source and record["level"] in ("info", "warn", "error")


def test_request_ids_link_the_gateway_to_the_services():
    incident = load_incident()
    app = {json.loads(x.text).get("request_id") for x in incident.lines if x.source != "gateway"}
    for line in incident.lines:
        if line.source == "gateway" and ("upstream=catalog-api" in line.text or "upstream=playback-api" in line.text):
            assert line.text.rsplit("request_id=", 1)[1] in app


def test_the_truth_matches_the_lines():
    incident = load_incident()
    truth = incident.truth
    words = {"config reload": "config reloaded", "pool resized": "database pool resized",
             "pool timeouts": "waiting for a database connection", "pool saturated": "database pool saturated"}  # fmt: skip
    for group, ids in truth["support"].items():
        assert ids
        for i in ids:
            assert words[group] in incident.line(i).text, (group, i)
    assert incident.change(truth["cause"])["kind"] == "config"
    deploys = [c for c in incident.changes if c["kind"] == "deploy"]
    assert truth["trap"] == max(deploys, key=lambda c: c["at"])["id"]  # the trap is the last deployment
    for i in truth["symptoms"]["gateway 500s from catalog-api"]:
        assert i not in incident.support


def test_the_deploy_is_innocent_and_the_logs_say_so():
    incident = load_incident()
    truth = incident.truth
    deployed, applied, first = iso(truth["deploy_finished"]), iso(truth["config_applied"]), iso(truth["first_error"])
    assert deployed + timedelta(minutes=3) < applied < first < applied + timedelta(seconds=30)
    # no 5xx anywhere before the config change, and the new version served traffic in between
    for line in incident.lines:
        if line.ts < applied:
            assert " 500 " not in line.text and '"status":500' not in line.text
    served = [x for x in incident.lines if x.ts > deployed and x.ts < applied and '"version":"2.41.0"' in x.text
              and '"status":200' in x.text]  # fmt: skip
    assert len(served) >= 20
    deploy_diff = incident.diffs[truth["trap"]]
    touched = re.findall(r"^diff --git a/(\S+)", deploy_diff, re.M)
    assert touched and all(p.startswith(("locales/", "tests/")) for p in touched)
    assert "-  max_connections: 40\n+  max_connections: 4\n" in incident.diffs[truth["cause"]]


def test_the_page_quotes_real_numbers():
    incident = load_incident()
    alert = incident.truth["alert"]
    window = [x for x in incident.lines if x.source == "gateway" and iso("2026-11-12T14:06:00Z") <= x.ts
              < iso("2026-11-12T14:08:00Z")]  # fmt: skip
    errors = [x for x in window if re.search(r'HTTP/1.1" 5\d\d ', x.text)]
    assert (len(window), len(errors)) == (alert["window"], alert["errors"])
    assert f"{100 * len(errors) / len(window):.1f}%" in incident.page
