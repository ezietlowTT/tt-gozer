# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""A supervised lease whose own processes hold a device outside the grant.

gozer only advises: a server set up for four devices on a one-board grant
opens the other board anyway. With several models side by side that is the
collision that matters, so `status`, `reconcile` and `gozer run` name it.
"""
import json
import subprocess
import sys

from gozer import cli, history
from gozer import keymaster as keymaster_mod
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster
from conftest import QUIETBOX, fake_proc_tree

OWNER, CHILD, STRANGER = 100, 101, 300


def make(tmp_path, sysfs, procs):
    proc = fake_proc_tree(tmp_path, procs)
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=proc)
    km = Keymaster(gk)
    km.reset_runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
    return km, gk


def test_a_child_of_the_owner_on_another_board_is_reported(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs, {1: {}, OWNER: {"ppid": 1}})
    mine = km.acquire("1", who="claude:server", pid=OWNER)       # board of dev 0,1
    other = km.acquire("1", who="claude:other", pid=1)            # board of dev 2,3
    # Then the server's worker opens its own chip and the other board's.
    fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER, "devs": [0, 2]}})
    found = gk.fence_violations()
    assert [(v["lease_id"], v["pid"], v["dev_index"]) for v in found] == [
        (mine.lease_id, CHILD, 2)]
    assert found[0]["held_by"] == other.lease_id
    assert found[0]["bdf"] == "0000:03:00.0"


def test_devices_inside_the_grant_and_strangers_are_not_violations(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs, {
        1: {}, OWNER: {"ppid": 1, "devs": [0, 1]},
        STRANGER: {"ppid": 1, "devs": [3]}})
    km.acquire("1", who="claude:server", pid=OWNER)
    assert gk.fence_violations() == []


def test_a_detached_lease_has_nobody_to_judge(tmp_path, sysfs):
    """A detached lease records the pid of the one-shot CLI that took it,
    which is gone (or reused) moments later; judging by it would blame
    whatever process now has that number."""
    import os
    km, gk = make(tmp_path, sysfs, {1: {}})
    lease = km.acquire("1", who="claude:detached")               # no pid: detached
    assert gk.read_lease(lease.lease_id)["detached"] is True
    recorded = gk.read_lease(lease.lease_id)["pid"]
    assert recorded == os.getpid()
    fake_proc_tree(tmp_path, {recorded: {"ppid": 1, "devs": [2]}})
    assert gk.fence_violations() == []


def test_lease_id_limits_the_check(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs, {
        1: {}, OWNER: {"ppid": 1, "devs": [2]}})
    mine = km.acquire("1", who="claude:server", pid=OWNER)
    assert gk.fence_violations(lease_id="nope") == []
    assert len(gk.fence_violations(lease_id=mine.lease_id)) == 1


def _cli_env(monkeypatch, gk):
    monkeypatch.setenv("GOZER_ROOT", gk.root)
    monkeypatch.setenv("GOZER_SYSFS_ROOT", gk.sysfs_root)
    monkeypatch.setenv("GOZER_PROC_ROOT", gk.proc_root)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")


def test_status_names_the_breach_and_stays_read_only(tmp_path, sysfs, monkeypatch, capsys):
    km, gk = make(tmp_path, sysfs, {
        1: {}, OWNER: {"ppid": 1}, CHILD: {"ppid": OWNER, "devs": [2]}})
    mine = km.acquire("1", who="claude:server", pid=OWNER)
    _cli_env(monkeypatch, gk)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"pid {CHILD} of lease {mine.lease_id} (claude:server) is outside its grant" in out
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fence_violations"][0]["dev_index"] == 2
    assert not [r for r in history.read(gk.history_root)
                if r["event"] == "fence_violation"]


def test_reconcile_reports_and_logs_it(tmp_path, sysfs, monkeypatch, capsys):
    km, gk = make(tmp_path, sysfs, {
        1: {}, OWNER: {"ppid": 1}, CHILD: {"ppid": OWNER, "devs": [3]}})
    km.acquire("1", who="claude:server", pid=OWNER)
    _cli_env(monkeypatch, gk)
    assert cli.main(["reconcile"]) == 0
    assert "outside its grant" in capsys.readouterr().out
    logged = [r for r in history.read(gk.history_root)
              if r["event"] == "fence_violation"]
    assert logged and logged[0]["dev_index"] == 3


def test_run_warns_once_per_device_and_leaves_the_command_running(
        tmp_path, sysfs, monkeypatch, capsys):
    km, gk = make(tmp_path, sysfs, {1: {}})
    monkeypatch.setattr(keymaster_mod, "FENCE_CHECK_SECONDS", (0.1, 0.2, 0.3))
    calls = []

    def fake_violations(fd_map=None, lease_id=None):
        calls.append(lease_id)
        return [{"lease_id": lease_id, "who": "run:me", "pid": 4242,
                 "dev_index": 2, "bdf": "0000:03:00.0",
                 "held_by": None, "held_by_who": None}]
    monkeypatch.setattr(gk, "fence_violations", fake_violations)
    rc = km.run([sys.executable, "-c", "import time; time.sleep(0.6)"],
                chips_spec="1", who="run:me", pid=1)
    assert rc == 0                                  # not killed
    assert len(calls) == 3                          # checked at each point
    err = capsys.readouterr().err
    assert err.count("WARNING: pid 4242 opened device 2") == 1
    logged = [r for r in history.read(gk.history_root)
              if r["event"] == "fence_violation"]
    assert len(logged) == 1


def test_a_failing_check_never_breaks_run(tmp_path, sysfs, monkeypatch):
    km, gk = make(tmp_path, sysfs, {1: {}})
    monkeypatch.setattr(keymaster_mod, "FENCE_CHECK_SECONDS", (0.05,))

    def boom(**kw):
        raise OSError("proc vanished")
    monkeypatch.setattr(gk, "fence_violations", boom)
    rc = km.run([sys.executable, "-c", "import time; time.sleep(0.2)"],
                chips_spec="1", who="run:me", pid=1)
    assert rc == 0


def test_history_keeps_a_long_event_name_apart_from_its_fields():
    line = cli._format_history_line(
        {"ts": "2026-11-11T17:20:40Z", "event": "fence_violation", "pid": 1})
    assert "fence_violation pid=1" in line
    short = cli._format_history_line(
        {"ts": "T", "event": "released", "pid": 1})
    assert short == "T  released  pid=1"
