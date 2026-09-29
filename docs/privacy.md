# Privacy controls

Code: `src/sombra/privacy/`. None of this is legal advice; review with counsel before using Sombra in a company setting (LGPD).

## Consent gate

Before capture starts, the orchestrator calls `require_consent(meeting_dir)`. It prints a PT-BR notice to paste into the call chat:

> Aviso: esta reunião está sendo gravada e transcrita pelo Sombra, um assistente que roda no meu computador. Trechos da conversa e da tela compartilhada podem ser enviados a um provedor de IA para gerar respostas. Se você não concorda, me avise agora e eu desligo.

Type `sim` to confirm participants were told. Sombra writes `consent.json` (OS user, UTC time, exact notice text) in the meeting folder. Any other answer aborts the start and records nothing. `require_consent(..., interactive=False)` never prompts: it only accepts a meeting that already has a `consent.json`.

## Pause

| How | What |
| --- | --- |
| Global shortcut (macOS) | default **⌃⌥⌘P**; configurable as `ctrl+alt+cmd+p` or `⌃⌥⌘P` |
| `sombra pause` / `sombra resume` | from any terminal, through the local control socket |

The shortcut works while another app (the call) is focused. For that, grant Sombra (or your terminal while developing) access under **System Settings → Privacy & Security → Accessibility**.

The control socket is `$XDG_RUNTIME_DIR/sombra/control.sock`, or `~/.sombra/run/control.sock` when that variable is unset. Its directory is `0700` and the socket is `0600`. The server also checks the peer's UID on every connection (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS), so only your user can pause or resume. Use `--socket PATH` to point at a different socket.

## Blocked apps

Sombra never keeps frames from these apps or windows. `BlockedApps.skip` is the frame pipeline's hook.

- Apps (name or bundle id, case- and accent-insensitive): password managers (1Password, Bitwarden, KeePassXC, LastPass, Dashlane, Keychain Access, Passwords, Enpass, Proton Pass), mail (Mail, Outlook, Spark, Thunderbird, Airmail, Mimestream), and Brazilian banks (Nubank, Itaú, Bradesco, Banco do Brasil, Santander, Caixa, Inter, C6 Bank, PicPay, Mercado Pago).
- Window titles containing: 1Password, Bitwarden, LastPass, Gmail, Outlook, Internet Banking, the bank names above, Password, Senha.

Your config adds to these lists. `extend_defaults = false` replaces them instead.

## Retention

| Content | Kept for |
| --- | --- |
| `frames/*.jpg` | 7 days |
| `transcript.md`, `summary.md`, `log.jsonl`, `frames/index.jsonl` | 30 days |

A file's age is its modification time, which is the time of its last append. When a sweep removes the last recorded content of a meeting, it also removes the rest of that meeting's folder (`meeting.toml`, `context/`, `consent.json`). A meeting that never recorded anything is left alone.

```sh
sombra retention run --dry-run                     # list what would go
sombra retention run                               # delete it
sombra retention run --frames-days 3 --text-days 14 --root ~/Sombra/meetings
sombra delete 2026-09-29_1430_daily-time-x         # asks you to type the folder name
sombra delete 2026-09-29_1430_daily-time-x --yes
```

Deletion stays inside the meetings root. Sombra refuses a root that is a symlink, `/` or your home directory. It never follows symlinks: a symlinked meeting, `frames/` folder or file is reported as skipped and left as is. `sombra delete` accepts only a real folder directly inside the root.

## API keys

```sh
sombra auth set anthropic      # hidden prompt; or: pass | sombra auth set anthropic --stdin
sombra auth status anthropic   # "stored" / "not set"; never prints the key
sombra auth clear anthropic
```

Keys are stored only in the OS keychain, through `keyring`: Keychain on macOS, Secret Service (GNOME Keyring or KWallet) on Linux. The service name is `sombra` and the account is the provider name. Keys never go in config files, env files or logs. Error messages name the provider, never the key.

## Disk encryption

At start, the orchestrator calls `check_disk_encryption()` and shows `.warning` when it is set. On macOS this checks `fdesetup status` (FileVault). On Linux it checks whether the filesystem holding your home sits on a LUKS/dm-crypt device (`findmnt` + `lsblk`). Turn on FileVault or LUKS: meeting folders hold other people's speech.
