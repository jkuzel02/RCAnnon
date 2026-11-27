"""Offline checks of the live prompt runner and its case fixtures."""

import json
import re
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import URLError

from jsonschema import Draft202012Validator

from rca.contracts.schemas import REPORT_SCHEMAS, validate_observations
from tests.prompt_checks.run import (
    DEFAULT_TIMEOUT_SECONDS,
    ENDPOINT,
    SPECIALISTS,
    complete,
    evaluate_case,
    load_inputs,
    main,
    missing_concepts,
)


REPORTS = {
    "k8s-config-events": {
        "agent": "k8s-config-events",
        "anomalies": [],
        "suspicious_resources": [],
        "significant_events": [],
        "preliminary_assessment": "Configuration observations received.",
    },
    "logs-connectivity": {
        "log_findings": [],
        "agent": "logs-connectivity",
        "connectivity_findings": [],
        "runtime_failure_mode": "unknown",
        "preliminary_assessment": "Further runtime evidence is needed.",
    },
    "metrics-telemetry": {
        "agent": "metrics-telemetry",
        "resource_findings": [],
        "throttling_detected": False,
        "saturation_identified": None,
        "preliminary_assessment": "Further telemetry is needed.",
    },
}


class PromptRunnerTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompts, cls.cases, cls.expectations = load_inputs()

    def test_every_prompt_receives_the_right_inputs_for_every_case(self):
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id):
                context = case["context"]
                expected = case["root-cause"]
                calls = {}

                def send(prompt, payload):
                    self.assertNotIn(prompt, calls)
                    calls[prompt] = payload
                    self.assertNotIn("root-cause", payload)
                    if prompt == "base.md":
                        return expected["cause"], None
                    response = REPORTS.get(prompt.split("/")[-1].removesuffix(".md"), expected)
                    return json.dumps(response), None

                results = evaluate_case(case_id, context, self.expectations[case_id], send)

                failures = {
                    result["prompt"]: result["error"]
                    for result in results
                    if result["status"] != "passed"
                }
                self.assertEqual(failures, {})
                self.assertEqual(calls.keys(), self.prompts.keys())
                self.assertEqual(calls["base.md"], {"context": context})
                self.assertEqual(calls["optimal.md"], {"context": context})
                for specialist, fields in SPECIALISTS.items():
                    payload = calls[f"mas/{specialist}.md"].copy()
                    if specialist == "metrics-telemetry":
                        limits = payload.pop("resource_limits")
                        if case_id == "01":
                            self.assertIn(
                                {
                                    "resource": "Deployment/api",
                                    "namespace": "rca-01",
                                    "container": "web",
                                    "requests": {"cpu": "25m", "memory": "32Mi"},
                                    "limits": {"cpu": "500m", "memory": "64Mi"},
                                },
                                limits,
                            )
                    self.assertEqual(payload, {field: context[field] for field in fields})
                self.assertEqual(
                    calls["mas/coordinator.md"], {"context": context, "agent_reports": REPORTS}
                )
                self.assertEqual(list(calls)[-1], "mas/coordinator.md")

    def test_invalid_specialist_report_blocks_only_coordinator(self):
        case = self.cases["01"]

        def send(prompt, payload):
            responses = {
                "base.md": case["root-cause"]["cause"],
                "optimal.md": "```json\n{}\n```",
                "mas/k8s-config-events.md": "{}",
                "mas/logs-connectivity.md": json.dumps(REPORTS["logs-connectivity"]),
                "mas/metrics-telemetry.md": json.dumps(REPORTS["metrics-telemetry"]),
            }
            return responses[prompt], None

        results = evaluate_case("01", case["context"], self.expectations["01"], send)

        self.assertEqual(
            [result["status"] for result in results],
            ["passed", "failed", "failed", "passed", "passed", "blocked"],
        )
        self.assertIn("Invalid JSON", results[1]["error"])
        self.assertIn("Schema violation", results[2]["error"])

    def test_secret_key_diagnosis_requires_a_missing_key(self):
        groups = self.expectations["05"]
        self.assertFalse(missing_concepts("Secret lacks api-token.", groups))
        self.assertTrue(missing_concepts("Secret contains api-token.", groups))

    def test_generic_symptom_is_not_a_diagnosis(self):
        groups = self.expectations["01"]
        self.assertEqual(missing_concepts("CrashLoopBackOff", groups), groups)
        cpu_groups = self.expectations["02"]
        self.assertEqual(missing_concepts("CPU limit is 150m", cpu_groups), cpu_groups[1:])

    def test_specialists_cannot_report_missing_observations_as_measured(self):
        case = self.cases["01"]
        reports = {
            **REPORTS,
            "logs-connectivity": {
                **REPORTS["logs-connectivity"],
                "log_findings": [
                    {"source": "logs", "level": "info", "summary": "No container logs were provided."}
                ],
            },
            "metrics-telemetry": {**REPORTS["metrics-telemetry"], "throttling_detected": True},
        }

        def send(prompt, payload):
            agent = prompt.removeprefix("mas/").removesuffix(".md")
            diagnosis = case["root-cause"]
            if prompt == "base.md":
                return diagnosis["cause"], None
            return json.dumps(reports.get(agent, diagnosis)), None

        results = evaluate_case("01", case["context"], self.expectations["01"], send)

        self.assertIn("no logs in the input", results[3]["error"])
        self.assertIn("no metrics in the input", results[4]["error"])
        self.assertEqual(results[-1]["status"], "blocked")

    def test_logs_and_metrics_examples_match_the_runtime_contract(self):
        pattern = re.compile(
            r"\*\*Input:\*\*\s*```json\n(.*?)\n```\s*"
            r"\*\*Output:\*\*\s*```json\n(.*?)\n```",
            re.DOTALL,
        )
        for specialist, source in (("logs-connectivity", "logs"), ("metrics-telemetry", "metrics")):
            examples = pattern.findall(self.prompts[f"mas/{specialist}.md"])
            self.assertTrue(examples)
            self.assertTrue(any(json.loads(payload)[source] == [] for payload, _ in examples))
            validator = Draft202012Validator(REPORT_SCHEMAS[specialist])
            for input_text, output_text in examples:
                with self.subTest(specialist=specialist, payload=input_text):
                    payload = json.loads(input_text)
                    report = json.loads(output_text)
                    validator.validate(report)
                    validate_observations(report, payload)

    @patch("tests.prompt_checks.run.urlopen")
    def test_chat_completion_request_and_usage(self, urlopen):
        response = {
            "choices": [{"message": {"content": "RCA result"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 42},
        }
        urlopen.return_value = BytesIO(json.dumps(response).encode("utf-8"))

        self.assertEqual(
            complete("test-model", "fake-key", "Prompt", {"context": {}}),
            ("RCA result", {"total_tokens": 42}),
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "Prompt"})
        self.assertEqual(json.loads(body["messages"][1]["content"]), {"context": {}})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], DEFAULT_TIMEOUT_SECONDS)

    @patch("tests.prompt_checks.run.urlopen")
    def test_chat_completion_distinguishes_safe_timeout_and_connection_errors(self, urlopen):
        for error, expected in (
            (TimeoutError("sensitive provider detail"), "timed out after 2.5 seconds"),
            (URLError(TimeoutError("sensitive provider detail")), "timed out after 2.5 seconds"),
            (URLError("sensitive provider detail"), "connection failed"),
        ):
            with self.subTest(error=type(error)), self.assertRaisesRegex(ValueError, expected) as raised:
                urlopen.side_effect = error
                complete("test-model", "fake-key", "Prompt", {}, timeout=2.5)
            self.assertNotIn("sensitive provider detail", str(raised.exception))
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 2.5)

    def test_main_cli_only_writes_no_file_by_default(self):
        case = self.cases["01"]
        expected = case["root-cause"]
        calls = []

        def fake_complete(model, api_key, prompt, payload, *, endpoint, timeout):
            calls.append((model, api_key, endpoint, timeout))
            if prompt == self.prompts["base.md"]:
                return expected["cause"], None
            for specialist, report in REPORTS.items():
                if prompt == self.prompts[f"mas/{specialist}.md"]:
                    return json.dumps(report), None
            return json.dumps(expected), None

        with (
            TemporaryDirectory() as tmpdir,
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-openai-key"}, clear=True),
            patch("tests.prompt_checks.run.complete", side_effect=fake_complete),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            status = main(["--case", "01"])
            self.assertEqual(status, 0)
            self.assertIn("6/6 prompt checks passed.", stdout.getvalue())
            self.assertEqual(list(Path(tmpdir).iterdir()), [])
            self.assertTrue(
                all(
                    call == ("gpt-4o-mini", "fake-openai-key", ENDPOINT, DEFAULT_TIMEOUT_SECONDS)
                    for call in calls
                )
            )

            output_file = Path(tmpdir) / "results.json"
            status_with_out = main(["--case", "01", "--output", str(output_file)])
            self.assertEqual(status_with_out, 0)
            self.assertTrue(output_file.is_file())

    def test_main_supports_custom_openai_endpoint_model_and_timeout(self):
        case = self.cases["01"]
        expected = case["root-cause"]
        calls = []

        def fake_complete(model, api_key, prompt, payload, *, endpoint, timeout):
            calls.append((model, api_key, endpoint, timeout))
            if prompt == self.prompts["base.md"]:
                return expected["cause"], None
            for specialist, report in REPORTS.items():
                if prompt == self.prompts[f"mas/{specialist}.md"]:
                    return json.dumps(report), None
            return json.dumps(expected), None

        with (
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "fake-custom-key",
                    "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1/",
                    "OPENAI_MODEL": "llama3.1:8b",
                    "OPENAI_TIMEOUT_SECONDS": "150",
                },
                clear=True,
            ),
            patch("tests.prompt_checks.run.complete", side_effect=fake_complete),
            patch("sys.stdout", new_callable=StringIO),
        ):
            self.assertEqual(main(["--case", "01"]), 0)

        self.assertTrue(
            all(
                call == ("llama3.1:8b", "fake-custom-key", "http://127.0.0.1:11434/v1/chat/completions", 150.0)
                for call in calls
            )
        )
