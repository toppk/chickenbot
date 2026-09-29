# Deploying and running chickenbot

The services run a **built artifact**, not the checkout. Editing the working
tree cannot change a running bot; only `deploy.sh` can.

## One-time setup

```bash
git clone https://github.com/toppk/chickenbot ~/workspace/chickenbot
cd ~/workspace/chickenbot && uv sync

mkdir -p ~/.config/systemd/user
cp deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger $USER          # so it survives logout
```

## A new instance

An instance is a domain — hobby, work, personal — with its own everything.
Nothing is shared between them: not the database, not the socket, not the
owners, and ideally not the API key, since OpenRouter reports spend per key.

```bash
uv run chickenbot init ~/server/chickenbot/hobby
```

That writes:

```
~/server/chickenbot/hobby/
  conf/chickenbot.toml    host, nick, channels, rooms, owners, model
  conf/.env               secrets, mode 600
  data/chickenbot.db      soul, people, rooms, chat log, activity, moderation
  cache/                  an external tool's mirror; delete it freely
  run/                    the tool socket
```

Then, in order:

1. **Secrets** into `conf/.env` — `OPENROUTER_API_KEY`, `GITHUB_TOKEN`,
   `CHICKENBOT_SASL_PASSWORD`. No paths belong in there: the unit sets
   `CB_INSTANCE_DIR` and everything hangs off it.
2. **`conf/chickenbot.toml`** — at minimum `irc.host`, `irc.nick`,
   `irc.channels`, `irc.owners` (services accounts, not nicks), and `rooms`
   saying what each channel is for. A config with no owners will not load, on
   purpose.
3. **Check it**: `uv run chickenbot -c ~/server/chickenbot/hobby/conf/chickenbot.toml --check-config`
4. **Start it**: `systemctl --user enable --now chickenbot@hobby chickenbot-github@hobby`

The unit name and the directory name are the same thing (`%i`).

## Deploying a change

```bash
cd ~/workspace/chickenbot
uv run pytest tests -q && uv run ruff check .
./deploy/deploy.sh
systemctl --user restart chickenbot@eaccel chickenbot-github@eaccel
```

`deploy.sh` refuses a dirty tree — a wheel built from uncommitted changes
cannot be traced to anything. It builds both wheels, installs them into
`~/server/chickenbot/venv`, and writes `~/server/chickenbot/DEPLOYED`:

```
revision: 39af305
tag:      deploy/20260929-012447
built:    2026-09-29T00:55:40-04:00
from:     /home/toppk/workspace/chickenbot
versions: chickenbot 0.1.0, chickenbot-github-tool 0.1.0
```

**Every clean deploy is tagged and the tag is pushed**, so what ran on a given
evening can be checked out by name rather than reconstructed from a timestamp.
A dirty build is not tagged: there would be nothing for the tag to point at.
The revision is also written into the package as `_revision.txt`, which is how
the running bot can say which commit it is — it has no working tree to ask,
and the whole point is that it is not running the working tree.

It prints the restart commands rather than running them: building and
interrupting a running bot are two decisions. **Every instance shares the one
venv**, so a deploy stages the new code for all of them and each restarts when
you say so.

`ALLOW_DIRTY=1 ./deploy/deploy.sh` overrides the clean-tree check. The
`DEPLOYED` file then says `(dirty)` and the revision is a lie about what is
running, so use it for a two-minute experiment and deploy properly after.

## Checking it came up

```bash
systemctl --user status chickenbot@eaccel
journalctl --user -u chickenbot@eaccel --since '-2 min' -o cat
```

The first line of a healthy start names the build and the instance:

```
WARNING chickenbot starting as chickenbot[eaccel-main]: chickenbot 0.1.0, chickenbot-github-tool 0.1.0
```

`ps` shows one process per unit, named for its instance — no `uv` wrapper,
because the unit runs the venv directly:

```
chickenbot[eaccel-main]
chickenbot[eaccel-github]
```

Then confirm it is actually somewhere, not merely running:

```bash
chickenbot -c <config> room        # the rooms it should be in, and what for
chickenbot -c <config> log         # what it has heard
chickenbot -c <config> activity --since 1 --exclude mode,topic,roster
```

## Rolling back

There is no rollback command. Check out the revision you want and deploy it:

```bash
git tag -l 'deploy/*' | tail -5      # what was deployed, and when
git checkout deploy/20260929-012447
./deploy/deploy.sh
systemctl --user restart chickenbot@eaccel chickenbot-github@eaccel
git checkout master
```

The database is **not** rolled back with it, and migrations run forward on
start. A schema change is the one thing to think about before rolling back
past it; everything else is safe.

## Backups

`conf/` and `data/` are the whole of it. `cache/` refills itself and `run/`
dies with the process.

```bash
tar -czf ~/server/chickenbot/backups/eaccel-$(date +%Y%m%d-%H%M%S).tar.gz \
    -C ~/server/chickenbot --exclude=eaccel/cache --exclude=eaccel/run eaccel
```

Take one before anything destructive. sqlite is fine to copy while running for
a backup of this kind, but stopping the unit first is cleaner.

## Starting an instance over

```bash
systemctl --user stop chickenbot@eaccel chickenbot-github@eaccel
# back up first, as above
rm -f ~/server/chickenbot/eaccel/data/chickenbot.db*
rm -f ~/server/chickenbot/eaccel/cache/*.db*
systemctl --user start chickenbot@eaccel chickenbot-github@eaccel
```

Everything learned is gone: the soul reverts to the shipped template, people,
aliases, room notes, standing, chat log, activity and the moderation record
all start empty. `conf/` survives, so it reconnects and re-joins as configured
— and because a room's job is declared in the toml, it comes back as the same
kind of bot in each room rather than a stranger.

## What is deliberately not automatic

- **The restart.** `deploy.sh` never interrupts a running bot.
- **`uv sync` in the service.** The unit runs `~/server/chickenbot/venv`
  directly, so a dependency change needs a deploy, not a restart. A service
  that resolves its own dependencies on boot turns a failed resolve into a
  failed start.
- **Schema downgrades.** Migrations run forward only.
