from __future__ import annotations

from pathlib import Path

from coding_agent.observability.metrics import AuditLogger, MetricsCollector
from coding_agent.security.safety import check_bash_safety, scan_and_redact_secrets, scan_pii


def test_secret_redaction_masks_key() -> None:
    result = scan_and_redact_secrets("token sk-abcdefghijklmnopqrstuvwxyz123456")
    assert result.redaction_count == 1
    assert "[REDACTED]" in result.redacted_text


def test_pii_scan_detects_email() -> None:
    assert "email address" in scan_pii("contact me at user@example.com")


def test_bash_safety_blocks_rm_rf_root() -> None:
    verdict = check_bash_safety("rm -rf /")
    assert verdict.blocked is True
    assert verdict.level == "critical"


def test_bash_safety_warns_force_push() -> None:
    verdict = check_bash_safety("git push --force origin main")
    assert verdict.warning is True


def test_audit_logger_writes_jsonl(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path, enabled=True)
    logger.log({"type": "security_event", "event_type": "secret_redacted"})
    files = sorted((tmp_path / ".yucode" / "audit").glob("*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "secret_redacted" in content


def test_metrics_collector_records_security_events(tmp_path: Path) -> None:
    metrics = MetricsCollector(audit_logger=AuditLogger(tmp_path, enabled=True))
    metrics.record_security_event("permission_denied", "bash", "blocked")
    assert len(metrics.security_events) == 1
    assert metrics.security_events[0].event_type == "permission_denied"
