"""Tests for secret redaction and Kubernetes resource allowlist sanitization."""

from __future__ import annotations

import json

from rca.evidence.redaction import redact_reason, redact_text, redact_value, sanitize_resource


def test_redact_text_scrubs_canary_credentials_and_kubeconfig_blocks():
    canary_token = "canary-cluster-token-987654"
    pem_block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    sample = "\n".join(
        [
            f"Authorization: Bearer {canary_token}",
            "Authorization: Basic dXNlcjpzZWNyZXQ=",
            "api_key=sk-test1234567890abcdef",
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
            "url=https://admin:superpass@k8s.example.internal:6443/api",
            "client-key-data: LS0tLS1CRUdJTiBSU0EtLS0t",
            "certificate-authority-data: LS0tLS1CRUdJTiBDRVJULT0=",
            "token: kube-raw-token-value",
            pem_block,
        ]
    )

    cleaned = redact_text(sample, secrets=(canary_token,))

    for forbidden in (
        canary_token,
        "dXNlcjpzZWNyZXQ=",
        "sk-test1234567890abcdef",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "superpass",
        "LS0tLS1CRUdJTiBSU0EtLS0t",
        "LS0tLS1CRUdJTiBDRVJULT0=",
        "kube-raw-token-value",
        "MIIEowIBAAKCAQEA",
    ):
        assert forbidden not in cleaned
    assert "[REDACTED]" in cleaned
    assert "[REDACTED_KEY]" in cleaned


def test_redact_value_does_not_mutate_input_and_bounds_reasons():
    original = {
        "logs": [{"message": "password=my-secret-value"}],
        "nested": ("Bearer topsecret-value",),
    }
    redacted = redact_value(original)
    assert original["logs"][0]["message"] == "password=my-secret-value"
    assert redacted["logs"][0]["message"] == "password=[REDACTED]"
    assert redacted["nested"] == ["Bearer [REDACTED]"]

    long_reason = "Bearer literal-canary-123 " + ("x" * 300)
    bounded = redact_reason(long_reason, secrets=("literal-canary-123",))
    assert bounded is not None
    assert "literal-canary-123" not in bounded
    assert len(bounded) <= 240
    assert bounded.endswith("...")


def test_sanitize_resource_drops_secrets_by_default_and_preserves_secret_key_refs():
    secret_obj = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "credentials", "namespace": "rca-05"},
        "type": "Opaque",
        "data": {"other-key": "c2VjcmV0LXZhbHVl"},
    }
    assert sanitize_resource(secret_obj) is None

    inventory = sanitize_resource(secret_obj, allow_secret_key_inventory=True)
    assert inventory is not None
    assert inventory["data_keys"] == ["other-key"]
    assert "c2VjcmV0LXZhbHVl" not in json.dumps(inventory)

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "api",
            "namespace": "rca-05",
            "managedFields": [{"manager": "kubectl"}],
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": '{"secret":"leak"}',
                "deployment.kubernetes.io/revision": "2",
                "custom/auth-token": "should-be-dropped",
            },
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "web",
                            "image": "python:3.12.8-slim",
                            "resources": {
                                "requests": {"memory": "32Mi"},
                                "limits": {"memory": "64Mi"},
                            },
                            "env": [
                                {
                                    "name": "API_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "credentials",
                                            "key": "api-token",
                                            "optional": False,
                                        }
                                    },
                                },
                                {
                                    "name": "DB_PASSWORD",
                                    "value": "raw-db-password-canary",
                                },
                                {
                                    "name": "APP_MODE",
                                    "value": "production",
                                },
                            ],
                        }
                    ]
                }
            }
        },
    }

    cleaned = sanitize_resource(deployment)
    assert cleaned is not None
    serialized = json.dumps(cleaned)
    assert "managedFields" not in serialized
    assert "last-applied-configuration" not in serialized
    assert "should-be-dropped" not in serialized
    assert "raw-db-password-canary" not in serialized
    assert cleaned["metadata"]["annotations"] == {"deployment.kubernetes.io/revision": "2"}

    container = cleaned["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"memory": "32Mi"},
        "limits": {"memory": "64Mi"},
    }
    assert container["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "credentials",
        "key": "api-token",
        "optional": False,
    }
    assert container["env"][1]["value"] == "[REDACTED]"
    assert container["env"][2]["value"] == "production"
