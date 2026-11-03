# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Stable names for boards and chips (gozer/names.py, `gozer names`).

Every serial, ASIC id and PCI address here is made up. The fixtures are the
shared QUIETBOX tree (board ...02 holds device indices 0 and 1, board ...01
holds 2 and 3) plus a three-board tree of this file's own, so file order, serial
order and device-index order can all be told apart.
"""
import builtins
import json
import os
import re

import pytest

from conftest import GALAXY_LIKE, QUIETBOX, SINGLE_CHIP_BOARDS, build_sysfs
from gozer import names
from gozer.cli import main
from gozer.gatekeeper import Gatekeeper
from gozer.names import NamesError

S1, S2 = "0000000000000001", "0000000000000002"

# Three p300c boards. Serial order is S-A, S-B, S-C; device-index order is
# S-C, S-B, S-A (the opposite), so a rule that quietly fell back to either one
# would not pass for the file's own order.
THREE = [
    {"dev_index": 0, "bdf": "0000:01:00.0", "serial": "S-C", "asic_id": "c0", "card": "p300c"},
    {"dev_index": 1, "bdf": "0000:02:00.0", "serial": "S-C", "asic_id": "c1", "card": "p300c"},
    {"dev_index": 2, "bdf": "0000:03:00.0", "serial": "S-B", "asic_id": "b0", "card": "p300c"},
    {"dev_index": 3, "bdf": "0000:04:00.0", "serial": "S-B", "asic_id": "b1", "card": "p300c"},
    {"dev_index": 4, "bdf": "0000:05:00.0", "serial": "S-A", "asic_id": "a0", "card": "p300c"},
    {"dev_index": 5, "bdf": "0000:06:00.0", "serial": "S-A", "asic_id": "a1", "card": "p300c"},
]

EXAMPLE = f"""\
# The order of the [[board]] tables is the board order.
[[board]]
name = "left"
serial = "{S2}"

[[board]]
name = "right"
serial = "{S1}"
"""


@pytest.fixture
def box(tmp_path, sysfs, monkeypatch):
    """A QUIETBOX with a names file at tmp/names.toml (not yet written)."""
    (tmp_path / "proc").mkdir()
    monkeypatch.setenv("GOZER_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GOZER_SYSFS_ROOT", sysfs(QUIETBOX))
    monkeypatch.setenv("GOZER_PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    monkeypatch.setenv("GOZER_NAMES", str(tmp_path / "names.toml"))
    return tmp_path


def write_names(box, text):
    (box / "names.toml").write_text(text)


def run(argv, capsys):
    code = main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def use_tree(box, chips, monkeypatch, sub="sys2"):
    root = build_sysfs(str(box / sub), chips)
    monkeypatch.setenv("GOZER_SYSFS_ROOT", root)


def state_snapshot(box):
    """Everything under the gate state dir, so a refused command can be shown
    to have left it alone."""
    out = {}
    for base, _, files in os.walk(box / "state"):
        for f in files:
            if f == "history.jsonl":
                continue
            p = os.path.join(base, f)
            out[p] = open(p).read()
        out[base] = None
    return out


# ---- the file --------------------------------------------------------------


def test_example_file_parses():
    nf = names.parse(EXAMPLE)
    assert [(b.name, b.serial) for b in nf.boards] == [("left", S2), ("right", S1)]


def test_chip_pin_parses():
    nf = names.parse(EXAMPLE + '\n[[chip]]\nboard = "right"\nposition = 0\nasic = "x1"\n')
    assert [(p.board, p.position, p.asic) for p in nf.chips] == [("right", 0, "x1")]


@pytest.mark.parametrize("name", ["all", "0", "12", "0000:03:00.0", "03:00.0",
                                  "deadbeef", "abcdef0123456789"])
def test_reserved_names_are_refused_as_reserved(name):
    with pytest.raises(NamesError) as e:
        names.parse(f'[[board]]\nname = "{name}"\nserial = "s"\n')
    assert e.value.kind == "reserved-name"


@pytest.mark.parametrize("name", ["Left", "left.0", "1left", "k" * 33, "le ft", "-x", ""])
def test_malformed_names_are_refused_as_invalid(name):
    with pytest.raises(NamesError) as e:
        names.parse(f'[[board]]\nname = "{name}"\nserial = "s"\n')
    assert e.value.kind in ("invalid-name", "malformed")


def test_a_name_may_be_32_characters_and_use_digits_and_hyphens():
    names.parse('[[board]]\nname = "' + "a" + "b-1" * 10 + 'x"\nserial = "s"\n')
    names.parse('[[board]]\nname = "' + "k" * 32 + '"\nserial = "s"\n')


def test_duplicate_name_is_an_error_with_its_line():
    with pytest.raises(NamesError) as e:
        names.parse('[[board]]\nname = "x"\nserial = "a"\n\n'
                    '[[board]]\nname = "x"\nserial = "b"\n', "f.toml")
    assert e.value.kind == "duplicate-name"
    assert "f.toml:6" in e.value.message


def test_serial_listed_twice_is_an_error_even_in_another_case():
    with pytest.raises(NamesError) as e:
        names.parse('[[board]]\nname = "x"\nserial = "AB"\n'
                    '[[board]]\nname = "y"\nserial = "ab"\n', "f.toml")
    assert e.value.kind == "duplicate-serial"
    assert "f.toml:6" in e.value.message


def test_unknown_key_and_unknown_table_are_errors_with_line():
    with pytest.raises(NamesError) as e:
        names.parse('[[board]]\nname = "x"\nserial = "a"\ncolour = "red"\n', "f.toml")
    assert e.value.kind == "unknown-key" and "f.toml:4" in e.value.message
    for header in ("[machine]", "[[machine]]"):
        with pytest.raises(NamesError) as e:
            names.parse(f'{header}\nname = "x"\n', "f.toml")
        assert e.value.kind == "unknown-key" and "f.toml:1" in e.value.message


@pytest.mark.parametrize("text,line", [
    ('name = "x"\n', 1),                                   # key outside a table
    ('[[board]]\nname = x\nserial = "a"\n', 2),            # bare word
    ('[[board]]\nname = "x\nserial = "a"\n', 2),           # unclosed string
    ('[[board]]\nname = "x"\nname = "y"\nserial = "a"\n', 3),  # repeated key
    ('[[board]]\nname = ["x"]\nserial = "a"\n', 2),        # array
    ('[[board]]\nname = "x"\n', 1),                        # missing serial
    ('[[board]]\nname = "x"\nserial = ""\n', 3),           # empty serial
    ('this is not toml\n', 1),
])
def test_malformed_files_are_errors_naming_the_line(text, line):
    with pytest.raises(NamesError) as e:
        names.parse(text, "f.toml")
    assert e.value.kind == "malformed"
    assert f"f.toml:{line}" in e.value.message


def test_chip_pin_rules():
    base = EXAMPLE
    with pytest.raises(NamesError) as e:
        names.parse(base + '[[chip]]\nboard = "nope"\nposition = 0\nasic = "a"\n')
    assert e.value.kind == "unknown-board"
    with pytest.raises(NamesError) as e:
        names.parse(base + '[[chip]]\nboard = "left"\nposition = 0\nasic = "a"\n'
                           '[[chip]]\nboard = "right"\nposition = 1\nasic = "a"\n')
    assert e.value.kind == "duplicate-asic"
    with pytest.raises(NamesError) as e:
        names.parse(base + '[[chip]]\nboard = "left"\nposition = 0\nasic = "a"\n'
                           '[[chip]]\nboard = "left"\nposition = 0\nasic = "b"\n')
    assert e.value.kind == "duplicate-position"


def test_the_messages_for_each_kind_of_problem_differ():
    msgs = {}
    for kind, text in {
        "reserved": '[[board]]\nname = "all"\nserial = "s"\n',
        "invalid": '[[board]]\nname = "Up"\nserial = "s"\n',
        "dup-name": '[[board]]\nname="x"\nserial="a"\n[[board]]\nname="x"\nserial="b"\n',
        "dup-serial": '[[board]]\nname="x"\nserial="a"\n[[board]]\nname="y"\nserial="a"\n',
        "unknown-key": '[[board]]\nname="x"\nserial="a"\nzz="a"\n',
        "malformed": 'garbage\n',
    }.items():
        with pytest.raises(NamesError) as e:
            names.parse(text, "f")
        msgs[kind] = re.sub(r"f:\d+", "f:N", e.value.message)
    assert len(set(msgs.values())) == len(msgs)


def test_absent_file_is_an_empty_file(tmp_path):
    nf = names.load(str(tmp_path / "nothing.toml"))
    assert nf.boards == [] and nf.chips == []


def test_a_symbolic_link_is_not_followed(tmp_path):
    real = tmp_path / "real.toml"
    real.write_text(EXAMPLE)
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    with pytest.raises(NamesError) as e:
        names.load(str(link))
    assert e.value.kind == "unreadable-file" and "symbolic link" in e.value.message
    assert names.load(str(real)).boards      # the real file loads


def test_the_file_is_read_with_a_size_limit(tmp_path):
    big = tmp_path / "big.toml"
    big.write_text("# x\n" * (names.MAX_NAMES_FILE_BYTES // 4 + 10))
    with pytest.raises(NamesError) as e:
        names.load(str(big))
    assert "larger than" in e.value.message


def test_a_file_that_is_not_regular_is_refused(tmp_path):
    with pytest.raises(NamesError) as e:
        names.load(str(tmp_path))          # a directory
    assert e.value.kind == "unreadable-file"


def test_default_path_follows_the_environment(monkeypatch):
    monkeypatch.delenv("GOZER_NAMES")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    assert names.default_path() == "/cfg/gozer/names.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert names.default_path().endswith("/.config/gozer/names.toml")
    monkeypatch.setenv("GOZER_NAMES", "/elsewhere/n.toml")
    assert names.default_path() == "/elsewhere/n.toml"


# ---- order and identity ------------------------------------------------------


def gk_for(box):
    return Gatekeeper(root=str(box / "state"), proc_root=str(box / "proc"))


def test_boards_are_in_file_order_then_unnamed_boards_by_serial(box, monkeypatch):
    use_tree(box, THREE, monkeypatch)
    # File order (B, C) differs from serial order (A, B, C) and from device
    # index order (C, B, A); the unnamed board A comes last.
    write_names(box, '[[board]]\nname = "one"\nserial = "S-B"\n'
                     '[[board]]\nname = "two"\nserial = "S-C"\n')
    gk = gk_for(box)
    assert [b.serial for b in gk.boards] == ["S-B", "S-C", "S-A"]


def test_with_no_file_boards_stay_in_serial_order(box, monkeypatch):
    use_tree(box, THREE, monkeypatch)
    assert [b.serial for b in gk_for(box).boards] == ["S-A", "S-B", "S-C"]


def test_unnamed_boards_after_named_are_sorted_by_serial(box, monkeypatch):
    use_tree(box, THREE, monkeypatch)
    write_names(box, '[[board]]\nname = "only"\nserial = "S-B"\n')
    assert [b.serial for b in gk_for(box).boards] == ["S-B", "S-A", "S-C"]


def test_chips_in_a_board_are_in_pci_address_order(box, monkeypatch):
    chips = [dict(c) for c in QUIETBOX]
    # Hand the lower PCI address the higher device index.
    chips[2]["dev_index"], chips[3]["dev_index"] = 3, 2
    use_tree(box, chips, monkeypatch)
    write_names(box, f'[[board]]\nname = "right"\nserial = "{S1}"\n')
    nm = gk_for(box).names
    board = next(b for b in nm.ordered() if b.serial == S1)
    assert [c.bdf for c in board.chips] == ["0000:03:00.0", "0000:04:00.0"]
    assert nm.chip_name(board.chips[0]) == "right.0"


def test_a_chip_can_be_pinned_to_a_position_by_asic_id(box):
    write_names(box, EXAMPLE + '\n[[chip]]\nboard = "right"\nposition = 0\n'
                               'asic = "4444444444444444"\n')
    nm = gk_for(box).names
    board = next(b for b in nm.ordered() if b.serial == S1)
    # The chip at the higher PCI address now holds position 0.
    assert [c.asic_id for c in board.chips] == ["4444444444444444", "3333333333333333"]
    assert nm.chip_name(board.chips[0]) == "right.0"


def test_a_pinned_chip_keeps_its_name_when_it_moves_to_another_pci_slot(box, monkeypatch):
    pin = EXAMPLE + ('\n[[chip]]\nboard = "right"\nposition = 1\n'
                     'asic = "3333333333333333"\n')
    write_names(box, pin)
    before = gk_for(box).names
    chips = [dict(c) for c in QUIETBOX]
    chips[2]["bdf"], chips[3]["bdf"] = "0000:09:00.0", "0000:0a:00.0"
    use_tree(box, chips, monkeypatch)
    after = gk_for(box).names
    pinned = lambda nm: next(c for b in nm.ordered() for c in b.chips
                             if c.asic_id == "3333333333333333")
    assert before.chip_name(pinned(before)) == after.chip_name(pinned(after)) == "right.1"


def test_a_name_resolves_to_the_same_chip_after_the_indices_are_renumbered(
        box, monkeypatch, capsys):
    write_names(box, EXAMPLE)
    code, out1, _ = run(["names", "--json"], capsys)
    first = json.loads(out1)
    # Same hardware, driver hands out the indices in the opposite order.
    renumbered = [dict(c, dev_index=3 - c["dev_index"]) for c in QUIETBOX]
    use_tree(box, renumbered, monkeypatch)
    code, out2, _ = run(["names", "--json"], capsys)
    second = json.loads(out2)
    strip = lambda d: [(b["name"], b["serial"],
                        [(c["name"], c["bdf"], c["asic_id"]) for c in b["chips"]])
                       for b in d["boards"]]
    assert strip(first) == strip(second)
    assert [b["name"] for b in second["boards"]] == ["left", "right"]
    # ...and the index shown beside each name did change.
    assert ([c["dev_index"] for b in first["boards"] for c in b["chips"]]
            != [c["dev_index"] for b in second["boards"] for c in b["chips"]])


def test_a_named_board_that_is_absent_is_reported_not_fatal(box, capsys):
    write_names(box, EXAMPLE + '\n[[board]]\nname = "spare"\nserial = "NOT-HERE"\n')
    code, out, _ = run(["names"], capsys)
    assert code == 0
    assert "missing: spare" in out
    code, out, _ = run(["acquire", "--exact", "left", "--who", "t"], capsys)
    assert code == 0


def test_a_position_outside_a_present_board_is_an_error(box):
    write_names(box, EXAMPLE + '\n[[chip]]\nboard = "left"\nposition = 2\n'
                               'asic = "1111111111111111"\n')
    assert gk_for(box).names_error.kind == "position-outside"


# ---- addressing ----------------------------------------------------------------


def grant(box, capsys, *argv):
    code, out, err = run(["acquire", "--who", "t", "--json", *argv], capsys)
    return code, (json.loads(out) if out.strip() else None), err


def test_acquire_exact_board_name_leases_that_board(box, capsys):
    write_names(box, EXAMPLE)
    code, g, _ = grant(box, capsys, "--exact", "left")
    assert code == 0
    lease = gk_for(box).read_lease(g["lease_id"])
    assert lease["units"] == [S2]
    assert lease["chips"] == ["0000:01:00.0", "0000:02:00.0"]     # BDFs, not names


def test_acquire_exact_chip_name_leases_the_board_holding_that_chip(box, capsys):
    write_names(box, EXAMPLE)
    code, g, _ = grant(box, capsys, "--exact", "right.1")
    assert code == 0
    lease = gk_for(box).read_lease(g["lease_id"])
    # On a two-chip board the lease unit is the board (UMD shows both chips).
    assert lease["units"] == [S1]
    assert "0000:04:00.0" in lease["chips"]
    assert g["env"]["TT_VISIBLE_DEVICES"] == ",".join(g["chips"])


def test_at_chip_grain_a_chip_name_leases_one_chip(box, monkeypatch, capsys):
    use_tree(box, GALAXY_LIKE, monkeypatch)
    write_names(box, '[[board]]\nname = "gal"\nserial = "GGG"\n')
    code, g, _ = grant(box, capsys, "--exact", "gal.2")
    assert code == 0
    lease = gk_for(box).read_lease(g["lease_id"])
    assert lease["chips"] == ["0000:03:00.0"] and lease["units"] == ["0000:03:00.0"]


def test_at_chip_grain_a_board_name_with_one_chip_leases_that_chip(box, monkeypatch, capsys):
    use_tree(box, SINGLE_CHIP_BOARDS, monkeypatch)
    write_names(box, '[[board]]\nname = "solo"\nserial = "BBB"\n')
    code, g, _ = grant(box, capsys, "--exact", "solo")
    assert gk_for(box).read_lease(g["lease_id"])["chips"] == ["0000:02:00.0"]


def test_at_chip_grain_a_board_name_with_several_chips_asks_for_a_chip(box, monkeypatch, capsys):
    use_tree(box, GALAXY_LIKE, monkeypatch)
    write_names(box, '[[board]]\nname = "gal"\nserial = "GGG"\n')
    code, out, err = run(["acquire", "--exact", "gal", "--who", "t"], capsys)
    assert code == names_exit() and "name a chip" in err


def names_exit():
    from gozer.cli import EXIT_NAMES
    return EXIT_NAMES


def test_run_and_adopt_take_names_too(box, capsys, monkeypatch):
    write_names(box, EXAMPLE)
    code, out, err = run(["run", "--exact", "right", "--who", "t", "--",
                          "/bin/true"], capsys)
    assert code == 0, err
    # adopt: nothing holds the chip, so it gets as far as "no process".
    code, out, err = run(["adopt", "left.0", "--who", "t", "--json"], capsys)
    assert json.loads(out)["adopted"] is False
    code, out, err = run(["adopt", "left.0", "--who", "t"], capsys)
    assert "left.0 has no process holding it" in out


def test_a_queued_request_stores_the_resolved_serial_not_the_name(box, capsys):
    write_names(box, EXAMPLE)
    assert grant(box, capsys, "--exact", "left")[0] == 0
    code, q, _ = grant(box, capsys, "--exact", "left")
    assert code == 10
    entries = gk_for(box).queue_entries()
    assert entries[0]["exact"] == S2


def test_literals_still_work_when_a_names_file_exists(box, capsys):
    write_names(box, EXAMPLE)
    for token in ("0000:03:00.0", "2", S1):
        code, g, _ = grant(box, capsys, "--exact", token)
        assert code == 0
        run(["release", g["lease_id"]], capsys)


# ---- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize("text,token,kind_words", [
    (EXAMPLE, "lefft", "no board is named"),
    (EXAMPLE, "left.7", "outside board"),
    (EXAMPLE + '[[board]]\nname = "gone"\nserial = "NOT-HERE"\n', "gone", "not on this machine"),
    ('[[board]]\nname = "all"\nserial = "s"\n', "left", "reserved"),
    ('[[board]]\nname = "x"\nserial = "a"\n[[board]]\nname = "x"\nserial = "b"\n', "x", "already used"),
    ('[[board]]\nname = "x"\nserial = "a"\nbogus = 1\n', "x", "unknown key"),
    ('garbage\n', "left", "expected"),
])
def test_a_bad_name_or_file_is_refused_with_its_own_message_and_changes_nothing(
        box, capsys, text, token, kind_words):
    write_names(box, text)
    gk_for(box)                    # create the state dirs
    before = state_snapshot(box)
    code, out, err = run(["acquire", "--exact", token, "--who", "t"], capsys)
    assert code == names_exit() and kind_words in err
    assert state_snapshot(box) == before               # no lease, no ticket
    code, out, err = run(["run", "--exact", token, "--who", "t", "--", "/bin/true"], capsys)
    assert code == names_exit()
    assert state_snapshot(box) == before


def test_no_names_file_at_all_gives_its_own_message(box, capsys):
    code, out, err = run(["acquire", "--exact", "left", "--who", "t"], capsys)
    assert code == names_exit() and "there is no name" in err


def test_an_ambiguous_name_is_refused(box, monkeypatch, capsys):
    # "wing" is a name in the file and also the serial of a board here.
    chips = [dict(c) for c in SINGLE_CHIP_BOARDS]
    chips[1]["serial"] = "wing"
    use_tree(box, chips, monkeypatch, sub="sys3")
    write_names(box, '[[board]]\nname = "wing"\nserial = "AAA"\n')
    code, out, err = run(["acquire", "--exact", "wing", "--who", "t"], capsys)
    assert code == names_exit() and "both a name" in err


def test_a_bad_file_is_not_partly_applied_by_read_only_commands(box, capsys):
    write_names(box, EXAMPLE + 'bogus line\n')
    code, out, err = run(["topology", "--json"], capsys)
    data = json.loads(out)
    assert code == 0
    assert [b["name"] for b in data["boards"]] == [None, None]
    assert [b["serial"] for b in data["boards"]] == [S1, S2]   # serial order
    assert "names not applied" in err and data["names_error"]
    code, out, err = run(["status", "--json"], capsys)
    assert all(c["name"] is None for c in json.loads(out)["chips"])
    code, out, err = run(["names"], capsys)
    assert code == names_exit()


def test_a_bad_file_does_not_stop_an_acquire_that_uses_no_name(box, capsys):
    write_names(box, 'garbage\n')
    code, g, _ = grant(box, capsys, "--exact", "0000:03:00.0")
    assert code == 0


# ---- status, topology, names ---------------------------------------------------------


def test_topology_json_and_text_show_names_in_file_order(box, capsys):
    write_names(box, EXAMPLE)
    code, out, _ = run(["topology", "--json"], capsys)
    data = json.loads(out)
    assert [b["name"] for b in data["boards"]] == ["left", "right"]
    assert [b["serial"] for b in data["boards"]] == [S2, S1]
    assert [c["name"] for c in data["boards"][1]["chips"]] == ["right.0", "right.1"]
    assert {"serial", "card", "chips"} <= set(data["boards"][0])   # old keys stay
    code, out, _ = run(["topology"], capsys)
    assert out.index("board left") < out.index("board right")
    assert "right.1" in out


def test_status_orders_by_file_and_adds_name_keys(box, capsys):
    write_names(box, EXAMPLE)
    code, out, _ = run(["status", "--json"], capsys)
    chips = json.loads(out)["chips"]
    assert [c["name"] for c in chips] == ["left.0", "left.1", "right.0", "right.1"]
    assert chips[0]["board"] == S2 and chips[0]["board_name"] == "left"
    assert chips[0]["position"] == 0 and "dev_index" in chips[0] and "bdf" in chips[0]
    code, out, _ = run(["status"], capsys)
    assert "board left" in out and "left.0" in out


def test_status_orders_a_three_board_box_the_file_way(box, monkeypatch, capsys):
    use_tree(box, THREE, monkeypatch)
    # File order is A, C; device-index order would be C, B, A.
    write_names(box, '[[board]]\nname = "one"\nserial = "S-A"\n'
                     '[[board]]\nname = "two"\nserial = "S-C"\n')
    code, out, _ = run(["status", "--json"], capsys)
    assert [c["board"] for c in json.loads(out)["chips"]] == (
        ["S-A"] * 2 + ["S-C"] * 2 + ["S-B"] * 2)
    assert [c["name"] for c in json.loads(out)["chips"]] == (
        ["one.0", "one.1", "two.0", "two.1", None, None])


def test_status_without_a_names_file_keeps_its_old_order_and_adds_null_names(box, capsys):
    code, out, _ = run(["status", "--json"], capsys)
    chips = json.loads(out)["chips"]
    assert [c["dev_index"] for c in chips] == [0, 1, 2, 3]
    assert all(c["name"] is None and c["board_name"] is None for c in chips)


def test_names_lists_everything_and_flags_unnamed_and_missing(box, capsys):
    write_names(box, f'[[board]]\nname = "left"\nserial = "{S2}"\n'
                     '[[board]]\nname = "spare"\nserial = "NOT-HERE"\n')
    code, out, _ = run(["names", "--json"], capsys)
    data = json.loads(out)
    assert data["boards"][0]["name"] == "left" and data["boards"][1]["name"] is None
    assert data["missing"] == [{"name": "spare", "serial": "NOT-HERE"}]
    c = data["boards"][0]["chips"][0]
    assert {"name", "position", "dev_index", "bdf", "asic_id"} <= set(c)
    code, out, _ = run(["names"], capsys)
    assert "(no name)" in out and "gozer names set" in out and "missing: spare" in out


def test_names_works_with_no_file(box, capsys):
    code, out, _ = run(["names"], capsys)
    assert code == 0 and "does not exist" in out and out.count("(no name)") == 2


# ---- udev -------------------------------------------------------------------------------

UDEV_KEY = re.compile(r'^(SUBSYSTEM|ATTR\{tt_asic_id\}|SYMLINK)\s*(==|\+=)\s*"([^"\\]*)"$')


def check_udev_line(line):
    """Each comma-separated item is KEY OP "value", with the operators these
    keys take: SUBSYSTEM and ATTR match (==), SYMLINK appends (+=)."""
    items = [i.strip() for i in line.split(",")]
    assert len(items) == 3, line
    seen = []
    for item in items:
        m = UDEV_KEY.match(item)
        assert m, f"not udev rule grammar: {item!r}"
        key, op, value = m.groups()
        assert op == ("+=" if key == "SYMLINK" else "=="), item
        seen.append((key, value))
    assert [k for k, _ in seen] == ["SUBSYSTEM", "ATTR{tt_asic_id}", "SYMLINK"]
    assert seen[0][1] == "tenstorrent"
    assert re.fullmatch(r"tenstorrent/by-name/[a-z][a-z0-9-]*-[0-9]+", seen[2][1])


def rule_lines(text):
    return [l for l in text.splitlines() if l and not l.startswith("#")]


def test_udev_output_matches_the_fixture(box, capsys):
    write_names(box, EXAMPLE)
    code, out, _ = run(["names", "udev"], capsys)
    here = os.path.dirname(__file__)
    assert out == open(os.path.join(here, "data", "names_udev_quietbox.rules")).read()


def test_every_generated_udev_line_fits_the_grammar(box, capsys):
    write_names(box, EXAMPLE + '\n[[chip]]\nboard = "right"\nposition = 0\n'
                               'asic = "4444444444444444"\n')
    code, out, _ = run(["names", "udev"], capsys)
    lines = rule_lines(out)
    assert len(lines) == 4
    for line in lines:
        check_udev_line(line)
    assert any('"4444444444444444"' in l and l.endswith('right-0"') for l in lines)


def test_udev_grammar_check_rejects_a_bad_line():
    for bad in ['SUBSYSTEM=="tenstorrent", ATTR{tt_asic_id}="x", SYMLINK+="tenstorrent/by-name/a-0"',
                'SUBSYSTEM=="tenstorrent", ATTR{tt_asic_id}=="x", SYMLINK="tenstorrent/by-name/a-0"',
                'SUBSYSTEM=="tenstorrent", ATTR{tt_asic_id}=="x"',
                'SUBSYSTEM=="tenstorrent", ATTR{tt_asic_id}=="x", SYMLINK+="tenstorrent/by-name/A-0"']:
        with pytest.raises(AssertionError):
            check_udev_line(bad)


def test_udev_skips_missing_boards_and_chips_without_an_asic_id(box, monkeypatch, capsys):
    chips = [dict(c) for c in QUIETBOX]
    chips[0]["asic_id"] = ""
    use_tree(box, chips, monkeypatch)
    write_names(box, EXAMPLE + '[[board]]\nname = "spare"\nserial = "NOT-HERE"\n')
    code, out, _ = run(["names", "udev"], capsys)
    assert len(rule_lines(out)) == 3
    assert "spare: board not present" in out and "left-0: chip has no ASIC id" in out


def test_udev_prints_the_install_step_and_writes_nothing(box, capsys, tmp_path):
    write_names(box, EXAMPLE)
    files = lambda: sorted(p.name for p in tmp_path.iterdir() if p.name != "state")
    before = files()
    code, out, _ = run(["names", "udev"], capsys)
    assert "sudo tee /etc/udev/rules.d/" in out and "udevadm" in out
    assert files() == before            # no rules file was written anywhere here


def test_udev_refuses_a_bad_file(box, capsys):
    write_names(box, "garbage\n")
    assert run(["names", "udev"], capsys)[0] == names_exit()


# ---- set / unset ---------------------------------------------------------------------------


def test_set_adds_a_board_and_keeps_comments(box, capsys):
    write_names(box, f'# my boards\n[[board]]\nname = "left"  # first\nserial = "{S2}"\n')
    code, out, _ = run(["names", "set", "third", "0000:03:00.0"], capsys)
    assert code == 0
    text = (box / "names.toml").read_text()
    assert text.startswith("# my boards\n") and "# first" in text
    nf = names.parse(text)
    assert [(b.name, b.serial) for b in nf.boards] == [("left", S2), ("third", S1)]


def test_set_creates_the_file_and_its_directory(box, monkeypatch, capsys, tmp_path):
    target = tmp_path / "cfg" / "gozer" / "names.toml"
    monkeypatch.setenv("GOZER_NAMES", str(target))
    code, out, _ = run(["names", "set", "left", S2], capsys)
    assert code == 0 and names.load(str(target)).boards[0].serial == S2


def test_set_accepts_a_serial_pci_address_index_or_existing_name(box, capsys):
    for token in (S2, "0000:01:00.0", "0"):
        (box / "names.toml").unlink(missing_ok=True)
        assert run(["names", "set", "x", token], capsys)[0] == 0
        assert names.load(str(box / "names.toml")).boards[0].serial == S2


def test_set_on_an_existing_name_points_it_at_another_board(box, capsys):
    write_names(box, "# keep me\n" + EXAMPLE)
    code, _, err = run(["names", "set", "left", S1], capsys)
    # the board is already named "right": refused, with the way out
    assert code == names_exit() and "already named 'right'" in err
    assert run(["names", "unset", "right"], capsys)[0] == 0
    assert run(["names", "set", "left", S1], capsys)[0] == 0
    text = (box / "names.toml").read_text()
    assert "# keep me" in text
    assert [(b.name, b.serial) for b in names.parse(text).boards] == [("left", S1)]


def test_set_refuses_a_board_under_a_lease(box, capsys):
    write_names(box, EXAMPLE)
    assert grant(box, capsys, "--exact", "left")[0] == 0
    before = (box / "names.toml").read_text()
    code, _, err = run(["names", "set", "mine", "0000:01:00.0"], capsys)
    assert code == names_exit() and "under a lease" in err
    assert (box / "names.toml").read_text() == before
    # pointing an existing name away from a leased board is refused as well
    code, _, err = run(["names", "set", "left", S1], capsys)
    assert code == names_exit()


def test_set_refuses_unusable_names_and_boards_without_writing(box, capsys):
    for args, words in ((["all", S2], "reserved"), (["Bad", S2], "not a valid name"),
                        (["x", "nonesuch"], "not a board name"),
                        (["abcdef0123456789", S2], "reserved")):
        code, _, err = run(["names", "set", *args], capsys)
        assert code == names_exit() and words in err, (args, err)
    assert not (box / "names.toml").exists()


def test_set_refuses_a_name_equal_to_an_asic_id_on_the_machine(box, capsys):
    code, _, err = run(["names", "set", "1111111111111111", S2], capsys)
    assert code == names_exit()
    assert not (box / "names.toml").exists()


def test_write_text_refuses_a_link(tmp_path):
    real = tmp_path / "real.toml"
    real.write_text("x")
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    with pytest.raises(NamesError):
        names.write_text(str(link), "y")
    assert real.read_text() == "x" and link.is_symlink()


def test_set_refuses_a_name_equal_to_a_serial_on_the_machine(box, monkeypatch, capsys):
    chips = [dict(c) for c in SINGLE_CHIP_BOARDS]
    chips[1]["serial"] = "wing"
    use_tree(box, chips, monkeypatch)
    code, _, err = run(["names", "set", "wing", "AAA"], capsys)
    assert code == names_exit() and "could not be told from the name" in err
    assert not (box / "names.toml").exists()


def test_set_never_writes_through_a_link(box, capsys, tmp_path):
    real = tmp_path / "real.toml"
    real.write_text(EXAMPLE)
    (box / "names.toml").symlink_to(real)
    code, _, err = run(["names", "set", "x", S2], capsys)
    assert code == names_exit() and real.read_text() == EXAMPLE


def test_unset_removes_the_board_and_its_pins(box, capsys):
    write_names(box, "# top\n" + EXAMPLE + '\n[[chip]]\nboard = "left"\nposition = 0\n'
                                           'asic = "1111111111111111"\n')
    code, _, _ = run(["names", "unset", "left"], capsys)
    assert code == 0
    text = (box / "names.toml").read_text()
    nf = names.parse(text)
    assert [b.name for b in nf.boards] == ["right"] and nf.chips == []
    assert text.startswith("# top")


def test_unset_of_an_unknown_name_is_refused(box, capsys):
    write_names(box, EXAMPLE)
    code, _, err = run(["names", "unset", "nope"], capsys)
    assert code == names_exit() and "no board is named" in err


# ---- sysfs only -----------------------------------------------------------------------------


def test_nothing_here_opens_a_tenstorrent_device_node(box, monkeypatch, capsys):
    opened = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    def spy_os_open(path, *a, **k):
        opened.append(str(path))
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    write_names(box, EXAMPLE)
    for argv in (["names"], ["names", "udev"], ["topology"], ["status"],
                 ["names", "set", "z", "0000:03:00.0"],
                 ["acquire", "--exact", "left", "--who", "t", "--no-queue"]):
        main(argv)
    capsys.readouterr()
    assert not [p for p in opened if p.startswith("/dev/tenstorrent")]
