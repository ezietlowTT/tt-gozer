# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Stable names for boards and chips.

`/dev/tenstorrent/N`, the `tt-smi` index and the ttnn device id are positions:
the driver hands them out in probe order, and they move when a chip fails to
enumerate or the driver reloads. What does not move is a chip's ASIC id, a
board's serial and a PCI slot. This module lets a person give each board a short
name once, in a file they own, and then say `left` or `right.1` anywhere gozer
takes a board or chip.

Reads sysfs-derived data only (the `Board`/`Chip` records from topology.py). It
never opens /dev/tenstorrent/*.

The names file is a small subset of TOML, read by the parser below rather than
by `tomllib`, for two reasons: `tomllib` arrives in Python 3.11 and gozer
supports 3.10, and the parser can refuse anything it does not understand with
the line number, which is the behaviour the file's rules ask for.

    [[board]]            # the order of these tables is the board order
    name = "left"
    serial = "<board serial>"

    [[chip]]             # optional: pin a chip to a position inside its board
    board = "left"
    position = 0
    asic = "<asic id>"
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field

from gozer.topology import EXPECTED_CHIPS_PER_BOARD, Board, Chip

# The file is a few lines per board. A cap keeps a wrong GOZER_NAMES (a log, a
# device node) from being read whole.
MAX_NAMES_FILE_BYTES = 64 * 1024

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# What a command-line token that is meant as a name looks like: a name, or a
# name and a chip position (`left.1`).
NAME_TOKEN_RE = re.compile(r"^([a-z][a-z0-9-]{0,31})(?:\.([0-9]+))?$")

_PCI_RE = re.compile(r"^(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
_INTEGER_RE = re.compile(r"^[0-9]+$")
# Serials and ASIC ids are runs of hex digits. A name made only of a-f that long
# could be taken for one, so it is not allowed. (A name has to start with a
# letter, so a digit-led run is already refused by NAME_RE.)
_HEX_RUN_RE = re.compile(r"^[0-9a-f]{8,}$")

BOARD_KEYS = ("name", "serial")
CHIP_KEYS = ("board", "position", "asic")


class NamesError(Exception):
    """A names-file or name-resolution problem.

    `kind` is a short slug so callers and tests can tell the cases apart without
    reading the wording. Each kind has its own message.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---- the file -------------------------------------------------------------


@dataclass
class BoardEntry:
    name: str
    serial: str
    line: int                 # line of the [[board]] header
    last_line: int            # line of the last key in the table


@dataclass
class ChipPin:
    board: str
    position: int
    asic: str
    line: int


@dataclass
class NamesFile:
    path: str | None = None
    boards: list[BoardEntry] = field(default_factory=list)
    chips: list[ChipPin] = field(default_factory=list)


def default_path() -> str:
    """$GOZER_NAMES, else $XDG_CONFIG_HOME/gozer/names.toml, else
    ~/.config/gozer/names.toml."""
    explicit = os.environ.get("GOZER_NAMES")
    if explicit:
        return explicit
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "gozer", "names.toml")


def _file_error(path: str | None, line: int, kind: str, message: str) -> NamesError:
    where = f"{path}:{line}" if path else f"line {line}"
    return NamesError(kind, f"names file {where}: {message}")


def read_text(path: str) -> str | None:
    """The file's text, or None if there is no such file.

    Bounded and no-follow: a symbolic link, anything that is not a regular file,
    and anything over MAX_NAMES_FILE_BYTES are refused. Only the last path
    component is checked for being a link, so a directory in the path may be one
    (and GOZER_NAMES can point at the real file behind a linked dotfile).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                     | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        # ELOOP is what O_NOFOLLOW reports for a link.
        if os.path.islink(path):
            raise NamesError(
                "unreadable-file",
                f"names file {path} is a symbolic link; gozer does not follow "
                "links here. Point GOZER_NAMES at the file the link leads to") from e
        raise NamesError("unreadable-file",
                         f"cannot read names file {path}: {e.strerror}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NamesError("unreadable-file",
                             f"names file {path} is not a regular file")
        data = os.read(fd, MAX_NAMES_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_NAMES_FILE_BYTES:
        raise NamesError(
            "unreadable-file",
            f"names file {path} is larger than {MAX_NAMES_FILE_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise NamesError("malformed",
                         f"names file {path} is not UTF-8 text: {e}") from e


_HEADER_RE = re.compile(r"^\[\[\s*([A-Za-z0-9_-]+)\s*\]\]\s*(?:#.*)?$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*=\s*(.*)$")


def _parse_value(raw: str, path, lineno) -> str | int:
    """A quoted string or a non-negative integer, then an optional comment."""
    raw = raw.strip()
    if raw.startswith('"'):
        out, i = [], 1
        while i < len(raw):
            ch = raw[i]
            if ch == "\\":
                if i + 1 >= len(raw) or raw[i + 1] not in '"\\':
                    raise _file_error(path, lineno, "malformed",
                                      'only \\" and \\\\ are understood in a string')
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                rest = raw[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    raise _file_error(path, lineno, "malformed",
                                      "unexpected text after the string")
                return "".join(out)
            out.append(ch)
            i += 1
        raise _file_error(path, lineno, "malformed", "string is not closed")
    if raw.startswith("'"):
        end = raw.find("'", 1)
        if end < 0:
            raise _file_error(path, lineno, "malformed", "string is not closed")
        rest = raw[end + 1:].strip()
        if rest and not rest.startswith("#"):
            raise _file_error(path, lineno, "malformed",
                              "unexpected text after the string")
        return raw[1:end]
    m = re.match(r"^([0-9]+)\s*(?:#.*)?$", raw)
    if m:
        return int(m.group(1))
    raise _file_error(path, lineno, "malformed",
                      "a value is a quoted string or a whole number")


def check_name(name: str, path=None, line=None) -> None:
    """Raise NamesError if `name` may not name a board.

    Reserved is checked first so that `all` or `0` get the reason they are
    refused for, not just "bad shape".
    """
    def fail(kind, msg):
        if line is not None:
            raise _file_error(path, line, kind, msg)
        raise NamesError(kind, msg)

    if (name == "all" or _INTEGER_RE.match(name) or _PCI_RE.match(name)
            or _HEX_RUN_RE.match(name)):
        fail("reserved-name",
             f"{name!r} is reserved: a name may not be `all`, a whole number, a "
             "PCI address, or look like a serial or ASIC id")
    if not NAME_RE.match(name):
        fail("invalid-name",
             f"{name!r} is not a valid name: use a lowercase letter, then up to "
             "31 lowercase letters, digits or hyphens (no dots: `name.N` is how "
             "a chip is addressed)")


def parse(text: str, path: str | None = None) -> NamesFile:
    """Parse and check the file. Raises NamesError with the line; never returns
    a partly read file."""
    nf = NamesFile(path=path)
    cur_kind: str | None = None
    cur: dict = {}
    cur_line = 0
    last_key_line = 0
    board_lines: list[tuple[BoardEntry, dict]] = []

    def close():
        nonlocal cur_kind, cur
        if cur_kind is None:
            return
        keys = BOARD_KEYS if cur_kind == "board" else CHIP_KEYS
        for k in keys:
            if k not in cur:
                raise _file_error(path, cur_line, "malformed",
                                  f"[[{cur_kind}]] is missing `{k}`")
        if cur_kind == "board":
            for k in ("name", "serial"):
                if not isinstance(cur[k], str) or not cur[k].strip():
                    raise _file_error(path, cur["_line"][k], "malformed",
                                      f"`{k}` must be a non-empty string")
            check_name(cur["name"], path, cur["_line"]["name"])
            nf.boards.append(BoardEntry(cur["name"], cur["serial"].strip(),
                                        cur_line, last_key_line))
            board_lines.append((nf.boards[-1], cur["_line"]))
        else:
            if not isinstance(cur["board"], str) or not cur["board"]:
                raise _file_error(path, cur["_line"]["board"], "malformed",
                                  "`board` must be a name")
            if not isinstance(cur["position"], int):
                raise _file_error(path, cur["_line"]["position"], "malformed",
                                  "`position` must be a whole number")
            if not isinstance(cur["asic"], str) or not cur["asic"].strip():
                raise _file_error(path, cur["_line"]["asic"], "malformed",
                                  "`asic` must be a non-empty string")
            nf.chips.append(ChipPin(cur["board"], cur["position"],
                                    cur["asic"].strip(), cur_line))
        cur_kind, cur = None, {}

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            m = _HEADER_RE.match(line)
            if not m or m.group(1) not in ("board", "chip"):
                raise _file_error(path, lineno, "unknown-key",
                                  f"unknown table {line.split('#')[0].strip()}: "
                                  "only [[board]] and [[chip]] are understood")
            close()
            cur_kind, cur, cur_line = m.group(1), {"_line": {}}, lineno
            continue
        m = _KEY_RE.match(line)
        if not m:
            raise _file_error(path, lineno, "malformed",
                              "expected `key = value` or a [[board]] / [[chip]] "
                              "table")
        if cur_kind is None:
            raise _file_error(path, lineno, "malformed",
                              "a key must come after a [[board]] or [[chip]] "
                              "header")
        key = m.group(1)
        allowed = BOARD_KEYS if cur_kind == "board" else CHIP_KEYS
        if key not in allowed:
            raise _file_error(path, lineno, "unknown-key",
                              f"unknown key `{key}` in [[{cur_kind}]] "
                              f"(known: {', '.join(allowed)})")
        if key in cur:
            raise _file_error(path, lineno, "malformed",
                              f"`{key}` appears twice in this table")
        cur[key] = _parse_value(m.group(2), path, lineno)
        cur["_line"][key] = lineno
        last_key_line = lineno
    close()

    seen_names: dict[str, int] = {}
    seen_serials: dict[str, int] = {}
    for entry, lines in board_lines:
        if entry.name in seen_names:
            raise _file_error(path, lines["name"], "duplicate-name",
                              f"the name {entry.name!r} is already used on "
                              f"line {seen_names[entry.name]}")
        seen_names[entry.name] = lines["name"]
        key = entry.serial.casefold()
        if key in seen_serials:
            raise _file_error(path, lines["serial"], "duplicate-serial",
                              "this serial is already listed on line "
                              f"{seen_serials[key]}")
        seen_serials[key] = lines["serial"]

    pinned_asics: dict[str, int] = {}
    pinned_slots: dict[tuple[str, int], int] = {}
    for pin in nf.chips:
        if pin.board not in seen_names:
            raise _file_error(path, pin.line, "unknown-board",
                              f"[[chip]] names board {pin.board!r}, which has "
                              "no [[board]] table")
        if pin.position < 0:
            raise _file_error(path, pin.line, "position-outside",
                              "position cannot be negative")
        if pin.asic.casefold() in pinned_asics:
            raise _file_error(path, pin.line, "duplicate-asic",
                              "this ASIC id is already pinned on line "
                              f"{pinned_asics[pin.asic.casefold()]}")
        pinned_asics[pin.asic.casefold()] = pin.line
        slot = (pin.board, pin.position)
        if slot in pinned_slots:
            raise _file_error(path, pin.line, "duplicate-position",
                              f"position {pin.position} of {pin.board!r} is "
                              f"already pinned on line {pinned_slots[slot]}")
        pinned_slots[slot] = pin.line
    return nf


def load(path: str | None = None) -> NamesFile:
    """Read and parse the names file; an absent file is an empty one."""
    path = path or default_path()
    text = read_text(path)
    if text is None:
        return NamesFile(path=path)
    return parse(text, path)


# ---- applying the file to a machine -----------------------------------------


def _bdf_key(bdf: str):
    """Sort key for PCI addresses: numeric, so the order does not depend on
    string width."""
    parts = re.split(r"[:.]", bdf)
    try:
        return (0, tuple(int(p, 16) for p in parts))
    except ValueError:
        return (1, bdf)


@dataclass
class NamedBoard:
    entry: BoardEntry
    board: Board
    slots: dict[int, Chip]    # position -> chip
    size: int                 # positions 0..size-1 exist on this board


def literal_tokens(boards: list[Board], unreadable: list[Chip]) -> set[str]:
    """Every identifier already in use on this machine: serials, BDFs, device
    indices and ASIC ids."""
    out = {t for b in boards for t in (
        b.serial, *(x for c in b.chips for x in (c.bdf, str(c.dev_index),
                                                 c.asic_id)))}
    out |= {t for c in unreadable for t in (c.bdf, str(c.dev_index))}
    out.discard("")
    return out


class NameMap:
    """The names file applied to the boards found in sysfs."""

    def __init__(self, nf: NamesFile | None = None,
                 boards: list[Board] | None = None):
        self.file = nf or NamesFile()
        self.by_name: dict[str, NamedBoard] = {}
        self.by_serial: dict[str, NamedBoard] = {}
        self.missing: list[BoardEntry] = []
        self.unmatched_pins: list[ChipPin] = []
        self._ordered: list[Board] = list(boards or [])
        if nf is not None and boards is not None:
            self._build(nf, boards)

    def _build(self, nf: NamesFile, boards: list[Board]) -> None:
        present = {b.serial.casefold(): b for b in boards}
        named_serials = set()
        for entry in nf.boards:
            b = present.get(entry.serial.casefold())
            if b is None:
                self.missing.append(entry)
                continue
            named_serials.add(b.serial)
            pins = [p for p in nf.chips if p.board == entry.name]
            size = max(len(b.chips), EXPECTED_CHIPS_PER_BOARD.get(b.card, 0))
            slots: dict[int, Chip] = {}
            by_asic = {c.asic_id.casefold(): c for c in b.chips if c.asic_id}
            for p in pins:
                chip = by_asic.get(p.asic.casefold())
                if chip is None:
                    self.unmatched_pins.append(p)
                    continue
                if p.position >= size:
                    raise _file_error(
                        nf.path, p.line, "position-outside",
                        f"position {p.position} is outside board "
                        f"{entry.name!r}, which has positions 0 to {size - 1}")
                slots[p.position] = chip
            pinned = {c.bdf for c in slots.values()}
            rest = sorted((c for c in b.chips if c.bdf not in pinned),
                          key=lambda c: _bdf_key(c.bdf))
            free = (i for i in range(size) if i not in slots)
            for chip in rest:
                slots[next(free)] = chip
            nb = NamedBoard(entry, b, slots, size)
            self.by_name[entry.name] = nb
            self.by_serial[b.serial] = nb
        # Board order: named boards in file order, then the rest by serial.
        ordered = []
        for entry in nf.boards:
            nb = self.by_name.get(entry.name)
            if nb is not None:
                ordered.append(Board(
                    serial=nb.board.serial, card=nb.board.card,
                    chips=[nb.slots[i] for i in sorted(nb.slots)]))
        ordered += [b for b in sorted(boards, key=lambda b: b.serial)
                    if b.serial not in named_serials]
        self._ordered = ordered

    # ---- queries ----

    def ordered(self) -> list[Board]:
        return list(self._ordered)

    def board_name(self, serial: str) -> str | None:
        nb = self.by_serial.get(serial)
        return nb.entry.name if nb else None

    def position_of(self, chip: Chip) -> int | None:
        nb = self.by_serial.get(chip.serial)
        if nb is None:
            return None
        return next((p for p, c in nb.slots.items() if c.bdf == chip.bdf), None)

    def chip_name(self, chip: Chip) -> str | None:
        pos = self.position_of(chip)
        if pos is None:
            return None
        return f"{self.by_serial[chip.serial].entry.name}.{pos}"

    def unnamed(self) -> list[Board]:
        return [b for b in self._ordered if b.serial not in self.by_serial]

    def resolve(self, token: str, boards: list[Board], unreadable: list[Chip],
                grain: str) -> str:
        """Turn a name token into the BDF or serial the rest of gozer uses.

        A token that is not shaped like a name is returned unchanged, so BDFs,
        device indices, serials and unit keys keep working as before.
        """
        literals = literal_tokens(boards, unreadable)
        m = NAME_TOKEN_RE.match(token)
        if not m:
            return token
        name, pos = m.group(1), m.group(2)
        entry = next((e for e in self.file.boards if e.name == name), None)
        if entry is None:
            if token in literals:
                return token
            raise unknown_name(token, self.file)
        if token in literals:
            raise NamesError(
                "ambiguous",
                f"{token!r} is both a name in {self.file.path} and an "
                "identifier of a chip or board on this machine; use the "
                "BDF or serial, or rename the board")
        nb = self.by_name.get(name)
        if nb is None:
            raise NamesError(
                "board-missing",
                f"board {name!r} is named in {self.file.path} but its serial "
                f"{entry.serial} is not on this machine")
        if pos is None:
            if grain == "board":
                return nb.board.serial
            if len(nb.board.chips) == 1:
                return nb.board.chips[0].bdf
            raise NamesError(
                "board-needs-chip",
                f"boards are leased one chip at a time on this machine; "
                f"name a chip of {name!r} ({name}.0 to {name}.{nb.size - 1})")
        p = int(pos)
        if p >= nb.size:
            raise NamesError(
                "position-outside",
                f"{token} is outside board {name!r}, which has positions 0 to "
                f"{nb.size - 1}")
        chip = nb.slots.get(p)
        if chip is None:
            raise NamesError(
                "chip-missing",
                f"{token} has no readable chip: board {name!r} has "
                f"{len(nb.board.chips)} of {nb.size} chips readable")
        return chip.bdf


def unknown_name(token: str, nf: NamesFile) -> NamesError:
    known = ", ".join(e.name for e in nf.boards)
    if not nf.boards:
        return NamesError(
            "unknown-name",
            f"no board is named {token!r}: there is no name in {nf.path} "
            "(see `gozer names`, and `gozer names set` to add one)")
    return NamesError(
        "unknown-name",
        f"no board is named {token!r} in {nf.path} (names: {known})")


def build(nf: NamesFile, boards: list[Board]) -> NameMap:
    return NameMap(nf, boards)


# ---- udev ---------------------------------------------------------------------

UDEV_RULES_PATH = "/etc/udev/rules.d/60-tenstorrent-by-name.rules"


def udev_rules(nm: NameMap) -> str:
    """udev rules that make /dev/tenstorrent/by-name/<board>-<position>.

    Matches the ASIC id, the way the existing by-id rule does, so the link
    follows the chip. Writes nothing; the install step is in the header.
    """
    lines = [
        "# /dev/tenstorrent/by-name links, made by `gozer names udev`.",
        "# Each link follows the chip with that ASIC id, whatever index it gets.",
        "#",
        "# Install (needs root; gozer does not do this):",
        f"#   gozer names udev | sudo tee {UDEV_RULES_PATH} >/dev/null",
        "#   sudo udevadm control --reload",
        "#   sudo udevadm trigger --subsystem-match=tenstorrent --action=change",
        "",
    ]
    wrote = 0
    for entry in nm.file.boards:
        nb = nm.by_name.get(entry.name)
        if nb is None:
            lines.append(f"# {entry.name}: board not present on this machine, "
                         "no rules written")
            continue
        for pos in sorted(nb.slots):
            chip = nb.slots[pos]
            if not chip.asic_id:
                lines.append(f"# {entry.name}-{pos}: chip has no ASIC id, "
                             "no rule written")
                continue
            lines.append(
                f'SUBSYSTEM=="tenstorrent", ATTR{{tt_asic_id}}=="{chip.asic_id}"'
                f', SYMLINK+="tenstorrent/by-name/{entry.name}-{pos}"')
            wrote += 1
    if not wrote:
        lines.append("# No named board is present: nothing to write. "
                     "See `gozer names`.")
    return "\n".join(lines) + "\n"


# ---- editing the file ----------------------------------------------------------


def set_name(path: str, name: str, serial: str) -> str:
    """The new text of the file with `name` pointing at `serial`.

    Appends a [[board]] table, or changes the serial of the one that exists, so
    comments and the rest of the file stay as they were. Raises NamesError for
    a name or serial that cannot be used.
    """
    check_name(name)
    text = read_text(path) or ""
    nf = parse(text, path) if text else NamesFile(path=path)
    existing = next((e for e in nf.boards if e.name == name), None)
    holder = next((e for e in nf.boards
                   if e.serial.casefold() == serial.casefold()), None)
    if holder is not None and holder is not existing:
        raise NamesError(
            "duplicate-serial",
            f"that board is already named {holder.name!r}; "
            f"`gozer names unset {holder.name}` first")
    lines = text.splitlines()
    if existing is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[[board]]", f'name = "{name}"', f'serial = "{serial}"']
    else:
        for i in range(existing.line, existing.last_line):
            if re.match(r"^\s*serial\s*=", lines[i]):
                lines[i] = f'serial = "{serial}"'
                break
    return "\n".join(lines) + "\n"


def unset_name(path: str, name: str) -> str:
    """The new text with `name`'s [[board]] table and its [[chip]] pins gone."""
    text = read_text(path)
    if text is None:
        raise NamesError("unknown-name",
                         f"no board is named {name!r}: {path} does not exist")
    nf = parse(text, path)
    entry = next((e for e in nf.boards if e.name == name), None)
    if entry is None:
        raise unknown_name(name, nf)
    lines = text.splitlines()
    drop: set[int] = set(range(entry.line - 1, entry.last_line))
    # Pins for this board go with it: left behind they would name a board that
    # no longer has a table, and the file would stop loading.
    headers = [i for i, ln in enumerate(lines)
               if _HEADER_RE.match(ln.strip())]
    for pin in nf.chips:
        if pin.board != name:
            continue
        start = pin.line - 1
        end = next((h for h in headers if h > start), len(lines))
        stop = start
        for j in range(start + 1, end):
            if _KEY_RE.match(lines[j].strip()):
                stop = j
        drop |= set(range(start, stop + 1))
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    while kept and not kept[-1].strip():
        kept.pop()
    return ("\n".join(kept) + "\n") if kept else ""


def write_text(path: str, text: str) -> None:
    """Write the names file atomically; refuse to write through a link."""
    if os.path.islink(path):
        raise NamesError(
            "unreadable-file",
            f"names file {path} is a symbolic link; gozer does not follow "
            "links here. Point GOZER_NAMES at the file the link leads to")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
