# shortcode_locker

A tiny web UI for resolving 3-character codes into strings. Designed for a ZimaBoard running ZimaOS now, with a NixOS path later.

A user enters a code like `ABC`, `I2b`, or `Z9a`; the web UI returns one string:

- `https://...` or `http://...` → **Open URL** + **Copy**
- `file://...`, `smb://...`, `obsidian://...`, etc. → **Try opening URI** + **Copy**
- anything else → plain text/note + **Copy**

The app uses Python stdlib only and is packaged with [`uv`](https://docs.astral.sh/uv/).

## Fast deploy from GitHub on ZimaOS

If your ZimaBoard has Docker / Docker Compose:

```bash
git clone https://github.com/luketych/shortcode_locker.git
cd shortcode_locker
cp .env.example .env   # optional; edit it if you want a token
docker compose up -d --build
```

Then open:

```text
http://<zimaboard-ip>:8765
```

The page has both:

- **Look up a code** — enter a 3-character code and get its string back.
- **Add a new string** — paste a URL/URI/note and get a generated 3-character code back.

Stop/update later:

```bash
cd shortcode_locker
git pull
docker compose up -d --build
```

## Run directly with uv

Install uv if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Run from a clone:

```bash
git clone https://github.com/luketych/shortcode_locker.git
cd shortcode_locker
uv run shortcode-locker serve --host 0.0.0.0 --port 8765
```

## Manage entries

```bash
# Generate an unused random 3-character code
uv run shortcode-locker generate

# Add entries
uv run shortcode-locker add ABC 'https://example.com' --label 'Example website'
uv run shortcode-locker add Z9a 'file:///mnt/storage/manuals' --label 'Manuals folder' --kind uri
uv run shortcode-locker add I2b 'Spare batteries are in the top-left drawer.' --label 'Battery stash' --kind text

# Machine-readable list / lookup
uv run shortcode-locker list --json
uv run shortcode-locker lookup ABC --json

# Remove
uv run shortcode-locker remove ABC
```

You can also edit `data/codes.json` directly.

## Web/API usage

Web UI:

```text
http://<zimaboard-ip>:8765
```

The same page lets you look up existing codes and add new strings. When you add a string, `shortcode_locker` saves it and displays the generated 3-character code.

Lookup JSON endpoint:

```text
http://<zimaboard-ip>:8765/api/lookup?code=ABC
```

Add JSON endpoint:

```bash
curl -X POST 'http://<zimaboard-ip>:8765/api/add' \
  -H 'Content-Type: application/json' \
  -d '{"value":"https://example.com","label":"Example","kind":"auto"}'
```

Response:

```json
{
  "code": "Ab2",
  "label": "Example",
  "value": "https://example.com",
  "kind": "url",
  "scheme": "https",
  "openable": true
}
```

Health check:

```text
http://<zimaboard-ip>:8765/healthz
```

## Code alphabet

Codes are exactly 3 alphanumeric characters and are case-sensitive.

Default alphabet:

```text
23456789ABCDEFGHIJKLMNPQRSTUVWXYZabdefghjmnqrtuy
```

This default:

- avoids `0`, `1`, and uppercase `O`
- allows uppercase `I`
- avoids lowercase `l`, `i`, and `o`
- avoids lowercase `c`/`z`, so letters like `C` and `Z` are uppercase-only by default

Edit `data/config.json` or set `SHORTCODE_LOCKER_ALPHABET` to change this.

## Optional access token

A 3-character code is easy to brute-force. If this is reachable beyond your trusted LAN, set a token or put it behind a VPN / reverse proxy with auth.

For Docker Compose, create/edit `.env`:

```bash
SHORTCODE_LOCKER_TOKEN=change-this-token
```

When a token is set, the web UI shows a token field. API callers can use:

```bash
curl -H 'Authorization: Bearer change-this-token' \
  'http://<zimaboard-ip>:8765/api/lookup?code=ABC'
```

The old `CODEBOOK_*` env vars still work as compatibility fallbacks, but new deployments should use `SHORTCODE_LOCKER_*`.

## Linux systemd install with uv

On a Debian-like ZimaOS host:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/luketych/shortcode_locker.git
cd shortcode_locker
sudo env "PATH=$PATH" ./install-systemd.sh
```

The installer copies the project to `/opt/shortcode_locker`, runs `uv sync --frozen --no-dev`, and starts:

```text
shortcode-locker.service
```

Data lives here:

```text
/var/lib/shortcode_locker/codes.json
/var/lib/shortcode_locker/config.json
```

Optional token:

```bash
sudo sh -c 'printf "SHORTCODE_LOCKER_TOKEN=%s\n" "change-this-token" > /etc/shortcode_locker.env'
sudo systemctl restart shortcode-locker
```

## NixOS sketch with uv

If you move to NixOS, keep this repo in `/srv/shortcode_locker` and add something like:

```nix
{ pkgs, ... }:
{
  users.groups.shortcode-locker = {};
  users.users.shortcode-locker = {
    isSystemUser = true;
    group = "shortcode-locker";
  };

  systemd.services.shortcode-locker = {
    description = "shortcode_locker: tiny 3-character shortcut/string resolver";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      User = "shortcode-locker";
      Group = "shortcode-locker";
      StateDirectory = "shortcode_locker";
      WorkingDirectory = "/srv/shortcode_locker";
      Environment = [
        "SHORTCODE_LOCKER_DATA=/var/lib/shortcode_locker/codes.json"
        "UV_PROJECT_ENVIRONMENT=/var/lib/shortcode_locker/.venv"
        "UV_CACHE_DIR=/var/lib/shortcode_locker/.uv-cache"
        "PORT=8765"
      ];
      EnvironmentFile = "-/etc/shortcode_locker.env";
      ExecStartPre = "${pkgs.uv}/bin/uv sync --directory /srv/shortcode_locker --frozen --no-dev";
      ExecStart = "${pkgs.uv}/bin/uv run --directory /srv/shortcode_locker --frozen shortcode-locker serve --host 0.0.0.0";
      Restart = "on-failure";
      NoNewPrivileges = true;
    };
  };

  networking.firewall.allowedTCPPorts = [ 8765 ];
}
```

Initialize the data file once:

```bash
sudo mkdir -p /var/lib/shortcode_locker
sudo cp /srv/shortcode_locker/data/codes.json /var/lib/shortcode_locker/codes.json
sudo cp /srv/shortcode_locker/data/config.json /var/lib/shortcode_locker/config.json
```

## Development

```bash
uv sync
uv run python -m unittest discover -s tests
uv run shortcode-locker lookup ABC --json
```

## Browser limitation for URIs

Browsers may refuse to open some URI schemes, especially `file://`. The UI always includes a **Copy** button so you can paste the URI into Finder, a file manager, or another app.
