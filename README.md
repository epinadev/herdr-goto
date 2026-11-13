# herdr-goto

**Herdr's Go To tree, across every host.**

A [Herdr](https://herdr.dev) plugin that opens one fuzzy picker over the
workspaces, tabs, panes and agents of *all* your Herdr servers: this machine
and every saved machine (`herdr machine list`). Pick a row and you land
there, on whichever host it lives on. Under [Sheprd](https://github.com/sheprd-labs/sheprd),
the window switches to that host too.

```
╭──────────────────────────────────────────────────────────────────────────────────╮
│ ╭─ Go To ───────────────────────────────────────────────────────── 6 terminals ─╮ │
│ │ ⌕ api                                                                         │ │
│ ╰───────────────────────────────────────────────────────────────────────────────╯ │
│  All  Blocked  Working  Idle  Done                                                │
│                                                                                   │
│ Local                                                                             │
│   [1] backend                                                                     │
│   ├─ ◆ ● [1] API refactor                                   claude     working    │
│   └─   ▲ [2] api tests                                      codex      blocked    │
│ server                                                                     remote │
│   [2] infra                                                                       │
│   └─   ○ [1] deploy API                                     terminal   shell      │
│                                                                                   │
│ ctrl-k/j ↑↓ move · enter jump · tab filter · ctrl-u clear · ctrl-r reload · esc   │
╰──────────────────────────────────────────────────────────────────────────────────╯
```

Herdr's built-in Go To only sees the server it runs on. herdr-goto reads all
of them and draws them as one tree: **host › workspace › tab / pane**, with
each row's agent and status in columns.

## Features

- **Every host in one tree.** This server plus each enabled saved machine.
- **Instant open.** Other hosts draw immediately from the last snapshot seen,
  then refresh in place. They are read over a kept-open SSH connection
  (`ControlPersist`), so a refresh is one round trip, not a new handshake.
- **Fuzzy search, fzf-style.** The search bar has focus from the start. Letters
  must appear in order; word starts and adjacent letters score higher.
  Rows that don't match disappear, the best match comes first, and the matched
  letters are highlighted. Host and workspace names also work as filter words
  (`server deploy`).
- **Status filters.** Tab cycles All / Blocked / Working / Idle / Done.
- **Vim-style movement.** Ctrl-K / Ctrl-J move the selection, even though
  Herdr normally handles those keys before a popup sees them (see below).
- **Immediate jumps.** The popup closes the moment you press Enter. The jump
  finishes in the background, using a single SSH call for remote hosts.
- **Sheprd aware.** When Sheprd runs on this machine, or on a saved machine you
  name in the config, the Sheprd window switches to the picked host.
- **No dependencies.** Python 3.9+ standard library only (`curses`).

## Requirements

- Herdr **0.9** or newer, on macOS or Linux.
- Python **3.9+** on each host (macOS's `/usr/bin/python3` is fine).
- For remote hosts: saved Herdr machines (`herdr machine add …`) reachable over
  SSH **without a password prompt** (key or agent auth). herdr-goto runs SSH
  with `BatchMode=yes`. `herdr` must be on the remote `PATH`, or in one of
  `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` or
  `/home/linuxbrew/.linuxbrew/bin`.

## Install

On **every host** you run Herdr on (each server opens its own popup):

```sh
herdr plugin install epinadev/herdr-goto
```

Or from a clone:

```sh
git clone https://github.com/epinadev/herdr-goto
herdr plugin link ./herdr-goto
```

Then bind it in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = ["prefix+k", "prefix+w"]
type = "plugin_action"
command = "herdr-goto.open"
description = "go to (all hosts)"
```

If `prefix+w` is already Herdr's workspace picker, free it first:

```toml
[keys]
workspace_picker = ""
```

Reload with `herdr server reload-config`.

### Ctrl-K / Ctrl-J inside the picker

Herdr runs its keybindings *before* a popup sees a key. If Ctrl-K/J are bound
in Herdr (for example by
[vim-herdr-navigation](https://github.com/paulbkim-dev/vim-herdr-navigation)),
the picker never receives them. To fix that, bind them to herdr-goto's `nav`
actions:

```toml
[[keys.command]]
key = "ctrl+j"
type = "plugin_action"
command = "herdr-goto.nav-down"
description = "navigate down (go to picker / vim / herdr)"

[[keys.command]]
key = "ctrl+k"
type = "plugin_action"
command = "herdr-goto.nav-up"
description = "navigate up (go to picker / vim / herdr)"
```

While the picker is open, these move its selection. Otherwise they do what
the key did before: vim-herdr-navigation when it is installed, else Herdr's
own pane focus. If nothing in Herdr binds Ctrl-K/J, you don't need this; the
picker reads them directly.

## Keys

| Key | Action |
| --- | --- |
| type | filter (fuzzy) |
| `Ctrl-K` / `Ctrl-J`, `↑` / `↓`, `Ctrl-P` / `Ctrl-N` | move |
| `PgUp` / `PgDn` | move a page |
| `Enter` | jump to the row |
| `Tab` / `Shift-Tab` | cycle the status filter |
| `Backspace`, `Ctrl-W`, `Ctrl-U` | delete a letter, a word, everything |
| `Ctrl-R` | reload every host |
| `Esc`, `Ctrl-C`, `Ctrl-G` | close |

## Configuration

Optional. Put a `config.json` in the plugin's config directory
(`herdr plugin config-dir herdr-goto`):

```json
{
  "self_label": "laptop",
  "labels": { "my-server": "server" },
  "order": ["laptop", "server"],
  "sheprd_machine": "laptop",
  "sheprd_bin": "/opt/homebrew/bin/sheprd"
}
```

| Key | Meaning | Default |
| --- | --- | --- |
| `self_label` | name shown for this server | `Local` on macOS, else the short hostname |
| `labels` | rename saved machines: `{"machine label": "shown name"}` | machine labels |
| `order` | host order by shown name; others follow | this host first |
| `sheprd_machine` | saved machine that runs Sheprd, when this host doesn't | none |
| `sheprd_bin` | `sheprd` path on that machine | `/opt/homebrew/bin/sheprd` |

### Sheprd across machines

Say Sheprd runs on your laptop and you also use a server:

- **On the laptop** nothing is needed. herdr-goto finds `sheprd` locally and
  runs `sheprd host select` when you pick a server row.
- **On the server** set `sheprd_machine` to the saved machine that points back
  at the laptop. Optionally set `labels` and `order` so both hosts show the same
  names. Picking a laptop row then focuses it and switches the Sheprd window in
  one SSH call.

```json
{
  "self_label": "server",
  "labels": { "laptop": "Local" },
  "order": ["Local", "server"],
  "sheprd_machine": "laptop"
}
```

## How it works

- **Data.** `herdr api snapshot` on this server. For each saved machine, the
  same command over SSH, through a background `ControlMaster` that stays up
  for 4 hours. Snapshots are cached in `~/.cache/herdr-goto/` so the next
  open draws other hosts at once, marked *updating…* until fresh data arrives.
- **Jumping.** `herdr agent focus`, `tab focus` or `workspace focus` on the
  row's own server, plus `pane.focus` for a split tab on this host. Then
  `sheprd host select` if the host changed. This runs in a detached process so
  Herdr can close the popup immediately.
- **Ctrl-K/J.** The picker writes its pid to `~/.cache/herdr-goto/picker.pid`.
  `nav.sh` sends it `SIGUSR1` (up) or `SIGUSR2` (down) when it is running.

## Troubleshooting

- `./goto.py --dump [query]` prints the tree, matches in `[brackets]`, and exits.
- `~/.cache/herdr-goto/jump.log` timestamps each open, Enter and finished jump.
- A host showing *offline, cached* means SSH to it failed. Check that
  `ssh <target> herdr --version` works without a prompt.
- A picker left open keeps Ctrl-K/J. Press Esc.

## Limitations

- On remote hosts, a split tab's *terminal* panes only focus their tab. Agent
  panes focus exactly.
- Git branches are not shown.

## License

[MIT](LICENSE)
