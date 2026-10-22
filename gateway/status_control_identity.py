"""Home and live control identity checks; bind to the status reader namespace."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Optional

def canonical_hermes_home(path: Path | str) -> Path:
    """Stable absolute HERMES_HOME path for persisted identity data."""
    return Path(path).expanduser().resolve(strict=False)


def bind_status_identity(api):
    def _same_hermes_home(left: Path | str, right: Path | str) -> bool:
        """Compare HERMES_HOME paths with the host platform's case semantics."""
        left_c = os.path.normcase(str(api._canonical_hermes_home(left)))
        return left_c == os.path.normcase(str(api._canonical_hermes_home(right)))

    def _record_matches_control_identity(
        record: dict[str, Any], pid: int, expected_home: Optional[Path]
    ) -> bool:
        """An argv-opaque runtime needs a live, home- and incarnation-bound socket attestation."""
        if not api._record_looks_like_gateway(record):
            return False
        expected_home = expected_home if expected_home is not None else api._get_process_hermes_home()
        start = record.get("start_time")
        if record.get("pid") != pid or start is None or api._get_process_start_time(pid) != start:
            return False
        record_home = record.get("hermes_home")
        if not isinstance(record_home, str) or not record_home.strip() or not Path(record_home).is_absolute():
            return False
        try:
            home = api._canonical_hermes_home(expected_home)
            if api._canonical_hermes_home(Path(record_home)) != home:
                return False
            from gateway.control_socket import identify_gateway
            live = identify_gateway(home, timeout=0.5)
            live_home = live.get("hermes_home") if isinstance(live, dict) else None
            return bool(
                live and live.get("protocol") == 1 and live.get("kind") == api._GATEWAY_KIND
                and live.get("pid") == pid and live.get("start_time") == start
                and isinstance(live_home, str) and live_home.strip() and Path(live_home).is_absolute()
                and api._canonical_hermes_home(Path(live_home)) == home
            )
        except (OSError, TypeError, ValueError, ImportError):
            return False

    return _same_hermes_home, _record_matches_control_identity
