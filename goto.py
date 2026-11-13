#!/usr/bin/env python3
"""herdr-goto: Herdr's Go To tree across every host.

Reads this server's session and every enabled saved machine's session and
draws them as one tree (host > workspace > tab/pane). The search bar always
has focus: typing filters, keeping each match's host and workspace for
context; Ctrl-K/Ctrl-J and the arrows move. Enter focuses the row on its own
server; under Sheprd it also switches the window to that host.

Other hosts draw at once from the last snapshot seen (cache) and refresh in
place. They are read over a kept-open SSH connection (ControlPersist), so a
refresh costs one round trip instead of a new SSH handshake.

Herdr runs its keybindings before a popup sees the key, so Ctrl-K/Ctrl-J are
bound to this plugin's nav actions (nav.sh): while the picker is open they
signal it (SIGUSR1 up, SIGUSR2 down); otherwise they run vim-herdr-navigation.

Python 3.9+, standard library only (curses).

  goto.py            the picker (runs in the plugin's popup pane)
  goto.py --open     open the popup (what the plugin action runs)
  goto.py --dump     print the tree as text and exit (for checking)

Optional config.json in the plugin config dir (`herdr plugin config-dir herdr-goto`):
  self_label      name for this server's host       (default: "Local" on macOS, else hostname)
  labels          {"machine label": "shown name"}   (rename saved machines)
  order           ["shown name", ...]               (host order; others follow)
  sheprd_machine  saved machine label that runs Sheprd, when this host does not
  sheprd_bin      sheprd path on that machine       (default /opt/homebrew/bin/sheprd)
"""

import curses
import json
import locale
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata

PLUGIN_ID = "herdr-goto"
STATUS_FILTERS = ["all", "blocked", "working", "idle", "done"]
FILTER_NAMES = {"all": "All", "blocked": "Blocked", "working": "Working", "idle": "Idle", "done": "Done"}
GLYPH = {"working": "●", "blocked": "▲", "done": "✓", "idle": "○"}
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), PLUGIN_ID)
PIDFILE = os.path.join(CACHE, "picker.pid")
REMOTE_PATH = "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"


# ── herdr access ────────────────────────────────────────────────────────────

def herdr_bin():
    candidate = os.environ.get("HERDR_BIN_PATH")
    if candidate and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("herdr") or "herdr"


def run(cmd, timeout):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "failed").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "failed")
    return proc.stdout


def run_herdr(args, timeout=6.0):
    return run([herdr_bin()] + args, timeout)


def api_call(method, params):
    """One request over this server's socket (methods the CLI lacks, like pane.focus)."""
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect(path)
        sock.sendall((json.dumps({"id": "goto", "method": method, "params": params}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data or b"null")
    finally:
        sock.close()


# ── kept-open SSH to other hosts ────────────────────────────────────────────

def ssh_base(target):
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ControlPath=" + os.path.join(CACHE, "cm-%C"), target]


def ensure_master(target):
    """Start a background SSH master for target unless one is already up."""
    base = ssh_base(target)
    check = subprocess.run(base[:1] + ["-O", "check"] + base[1:], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    if check.returncode == 0:
        return
    os.makedirs(CACHE, exist_ok=True)
    subprocess.run(base[:1] + ["-o", "ControlMaster=yes", "-o", "ControlPersist=4h", "-N", "-f"] + base[1:],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def remote_sh(host, script, timeout=8.0, check_master=True):
    """Run a sh script on host's machine over the kept-open SSH connection. Without
    the master check (one more ssh process), a missing master just means a slower
    direct connection."""
    if check_master:
        ensure_master(host.target)
    inner = "PATH={}; {}".format(REMOTE_PATH, script)
    return run(ssh_base(host.target) + ["-o", "ControlMaster=no", "sh -c " + shlex.quote(inner)], timeout)


def remote_herdr(host, args, timeout=8.0, check_master=True):
    """herdr ARGS on host's machine: over the kept-open SSH connection, else `herdr --machine`."""
    if host.target:
        try:
            return remote_sh(host, "exec herdr " + " ".join(shlex.quote(a) for a in args), timeout, check_master)
        except Exception:
            pass
    return run_herdr(["--machine", host.machine] + args, timeout)


def herdr_on(host, args, timeout=6.0):
    return run_herdr(args, timeout) if host.is_self else remote_herdr(host, args, timeout)


def load_config():
    directory = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not directory:
        try:
            directory = run_herdr(["plugin", "config-dir", PLUGIN_ID], timeout=3).strip()
        except Exception:
            directory = ""
    path = os.path.join(directory, "config.json") if directory else ""
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ── hosts ───────────────────────────────────────────────────────────────────

class Host:
    def __init__(self, label, machine=None, machine_id=None, target=None):
        self.label = label          # shown name
        self.machine = machine      # saved machine label (None for this server)
        self.machine_id = machine_id
        self.target = target        # ssh target of the saved machine
        self.snap = None
        self.error = None
        self.stale = False          # snap came from the cache, a refresh is pending

    @property
    def is_self(self):
        return self.machine is None

    @property
    def cache_path(self):
        return os.path.join(CACHE, "snapshot-{}.json".format(self.machine))

    def load_cached(self):
        if self.is_self:
            return
        try:
            with open(self.cache_path) as f:
                self.snap = json.load(f)
            self.stale = True
        except Exception:
            pass

    def load(self):
        try:
            snap = json.loads(herdr_on(self, ["api", "snapshot"], timeout=8.0 if self.machine else 4.0))
            self.snap, self.error, self.stale = snap["result"]["snapshot"], None, False
            if not self.is_self:
                os.makedirs(CACHE, exist_ok=True)
                tmp = self.cache_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.snap, f)
                os.replace(tmp, self.cache_path)
        except Exception as exc:  # unreachable, auth, timeout
            self.error = str(exc) or exc.__class__.__name__


def discover_hosts(cfg):
    if "self_label" in cfg:
        self_label = cfg["self_label"]
    elif sys.platform == "darwin":
        self_label = "Local"
    else:
        self_label = socket.gethostname().split(".")[0]
    hosts = [Host(self_label)]
    try:
        machines = json.loads(run_herdr(["machine", "list", "--json"], timeout=3))
    except Exception:
        machines = []
    labels = cfg.get("labels", {})
    for m in machines:
        if m.get("enabled", True):
            hosts.append(Host(labels.get(m["label"], m["label"]), m["label"], m.get("id"), m.get("target")))
    order = cfg.get("order", [])
    rank = {name: i for i, name in enumerate(order)}
    hosts.sort(key=lambda h: (rank.get(h.label, len(order)), not h.is_self))
    return hosts


# ── tree ────────────────────────────────────────────────────────────────────

class Row:
    __slots__ = ("kind", "host", "depth", "branch", "text", "kind_col", "status", "current",
                 "ws_label", "hl", "score", "ws_id", "tab_id", "pane_id", "agent", "multi")

    def __init__(self, kind, host, text, **kw):
        self.kind = kind            # host | ws | leaf | msg
        self.host = host
        self.text = text
        self.depth = kw.get("depth", 0)
        self.branch = kw.get("branch", "")
        self.kind_col = kw.get("kind_col", "")
        self.status = kw.get("status", "")
        self.current = kw.get("current", False)
        self.ws_label = kw.get("ws_label", "")
        self.hl = frozenset()       # positions in text the query matched (highlighted)
        self.score = 0.0
        self.ws_id = kw.get("ws_id")
        self.tab_id = kw.get("tab_id")
        self.pane_id = kw.get("pane_id")
        self.agent = kw.get("agent")
        self.multi = kw.get("multi", False)


def pane_order(snap):
    """Pane ids per tab in reading order (top to bottom, left to right)."""
    order = {}
    for layout in snap.get("layouts", []):
        panes = sorted(layout.get("panes", []), key=lambda p: (p["rect"]["y"], p["rect"]["x"]))
        order[layout["tab_id"]] = [p["pane_id"] for p in panes]
    return order


def build_tree(hosts):
    """[(host_row, [(ws_row, [leaf_rows])], msg_row_or_None)]."""
    tree = []
    for host in hosts:
        hrow = Row("host", host, host.label)
        if host.snap is None:
            text = ("unreachable: " + host.error[:120]) if host.error else "loading…"
            tree.append((hrow, [], Row("msg", host, text, depth=1)))
            continue
        snap = host.snap
        focused_pane = snap.get("focused_pane_id") if host.is_self else None
        panes = {p["pane_id"]: p for p in snap.get("panes", [])}
        layout = pane_order(snap)
        tabs_by_ws = {}
        for tab in snap.get("tabs", []):
            tabs_by_ws.setdefault(tab["workspace_id"], []).append(tab)
        workspaces = []
        for ws in sorted(snap.get("workspaces", []), key=lambda w: w.get("number", 0)):
            wrow = Row("ws", host, ws["label"], depth=1, ws_id=ws["workspace_id"])
            leaves = []
            for tab in tabs_by_ws.get(ws["workspace_id"], []):
                ids = layout.get(tab["tab_id"]) or [p for p, v in panes.items() if v["tab_id"] == tab["tab_id"]]
                tab_panes = [panes[i] for i in ids if i in panes] or [None]
                multi = len(tab_panes) > 1
                for n, pane in enumerate(tab_panes, 1):
                    agent = pane.get("agent") if pane else None
                    status = (pane.get("agent_status") if agent else "shell") if pane else ""
                    title = ""
                    if pane:
                        title = pane.get("label") or pane.get("name") or (
                            pane.get("terminal_title_stripped") if agent else "terminal") or ""
                    text = tab["label"] + (" · {} · {}".format(title, n) if multi else "")
                    leaves.append(Row("leaf", host, text, depth=2, kind_col=agent or "terminal",
                                      status=status, current=bool(pane) and pane["pane_id"] == focused_pane,
                                      ws_label=ws["label"], ws_id=ws["workspace_id"], tab_id=tab["tab_id"],
                                      pane_id=pane["pane_id"] if pane else None, agent=agent, multi=multi))
            workspaces.append((wrow, leaves))
        tree.append((hrow, workspaces, None))
    return tree


def is_word_start(text, i):
    return i == 0 or not text[i - 1].isalnum() or (text[i - 1].islower() and text[i].isupper())


def fuzzy(term, text):
    """fzf-style match of term in text (both compared lowercased).

    Returns (score, positions) or None. A substring scores highest; otherwise the
    letters must appear in order, scored up for word starts and runs of adjacent
    letters and down for the gaps between them. Letters scattered far apart
    score below 7 per letter and do not count as a match.
    """
    low = text.lower()
    if len(low) != len(text):       # rare case-folding that changes length
        text = low
    i = low.find(term)
    if i >= 0:
        bonus = 15 if is_word_start(text, i) else 0
        return 100.0 + 10 * len(term) + bonus - min(i, 50) * 0.2, frozenset(range(i, i + len(term)))
    best = None
    starts = [k for k, ch in enumerate(low) if ch == term[0]][:24]
    for start in starts:
        positions, k = [start], start
        for ch in term[1:]:
            k = low.find(ch, k + 1)
            if k < 0:
                break
            positions.append(k)
        if len(positions) != len(term):
            continue
        score, prev = 0.0, None
        for p in positions:
            score += 5
            if is_word_start(text, p):
                score += 8
            if prev is not None:
                score += 6 if p == prev + 1 else -0.6 * (p - prev - 1)
            prev = p
        if best is None or score > best[0]:
            best = (score, frozenset(positions))
    if best is None or best[0] < 7 * len(term):
        return None
    return best


def match_leaf(leaf, terms):
    """Each term must match the row's own text (fuzzy), its agent, or its
    workspace or host name (substring). Returns (score, highlight) or None."""
    total, hl = 0.0, set()
    for term in terms:
        hit = fuzzy(term, leaf.text)
        if hit:
            total += hit[0]
            hl |= hit[1]
        elif term in leaf.kind_col.lower():
            total += 20
        elif term in leaf.ws_label.lower() or term in leaf.host.label.lower():
            total += 10
        else:
            return None
    return total, frozenset(hl)


def substring_hl(text, terms):
    low, hl = text.lower(), set()
    for term in terms:
        start = low.find(term)
        while term and start >= 0:
            hl |= set(range(start, start + len(term)))
            start = low.find(term, start + len(term))
    return frozenset(hl)


def visible_rows(tree, query, status_filter):
    """The rows to draw. With a query, only matching rows remain (with their host
    and workspace above them), best matches first within the tree."""
    terms = query.lower().split()
    hosts = []
    for hrow, workspaces, msg in tree:
        hrow.hl = substring_hl(hrow.text, terms)
        if msg is not None:
            if not terms and status_filter == "all":
                hosts.append((0.0, [hrow, msg]))
            continue
        groups = []
        for wrow, leaves in workspaces:
            wrow.hl = substring_hl(wrow.text, terms)
            keep = []
            for leaf in leaves:
                if status_filter != "all" and leaf.status != status_filter:
                    continue
                if terms:
                    hit = match_leaf(leaf, terms)
                    if hit is None:
                        continue
                    leaf.score, leaf.hl = hit
                else:
                    leaf.score, leaf.hl = 0.0, frozenset()
                keep.append(leaf)
            if not keep and (terms or status_filter != "all"):
                continue
            if terms:
                keep.sort(key=lambda l: -l.score)   # stable: ties keep tree order
            for i, leaf in enumerate(keep):
                leaf.branch = "└─" if i == len(keep) - 1 else "├─"
            groups.append((max([l.score for l in keep] or [0.0]), [wrow] + keep))
        if not groups and (terms or status_filter != "all"):
            continue
        if terms:
            groups.sort(key=lambda g: -g[0])
        rows = [hrow]
        for _, g in groups:
            rows += g
        hosts.append((max([g[0] for g in groups] or [0.0]), rows))
    if terms:
        hosts.sort(key=lambda h: -h[0])
    return [r for _, rows in hosts for r in rows]


# ── jumping ─────────────────────────────────────────────────────────────────

def sheprd_local():
    if sys.platform != "darwin":
        return None
    for path in (shutil.which("sheprd"), "/opt/homebrew/bin/sheprd", "/Applications/Sheprd.app/Contents/MacOS/sheprd"):
        if path and os.access(path, os.X_OK):
            return path
    return None


def focus_args(row):
    """The herdr command that focuses row on its own server."""
    if row.kind == "ws":
        return ["workspace", "focus", row.ws_id]
    if row.agent and row.pane_id:
        return ["agent", "focus", row.pane_id]
    return ["tab", "focus", row.tab_id]


def jump(row, cfg):
    """Focus row on its server and, for another host, show that host in Sheprd.
    Each SSH command into a machine pays for its login shell, so a jump makes at
    most one SSH call, and the local Sheprd switch runs alongside it."""
    host = row.host
    args = focus_args(row)
    if host.is_self:
        try:
            run_herdr(args)
            if row.kind == "leaf" and not row.agent and row.multi and row.pane_id:
                api_call("pane.focus", {"pane_id": row.pane_id})
        except Exception:
            pass
        return  # the window already shows the host this popup runs on
    local = sheprd_local()
    if local:
        # This machine runs Sheprd: switch its window while the remote focus travels.
        switch = threading.Thread(target=lambda: run([local, "host", "select", "ssh:" + host.machine_id], 6.0))
        switch.start()
        try:
            remote_herdr(host, args, check_master=False)
        except Exception:
            pass
        switch.join()
    elif cfg.get("sheprd_machine") == host.machine and host.target:
        # Sheprd runs on the target machine: focus there, then show it, in one SSH call.
        sheprd = cfg.get("sheprd_bin", "/opt/homebrew/bin/sheprd")
        script = "herdr {} >/dev/null 2>&1; exec {} host select local".format(
            " ".join(shlex.quote(a) for a in args), shlex.quote(sheprd))
        try:
            remote_sh(host, script, check_master=False)
        except Exception:
            remote_herdr(host, args)
    else:
        try:
            remote_herdr(host, args, check_master=False)
        except Exception:
            pass


def jump_detached(row, cfg):
    """Let the popup close at once (Herdr closes it when this process exits) and
    finish the jump in a child in its own session, safe from the pane's hangup."""
    try:
        pid = os.fork()
    except OSError:
        jump(row, cfg)
        return
    if pid:
        return
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        jump(row, cfg)
        trace("jumped")
    finally:
        os._exit(0)


# ── drawing ─────────────────────────────────────────────────────────────────

def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text):
    return sum(char_width(c) for c in text)


def fit(text, width):
    out, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > width:
            if out and width > 0:
                while out and used + 1 > width:
                    used -= char_width(out.pop())
                out.append("…")
                used += 1
            break
        out.append(ch)
        used += w
    return "".join(out), used


class UI:
    def __init__(self, scr, hosts, cfg):
        self.scr = scr
        self.hosts = hosts
        self.cfg = cfg
        self.query = ""
        self.filter = "all"
        self.sel = 0
        self.top = 0
        self.tree = []
        self.rows = []
        self.dirty = threading.Event()
        self.moves = []             # from SIGUSR1/SIGUSR2 (Ctrl-K/J through nav.sh)
        self.placed = False
        curses.curs_set(1)
        curses.nonl()               # Enter reads as CR, so Ctrl-J (LF) can mean down
        curses.use_default_colors()
        palette = [("accent", curses.COLOR_CYAN), ("working", curses.COLOR_YELLOW),
                   ("blocked", curses.COLOR_RED), ("done", curses.COLOR_GREEN),
                   ("match", curses.COLOR_BLUE), ("host", curses.COLOR_MAGENTA)]
        self.c = {}
        for i, (name, color) in enumerate(palette, 1):
            curses.init_pair(i, color, -1)
            self.c[name] = curses.color_pair(i)
        curses.init_pair(20, curses.COLOR_WHITE, curses.COLOR_BLUE)
        self.c["sel"] = curses.color_pair(20) | curses.A_BOLD
        # The frame: pure white (xterm 231) where 256 colors exist; the theme's
        # ANSI white (solarized base2) is beige, so it is only the fallback.
        white = 231 if curses.COLORS >= 256 else curses.COLOR_WHITE
        curses.init_pair(21, white, -1)
        self.c["frame"] = curses.color_pair(21)
        self.dim = curses.A_DIM

    # rows and selection

    def key_of(self, row):
        return (row.kind, row.host.label, row.ws_id, row.tab_id, row.pane_id)

    def first_leaf(self):
        for i, r in enumerate(self.rows):
            if r.kind == "leaf":
                return i
        return 0

    def rebuild(self):
        """New data arrived: redraw it and keep the selected row where it still exists."""
        keep = self.key_of(self.rows[self.sel]) if self.rows and self.placed else None
        self.tree = build_tree(self.hosts)
        self.rows = visible_rows(self.tree, self.query, self.filter)
        found = False
        if keep is not None:
            for i, r in enumerate(self.rows):
                if self.key_of(r) == keep:
                    self.sel, found = i, True
                    break
        if not self.placed:
            for i, r in enumerate(self.rows):
                if r.current:
                    self.sel, self.placed, found = i, True, True
        if not found and keep is not None and (self.query or self.filter != "all"):
            self.sel = self.first_leaf()
        self.clamp()

    def requery(self):
        self.rows = visible_rows(self.tree, self.query, self.filter)
        self.sel = self.first_leaf() if (self.query or self.filter != "all") else 0
        if not (self.query or self.filter != "all"):
            for i, r in enumerate(self.rows):
                if r.current:
                    self.sel = i
        self.placed = True
        self.clamp()

    def selectable(self, i):
        return 0 <= i < len(self.rows) and self.rows[i].kind != "msg"

    def clamp(self):
        if not self.rows:
            self.sel = 0
            return
        self.sel = max(0, min(self.sel, len(self.rows) - 1))
        if not self.selectable(self.sel):
            self.move(1) if self.sel < len(self.rows) - 1 else self.move(-1)

    def move(self, step):
        i = self.sel
        while True:
            i += step
            if i < 0 or i >= len(self.rows):
                return
            if self.selectable(i):
                self.sel = i
                self.placed = True
                return

    # drawing

    def inner(self):
        """Size of the area inside the frame; put() coordinates are relative to it."""
        h, w = self.scr.getmaxyx()
        return max(h - 2, 1), max(w - 2, 1)

    def draw_frame(self):
        """A thin white line around the whole popup (Sheprd draws none)."""
        h, w = self.scr.getmaxyx()
        if h < 2 or w < 2:
            return
        attr = self.c["frame"]
        lines = [(0, "╭" + "─" * (w - 2) + "╮")]
        lines += [(y, "│") for y in range(1, h - 1)]
        lines += [(h - 1, "╰" + "─" * (w - 2) + "╯")]
        for y, text in lines:
            for x in ((0, w - 1) if text == "│" else (0,)):
                try:
                    self.scr.addstr(y, x, text, attr)
                except curses.error:   # writing the bottom-right cell moves the cursor off screen
                    pass

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.inner()
        if y < 0 or y >= h or x >= w:
            return 0
        room = (w - x) if width is None else min(width, w - x)
        text, used = fit(text, max(room, 0))
        try:
            self.scr.addstr(y + 1, x + 1, text, attr)
        except curses.error:
            pass
        return used

    def put_hl(self, y, x, row, attr, width):
        """The row's text with the letters the query matched highlighted
        (underlined on the selected row, whose background is already colored)."""
        text, _ = fit(row.text, width)
        if not row.hl:
            return self.put(y, x, text, attr)
        hit_attr = (attr | curses.A_UNDERLINE) if attr == self.c["sel"] else (
            attr | self.c["match"] | curses.A_BOLD | curses.A_UNDERLINE)
        col = x
        for i, ch in enumerate(text):
            col += self.put(y, col, ch, hit_attr if i in row.hl else attr)
        return col - x

    def draw_search(self, w, terminals):
        """Rounded search box: title and count in the top border, the query inside."""
        border = self.c["accent"]
        inner = max(w - 4, 4)
        title, count = " Go To ", " {} terminals ".format(terminals)
        top = "╭─" + title + "─" * max(inner - len(title) - len(count) - 2, 1) + count + "─╮"
        self.put(0, 1, top, border)
        self.put(0, 3, title, border | curses.A_BOLD)
        self.put(1, 1, "│", border)
        self.put(1, w - 2, "│", border)
        self.put(1, 3, "⌕ ", border | curses.A_BOLD)
        if self.query:
            self.put(1, 5, self.query, curses.A_BOLD, inner - 4)
        else:
            self.put(1, 5, "Search hosts, workspaces, tabs, agents…", self.dim, inner - 4)
        self.put(2, 1, "╰" + "─" * (w - 4) + "╯", border)

    def draw(self):
        scr = self.scr
        scr.erase()
        self.draw_frame()
        h, w = self.inner()
        terminals = sum(len(l) for _, wss, _ in self.tree for _, l in wss)
        self.draw_search(w, terminals)
        x = 2
        for f in STATUS_FILTERS:
            name = FILTER_NAMES[f]
            attr = (self.c["accent"] | curses.A_BOLD | curses.A_UNDERLINE) if f == self.filter else self.dim
            x += self.put(3, x, name, attr) + 2
        body_top, body_h = 5, max(h - 7, 1)
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + body_h:
            self.top = self.sel - body_h + 1
        status_w, kind_w = 9, 11
        right_x = w - 1 - status_w - kind_w
        for idx in range(self.top, min(len(self.rows), self.top + body_h)):
            row = self.rows[idx]
            y = body_top + idx - self.top
            selected = idx == self.sel
            base = self.c["sel"] if selected else 0
            if selected:
                self.put(y, 0, " " * w, base)
            if row.kind == "host":
                self.put_hl(y, 1, row, base or (self.c["host"] | curses.A_BOLD), w - 24)
                host = row.host
                note = "" if host.is_self else (
                    "remote · offline, cached" if host.error and host.snap else
                    "remote · updating…" if host.stale else "remote")
                self.put(y, w - 1 - text_width(note), note, base or self.dim)
            elif row.kind == "msg":
                self.put(y, 4, row.text, self.c["blocked"] if row.text.startswith("unreachable") else self.dim)
            elif row.kind == "ws":
                self.put_hl(y, 3, row, base or curses.A_BOLD, w - 6)
            else:
                x = 3
                x += self.put(y, x, row.branch + " ", base or self.dim)
                x += self.put(y, x, "◆ " if row.current else "  ", base or self.c["accent"])
                glyph = GLYPH.get(row.status, "·")
                x += self.put(y, x, glyph + " ", base or self.c.get(row.status, self.dim))
                self.put_hl(y, x, row, base, max(right_x - x - 1, 4))
                self.put(y, right_x, row.kind_col, base or self.dim, kind_w)
                self.put(y, right_x + kind_w, row.status, base or self.c.get(row.status, self.dim), status_w)
        if not self.rows:
            self.put(body_top, 3, "no matches", self.dim)
        self.put(h - 1, 1, "ctrl-k/j ↑↓ move · enter jump · tab filter · ctrl-u clear · ctrl-r reload · esc close",
                 self.dim)
        try:
            scr.move(2, 1 + min(5 + text_width(self.query), w - 3))
        except curses.error:
            pass
        scr.refresh()

    # data

    def load_all(self, use_cache):
        """This server's session now (fast, local); the others from cache, then live."""
        for host in self.hosts:
            host.error = None
            if host.is_self:
                host.load()
            else:
                if use_cache and host.snap is None:
                    host.load_cached()
                else:
                    host.stale = host.snap is not None
                threading.Thread(target=self._load, args=(host,), daemon=True).start()

    def _load(self, host):
        host.load()
        self.dirty.set()

    # keys

    def escape_sequence(self):
        """Arrows and paging sent as ESC [ x / ESC O x, which curses misses when the
        pane is not in application cursor mode. A lone ESC stays ESC."""
        self.scr.timeout(30)
        seq = ""
        try:
            while len(seq) < 6:
                ch = self.scr.get_wch()
                if not isinstance(ch, str):
                    break
                seq += ch
                if len(seq) >= 2 and (ch.isalpha() or ch == "~"):
                    break
        except curses.error:
            pass
        finally:
            self.scr.timeout(50)
        table = {"[A": curses.KEY_UP, "OA": curses.KEY_UP, "[B": curses.KEY_DOWN, "OB": curses.KEY_DOWN,
                 "[5~": curses.KEY_PPAGE, "[6~": curses.KEY_NPAGE, "[Z": curses.KEY_BTAB}
        return table.get(seq, "\x1b" if not seq else None)

    def run(self):
        signal.signal(signal.SIGUSR1, lambda *_: self.moves.append(-1))
        signal.signal(signal.SIGUSR2, lambda *_: self.moves.append(1))
        self.load_all(use_cache=True)
        self.rebuild()
        self.scr.timeout(50)
        while True:
            if self.dirty.is_set():
                self.dirty.clear()
                self.rebuild()
            while self.moves:
                self.move(self.moves.pop(0))
            self.draw()
            try:
                key = self.scr.get_wch()
            except curses.error:
                continue
            if key == "\x1b":
                key = self.escape_sequence()
            if key in ("\x1b", "\x03", "\x07"):           # esc, ctrl-c, ctrl-g
                return None
            if key in ("\r", curses.KEY_ENTER):
                trace("enter")
                return self.rows[self.sel] if self.selectable(self.sel) else None
            if key in (curses.KEY_UP, "\x10", "\x0b"):    # up, ctrl-p, ctrl-k
                self.move(-1)
            elif key in (curses.KEY_DOWN, "\x0e", "\n"):  # down, ctrl-n, ctrl-j
                self.move(1)
            elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                for _ in range(max(self.inner()[0] - 8, 1)):
                    self.move(1 if key == curses.KEY_NPAGE else -1)
            elif key in ("\t", curses.KEY_BTAB):
                step = 1 if key == "\t" else -1
                self.filter = STATUS_FILTERS[(STATUS_FILTERS.index(self.filter) + step) % len(STATUS_FILTERS)]
                self.requery()
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                self.query = self.query[:-1]
                self.requery()
            elif key == "\x15":                           # ctrl-u
                self.query = ""
                self.requery()
            elif key == "\x17":                           # ctrl-w
                self.query = self.query.rstrip()
                self.query = self.query[: self.query.rfind(" ") + 1] if " " in self.query else ""
                self.requery()
            elif key == "\x12":                           # ctrl-r
                self.load_all(use_cache=False)
                self.rebuild()
            elif isinstance(key, str) and key.isprintable():
                self.query += key
                self.requery()


# ── entry points ────────────────────────────────────────────────────────────

def open_popup():
    os.execv(herdr_bin(), [herdr_bin(), "plugin", "pane", "open", "--plugin", PLUGIN_ID,
                           "--entrypoint", "goto", "--placement", "popup"])


def dump():
    cfg = load_config()
    hosts = discover_hosts(cfg)
    threads = [threading.Thread(target=h.load) for h in hosts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    query = " ".join(a for a in sys.argv[2:])

    def marked(row):  # matched letters in [brackets]
        return "".join("[{}]".format(c) if i in row.hl else c for i, c in enumerate(row.text))

    for row in visible_rows(build_tree(hosts), query, "all"):
        if row.kind == "leaf":
            mark = "◆ " if row.current else "  "
            line = "    {} {}{} {}".format(row.branch, mark, GLYPH.get(row.status, "·"), marked(row))
            print("{:<70} {:<10} {}".format(line, row.kind_col, row.status))
        elif row.kind == "ws":
            print("  " + marked(row))
        elif row.kind == "msg":
            print("    " + row.text)
        else:
            print(row.text + ("" if row.host.is_self else "  (remote: {})".format(row.host.machine)))


T0 = time.time()


def trace(*what):
    """Timing of the last jumps, for chasing lag: ~/.cache/herdr-goto/jump.log."""
    try:
        path = os.path.join(CACHE, "jump.log")
        if os.path.exists(path) and os.path.getsize(path) > 64 * 1024:
            os.replace(path, path + ".old")
        with open(path, "a") as f:
            f.write("{:.3f} +{:5.0f}ms {}\n".format(time.time(), (time.time() - T0) * 1000, " ".join(map(str, what))))
    except Exception:
        pass


def write_pidfile():
    os.makedirs(CACHE, exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pidfile():
    try:
        with open(PIDFILE) as f:
            if f.read().strip() == str(os.getpid()):
                os.remove(PIDFILE)
    except Exception:
        pass


def main():
    if "--open" in sys.argv:
        open_popup()
        return
    if "--dump" in sys.argv:
        dump()
        return
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    cfg = load_config()
    hosts = discover_hosts(cfg)
    trace("start")
    write_pidfile()
    try:
        choice = curses.wrapper(lambda scr: UI(scr, hosts, cfg).run())
    finally:
        remove_pidfile()
    if choice is not None:
        trace("picked", choice.host.label, " ".join(focus_args(choice)))
        jump_detached(choice, cfg)


if __name__ == "__main__":
    main()
