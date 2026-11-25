"""GitHub Actions workflow validation.

A workflow is production code in this project, but it is not executed by pytest.
These tests catch the failures that otherwise only appear after a deploy:

* a cron that drifted from ``config/settings.yaml``;
* a missing ``PYTHONPATH``, which makes ``python -m ai_radar`` fail to import;
* a required secret that is never passed in (the free.ai regression: the provider
  was documented as required but no workflow supplied ``FREEAI_API_KEY``, so the
  preferred provider would silently never run);
* a workflow that grants write permissions;
* a secret interpolated into shell output.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_radar.settings import load_config

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
SCHEDULED = ("research.yml", "digest.yml", "backup.yml", "maintenance.yml")


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _triggers(data: dict) -> dict:
    # PyYAML resolves the bare key ``on:`` to boolean True (YAML 1.1).
    return data.get("on", data.get(True)) or {}


def _jobs(data: dict) -> dict:
    return data.get("jobs") or {}


def test_every_workflow_exists_and_parses():
    for name in (*SCHEDULED, "tests.yml"):
        path = WORKFLOW_DIR / name
        assert path.exists(), name
        assert isinstance(_load(name), dict), name


@pytest.mark.parametrize("name", SCHEDULED)
def test_schedule_matches_the_policy_file(name: str):
    config = load_config()
    expected = {
        "research.yml": config.policy.schedule.research.cron_utc,
        "digest.yml": config.policy.schedule.digest.cron_utc,
        "backup.yml": config.policy.schedule.backup.cron_utc,
        "maintenance.yml": config.policy.schedule.maintenance.cron_utc,
    }[name]
    schedule = _triggers(_load(name)).get("schedule") or []
    crons = [entry.get("cron") for entry in schedule if isinstance(entry, dict)]
    assert expected in crons, f"{name} crons {crons} do not contain {expected!r}"


@pytest.mark.parametrize("name", (*SCHEDULED, "tests.yml"))
def test_cli_jobs_make_the_package_importable(name: str):
    # requirements.lock does not install the package itself, so `python -m
    # ai_radar` only works when src/ is on the path.
    data = _load(name)
    for job_name, job in _jobs(data).items():
        steps = job.get("steps") or []
        runs_cli = any(
            "python -m ai_radar" in str(step.get("run", "")) for step in steps
        )
        if not runs_cli:
            continue
        env = job.get("env") or {}
        assert env.get("PYTHONPATH") == "src", f"{name}:{job_name} is missing PYTHONPATH"


def test_research_workflow_supplies_the_provider_secrets():
    env = _jobs(_load("research.yml"))["research"]["env"]
    # The adapter is only constructed when its key is present, so a secret that is
    # documented but not passed here disables that provider entirely.
    for secret in (
        "DATABASE_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "FREEAI_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "SARVAM_API_KEY",
        "HEALTHCHECKS_PING_URL",
        "GITHUB_TOKEN",
    ):
        assert env.get(secret) == f"${{{{ secrets.{secret} }}}}", secret


def test_workflows_never_request_write_permissions():
    for name in (*SCHEDULED, "tests.yml"):
        data = _load(name)
        permissions = data.get("permissions") or {}
        assert permissions.get("contents") == "read", name
        assert "write" not in str(permissions), name


def test_workflows_never_echo_a_secret():
    for name in (*SCHEDULED, "tests.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("echo", "- echo", "printf")):
                continue
            assert "secrets." not in stripped, f"{name} prints a secret: {stripped}"


def test_scheduled_workflows_cannot_cancel_a_run_in_progress():
    # Killing a run mid-flight can leave a delivery claimed but undecided.
    for name in SCHEDULED:
        concurrency = _load(name).get("concurrency") or {}
        assert concurrency.get("cancel-in-progress") is False, name


def test_scheduled_jobs_are_gated_behind_an_explicit_variable():
    # Pushing the repository must not start the cadence by itself. Every
    # schedule-driven workflow has to check for an opt-in repository variable,
    # and the check must be "== true" so the absent-variable case is OFF.
    for name in SCHEDULED:
        gated = [
            (job_name, job)
            for job_name, job in _jobs(_load(name)).items()
            if "SCHEDULES_ENABLED" in str(job.get("if") or "")
        ]
        assert gated, f"{name} has no deployment gate"
        for job_name, job in gated:
            condition = str(job["if"])
            assert "github.event_name != 'schedule'" in condition, f"{name}:{job_name}"
            assert "vars.SCHEDULES_ENABLED == 'true'" in condition, f"{name}:{job_name}"
            # A negated comparison would default to ENABLED, which is the bug.
            assert "!= 'false'" not in condition, f"{name}:{job_name}"


def test_manual_dispatch_defaults_to_no_notifications():
    for name in ("research.yml", "digest.yml"):
        inputs = (_triggers(_load(name)).get("workflow_dispatch") or {}).get("inputs") or {}
        dry_run = inputs.get("dry_run") or {}
        assert dry_run.get("default") is True, (
            f"{name}: a manual dispatch must default to dry_run=true so it cannot "
            "notify anyone by accident"
        )


def test_no_scheduled_job_runs_the_destructive_suite():
    for name in SCHEDULED:
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert "pytest" not in text, f"{name} runs tests in production"


# --------------------------------------------------- production migration gate
def test_ordinary_ci_never_applies_a_migration():
    # A push to main must not be able to change the production schema.
    for name in ("tests.yml", *SCHEDULED):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert "python -m ai_radar migrate" not in text, f"{name} migrates automatically"
    assert "migrations" not in _jobs(_load("tests.yml"))


def test_ordinary_ci_has_no_database_credential():
    """CI must not be able to reach production, even by accident."""
    for job_name, job in _jobs(_load("tests.yml")).items():
        assert "DATABASE_URL" not in (job.get("env") or {}), job_name
        for step in job.get("steps") or []:
            step_env = str(step.get("env") or {})
            assert "secrets." not in step_env, f"{job_name} exposes a secret"
            assert "DATABASE_URL" not in step_env, job_name


def test_only_a_manual_approval_gated_workflow_can_migrate():
    data = _load("migrate-production.yml")
    assert set(_triggers(data)) == {"workflow_dispatch"}
    job = _jobs(data)["migrate"]
    # GitHub Environments provide the required-reviewer gate.
    assert job["environment"] == "production"
    # And the run still requires a typed confirmation.
    assert "inputs.confirm" in str(job["if"])


def test_production_migration_takes_a_verified_backup_first():
    job = _jobs(_load("migrate-production.yml"))["migrate"]
    runs = [str(step.get("run", "")) for step in job["steps"]]
    backup_at = next(i for i, run in enumerate(runs) if "ai_radar backup" in run)
    migrate_at = next(i for i, run in enumerate(runs) if "ai_radar migrate" in run)
    assert backup_at < migrate_at
    # The backup step must be able to upload and therefore must fail when Drive
    # credentials are absent.
    env = job["env"]
    for name in (
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
    ):
        assert env.get(name) == f"${{{{ secrets.{name} }}}}", name


def test_production_migration_does_not_echo_its_credential():
    text = (WORKFLOW_DIR / "migrate-production.yml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("echo", "- echo", "printf")):
            # Naming the variable is fine; interpolating its value is not.
            assert "secrets." not in stripped and "${{ " not in stripped


# ---------------------------------------------------------- failure visibility
@pytest.mark.parametrize("name", SCHEDULED)
def test_scheduled_workflows_signal_a_failure(name: str):
    steps = [
        step
        for job in _jobs(_load(name)).values()
        for step in (job.get("steps") or [])
    ]
    failure_steps = [step for step in steps if "failure()" in str(step.get("if") or "")]
    assert failure_steps, f"{name} fails silently"
    assert any(
        "HEALTHCHECKS_PING_URL" in (step.get("env") or {}) for step in failure_steps
    ), f"{name} has no monitor to signal"
    # The URL can disable monitoring, so it must never be printed.
    for step in failure_steps:
        assert "echo" not in str(step.get("run", ""))


def test_research_blocks_on_database_health_instead_of_advising():
    steps = _jobs(_load("research.yml"))["research"]["steps"]
    blocking = [
        step for step in steps if "doctor --database-only" in str(step.get("run") or "")
    ]
    assert blocking, "research.yml must gate on the database checks"
    assert all(step.get("continue-on-error") is not True for step in blocking)
