# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Queue fairness: a waiting whole-box request must not starve behind
smaller ones, and a freed unit must not sit behind a claim window that the
head of the queue cannot use.

Before this, the queue was enforced only by the 90-second claim window opened
at release, and only for the head ticket:

* a head that wanted 4 chips got a window when one board freed, could not use
  it, blocked every other waiter for 90 s, and was then sent to the back;
* with no window open, a newcomer took a freed board straight past the queue.

So a `--chips all` request could wait for ever on a box kept busy by 2-chip
jobs. Now an *active* ticket (its waiter polled recently) reserves the free
units it would take, in FIFO order, and later requests allocate around those
reservations.
"""
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from gozer import queue as queue_mod
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster, Grant
from conftest import QUIETBOX


def fake_proc(tmp_path, pids=(1,)):
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        (root / str(pid) / "fd").mkdir(parents=True)
        (root / str(pid) / "comm").write_text("python\n")
    return str(root)


def make(tmp_path, sysfs):
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=fake_proc(tmp_path))
    km = Keymaster(gk)
    # Never the real tt-smi: QUIETBOX's BDFs are a real QuietBox's BDFs.
    km.reset_runner = lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "", "")
    return km, gk


def _expire_claim_window(gk):
    path = gk.queue._window_path()
    if os.path.exists(path):
        old = time.time() - queue_mod.CLAIM_WINDOW_SECONDS - 5
        os.utime(path, (old, old))


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_ticket(gk, ticket, seconds, field):
    """Rewrite one timestamp on a ticket to `seconds` ago."""
    path = gk.queue._ticket_path(ticket)
    import json
    with open(path) as f:
        rec = json.load(f)
    rec[field] = _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))
    with open(path, "w") as f:
        json.dump(rec, f)


def test_whole_box_waiter_keeps_a_freed_board_from_a_newcomer(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    b = km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    assert not isinstance(big, Grant)

    km.release(a.lease_id)
    # The window used to go to `big` here, which cannot use one board; once
    # it expired, `big` went to the back and a newcomer took the board.
    _expire_claim_window(gk)
    small = km.acquire("1", who="claude:small", pid=1)
    assert not isinstance(small, Grant)

    km.release(b.lease_id)
    got = km.acquire("all", who="claude:big", ticket=big["ticket"])
    assert isinstance(got, Grant)
    assert len(got.bdfs) == 4


def test_whole_box_waiter_is_not_sent_to_the_back(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    later = km.acquire("1", who="claude:later", pid=1)

    km.release(a.lease_id)
    # No window for a head that cannot fit: nothing to expire, so nothing
    # gets demoted behind `later`.
    assert gk.queue.may_claim(later["ticket"]) is True
    assert [e["ticket"] for e in gk.queue_entries()] == [
        big["ticket"], later["ticket"]]
    # And `later` still cannot take the board `big` has reserved.
    assert not isinstance(
        km.acquire("1", who="claude:later", ticket=later["ticket"]), Grant)


def test_window_goes_to_the_first_ticket_that_fits(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    exact_busy = km.acquire("1", who="claude:wants-b", pid=1,
                            exact="0000:03:00.0")  # board b, still held
    fits = km.acquire("1", who="claude:fits", pid=1)

    km.release(a.lease_id)
    # wants-b is ahead but waits on a different board; it neither gets the
    # window nor reserves the freed board.
    assert gk.queue.may_claim(fits["ticket"]) is True
    assert gk.queue.may_claim(exact_busy["ticket"]) is False
    got = km.acquire("1", who="claude:fits", ticket=fits["ticket"])
    assert isinstance(got, Grant)


def test_inactive_ticket_does_not_reserve(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    km.acquire("1", who="claude:b", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    # Its waiter has not polled for longer than the reservation lasts.
    for field in ("since", "seen_at"):
        _age_ticket(gk, big["ticket"], queue_mod.RESERVE_ACTIVE_SECONDS + 60,
                    field)

    km.release(a.lease_id)
    _expire_claim_window(gk)
    small = km.acquire("1", who="claude:small", pid=1)
    assert isinstance(small, Grant)
    # Still queued, at the front: it lost the reservation, not its place.
    assert gk.queue_position(big["ticket"]) == 1


def test_a_ticket_that_can_never_fit_does_not_reserve(tmp_path, sysfs):
    """A head asking for more than the healthy box can ever hold must not
    park the healthy boards for ever."""
    km, gk = make(tmp_path, sysfs)
    a = km.acquire("1", who="claude:a", pid=1)
    big = km.acquire("all", who="claude:big", pid=1)
    # Pretend board b is degraded: no longer grantable to a normal request.
    gk.free_units = lambda include_degraded=False: (
        [u for u in Gatekeeper.free_units(gk, include_degraded)
         if u != "0000000000000001"])
    gk.grantable_units = lambda: ["0000000000000002"]
    km.release(a.lease_id)
    assert isinstance(km.acquire("1", who="claude:small", pid=1), Grant)
    assert gk.queue_position(big["ticket"]) == 1


def test_polling_refreshes_a_ticket_so_it_does_not_age_out(tmp_path, sysfs):
    """Known limitation closed: a waiter polling correctly behind a
    multi-hour lease used to lose its ticket at the one-hour mark."""
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:a", pid=1)
    t = km.acquire("1", who="claude:waiter", pid=1)
    # Enqueued over an hour ago, polled a few minutes ago.
    _age_ticket(gk, t["ticket"], queue_mod.TICKET_MAX_AGE_SECONDS + 60, "since")
    _age_ticket(gk, t["ticket"], 300, "seen_at")
    gk.prune_queue()
    assert gk.queue_position(t["ticket"]) == 1


def test_a_replayed_ticket_refreshes_seen_at(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:a", pid=1)
    t = km.acquire("1", who="claude:waiter", pid=1)
    _age_ticket(gk, t["ticket"], 300, "seen_at")
    before = gk.queue_entries()[0]["seen_at"]
    km.acquire("1", who="claude:waiter", ticket=t["ticket"])   # a poll
    assert gk.queue_entries()[0]["seen_at"] > before


def test_unpolled_ticket_still_ages_out(tmp_path, sysfs):
    km, gk = make(tmp_path, sysfs)
    km.acquire("all", who="claude:a", pid=1)
    t = km.acquire("1", who="claude:waiter", pid=1)
    _age_ticket(gk, t["ticket"], queue_mod.TICKET_MAX_AGE_SECONDS + 60, "since")
    _age_ticket(gk, t["ticket"], queue_mod.TICKET_MAX_AGE_SECONDS + 60, "seen_at")
    gk.prune_queue()
    assert gk.queue_position(t["ticket"]) is None
