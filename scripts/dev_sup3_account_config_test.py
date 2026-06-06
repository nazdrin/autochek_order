from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import orchestrator  # noqa: E402


def _expect_runtime_error(label: str, fn, expected: str) -> None:
    try:
        fn()
    except RuntimeError as e:
        msg = str(e)
        if expected not in msg:
            raise AssertionError(f"{label}: expected error containing {expected!r}, got {msg!r}") from e
        print(f"{label} failed as expected")
        return
    raise AssertionError(f"{label}: expected RuntimeError")


def main() -> int:
    original = {
        "ORCH_SUP3_STORAGE_STATE_FILE": orchestrator.ORCH_SUP3_STORAGE_STATE_FILE,
        "ORCH_SUP3_USE_CDP": orchestrator.ORCH_SUP3_USE_CDP,
        "ORCH_SUP3_ORG2_LOGIN_EMAIL": orchestrator.ORCH_SUP3_ORG2_LOGIN_EMAIL,
        "ORCH_SUP3_ORG2_LOGIN_PASSWORD": orchestrator.ORCH_SUP3_ORG2_LOGIN_PASSWORD,
        "ORCH_SUP3_ORG2_STORAGE_STATE_FILE": orchestrator.ORCH_SUP3_ORG2_STORAGE_STATE_FILE,
    }
    try:
        orchestrator.ORCH_SUP3_STORAGE_STATE_FILE = ".state_supplier3.json"
        orchestrator.ORCH_SUP3_USE_CDP = "0"
        orchestrator.ORCH_SUP3_ORG2_LOGIN_EMAIL = "olegtsimko123@gmail.com"
        orchestrator.ORCH_SUP3_ORG2_LOGIN_PASSWORD = "test-password"
        orchestrator.ORCH_SUP3_ORG2_STORAGE_STATE_FILE = ".state_supplier3_org2.json"

        org1 = orchestrator.resolve_sup3_account_config({"organizationId": 1})
        assert org1["organization_id"] == "1", org1
        assert org1["storage_state_file"] == ".state_supplier3.json", org1
        assert org1["login_email"] == "", org1
        assert org1["login_password"] == "", org1
        print("organizationId=1 keeps current SUP3 account")

        default_org = orchestrator.resolve_sup3_account_config({})
        assert default_org["organization_id"] == "default", default_org
        assert default_org["storage_state_file"] == ".state_supplier3.json", default_org
        assert default_org["login_email"] == "", default_org
        assert default_org["login_password"] == "", default_org
        print("missing organizationId keeps current SUP3 account")

        org2 = orchestrator.resolve_sup3_account_config({"organizationId": 2})
        assert org2["organization_id"] == "2", org2
        assert org2["storage_state_file"] == ".state_supplier3_org2.json", org2
        assert org2["use_cdp"] == "0", org2
        assert org2["login_email"] == "olegtsimko123@gmail.com", org2
        assert org2["login_password"] == "test-password", org2
        print("organizationId=2 selects isolated DSN account")

        orchestrator.ORCH_SUP3_ORG2_LOGIN_PASSWORD = ""
        _expect_runtime_error(
            "organizationId=2 without password",
            lambda: orchestrator.resolve_sup3_account_config({"organizationId": 2}),
            "ORCH_SUP3_ORG2_LOGIN_PASSWORD",
        )

        orchestrator.ORCH_SUP3_ORG2_LOGIN_PASSWORD = "test-password"
        orchestrator.ORCH_SUP3_USE_CDP = "1"
        _expect_runtime_error(
            "organizationId=2 with CDP",
            lambda: orchestrator.resolve_sup3_account_config({"organizationId": 2}),
            "ORCH_SUP3_USE_CDP=0",
        )

        orchestrator.ORCH_SUP3_USE_CDP = "0"
        _expect_runtime_error(
            "unknown organizationId",
            lambda: orchestrator.resolve_sup3_account_config({"organizationId": 3}),
            "Unsupported SUP3 organizationId",
        )

        _expect_runtime_error(
            "invalid organizationId",
            lambda: orchestrator.resolve_sup3_account_config({"organizationId": "abc"}),
            "Invalid SUP3 organizationId",
        )

        return 0
    finally:
        for key, value in original.items():
            setattr(orchestrator, key, value)


if __name__ == "__main__":
    raise SystemExit(main())
