"""Opaque runtime attestation requires absolute raw home identities (governance#2027)."""

import pytest

from gateway import control_socket, status


@pytest.fixture
def attestation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _: 100)
    monkeypatch.setattr(status, "_pid_exists", lambda _: True)
    monkeypatch.setattr(status, "_read_process_cmdline", lambda _: "python /fixture/wrapper.py")
    record = {
        "pid": 4242, "start_time": 100, "kind": "hermes-gateway",
        "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
        "gateway_state": "running", "hermes_home": str(tmp_path),
    }
    live = {
        "pid": 4242, "start_time": 100, "kind": "hermes-gateway",
        "protocol": 1, "hermes_home": str(tmp_path),
    }
    monkeypatch.setattr(control_socket, "identify_gateway", lambda *_args, **_kwargs: live)
    monkeypatch.setattr(status, "is_gateway_runtime_lock_active", lambda *_: True)
    monkeypatch.setattr(status, "_read_pid_record", lambda *_: record)
    monkeypatch.setattr(status, "_read_gateway_lock_record", lambda *_: record)
    return tmp_path, record, live


def caller_results(home, record):
    return (
        status._record_matches_live_gateway_pid(record, 4242, expected_home=home),
        status.get_runtime_status_running_pid(record, expected_home=home),
        status.get_running_pid(home / "gateway.pid", cleanup_stale=False),
    )


@pytest.mark.parametrize("side", ["record", "live"])
@pytest.mark.parametrize("value", [".", "", " ", "relative", "~/home", None, 1, {}, []])
def test_raw_home_must_be_absolute_nonempty_string(attestation, side, value):
    home, record, live = attestation
    assert caller_results(home, record) == (True, 4242, 4242)
    (record if side == "record" else live)["hermes_home"] = value
    assert caller_results(home, record) == (False, None, None)


@pytest.mark.parametrize("alias", [False, True])
def test_absolute_home_and_physical_alias_remain_valid(attestation, alias):
    home, record, live = attestation
    if alias:
        link = home / "alias"
        link.symlink_to(home, target_is_directory=True)
        record["hermes_home"] = live["hermes_home"] = str(link)
    assert caller_results(home, record) == (True, 4242, 4242)


@pytest.mark.parametrize("field,value", [
    ("pid", 4243), ("start_time", 101), ("kind", "other"),
    ("protocol", 2), ("hermes_home", "/unserved-fixture-home"),
])
def test_live_identity_mismatch_still_refuses(attestation, field, value):
    home, record, live = attestation
    live[field] = value
    assert caller_results(home, record) == (False, None, None)
