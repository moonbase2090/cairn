# Run a Cairn sync server

Use this guide to keep a Cairn vault on a Linux server and sync local vaults
over HTTPS. Cairn stores data on the server you choose. It does not host your
vault.

This setup uses one bearer token for trusted clients. Each client keeps its
`CAIRN_AGENT` identity in its sync pack. Use a separate server for
clients that should not share access.

## Install Cairn

Install Git, Python 3.11 or later, and `python3-venv` on the server.
Install Cairn from its repository:

~~~sh
sudo git clone https://github.com/moonbase2090/cairn.git /opt/cairn
sudo python3 -m venv /opt/cairn/.venv
sudo /opt/cairn/.venv/bin/pip install /opt/cairn
sudo ln -s /opt/cairn/.venv/bin/cairn /usr/local/bin/cairn
~~~

Install Cairn on each client with the same embedder as the server. The examples
below use `hash`.

## Initialize the server vault

Create the service account and its data directory:

~~~sh
sudo useradd --system --home-dir /var/lib/cairn --create-home \
  --shell /usr/sbin/nologin cairn
sudo chown -R cairn:cairn /var/lib/cairn
~~~

Initialize an empty vault as the service account:

~~~sh
sudo -u cairn env CAIRN_DIR=/var/lib/cairn CAIRN_AGENT=server-host \
  /usr/local/bin/cairn init --yes --embed-spec hash
~~~

Create a bearer token file that only root can read:

~~~sh
sudo install -d -m 0755 /etc/cairn
sudo sh -c 'umask 077
printf "CAIRN_TOKEN=%s\n" "$(openssl rand -hex 32)" > /etc/cairn/server.env'
~~~

The server reads `CAIRN_TOKEN` from this file. Cairn does not print a
configured token when it starts.

## Run Cairn with systemd

Install the service unit:

~~~sh
sudo install -m 0644 /opt/cairn/deploy/cairn.service \
  /etc/systemd/system/cairn.service
sudo systemctl daemon-reload
sudo systemctl enable --now cairn
sudo systemctl status cairn
~~~

The service listens on loopback port `8778`. Put a reverse proxy in
front of it to handle public HTTPS.

## Add HTTPS with Caddy

Point a DNS name such as `sync.example.com` at the server. Open ports 80
and 443. Install Caddy with its
[official installation instructions](https://caddyserver.com/docs/install).
Then add this site to its configuration:

~~~caddyfile
sync.example.com {
    reverse_proxy 127.0.0.1:8778
}
~~~

Caddy [provisions and renews public certificates](https://caddyserver.com/docs/automatic-https)
for the configured DNS name. Cairn accepts plain HTTP only on loopback.
The proxy terminates TLS before traffic reaches Cairn. Reload Caddy after you
save its configuration:

~~~sh
sudo systemctl reload caddy
~~~

If Cairn terminates TLS, edit the installed service unit and restart it:

~~~sh
sudo systemctl edit --full cairn
sudo systemctl daemon-reload
sudo systemctl restart cairn
~~~

In `ExecStart`, change `--host` to an address clients can reach and add
`--tls-cert` and `--tls-key` with the certificate paths. Allow only the
needed client addresses through the firewall. Make the key readable by the
`cairn` service account. Cairn requires TLS 1.2 or later and a bearer token
when it listens beyond loopback. For a private certificate authority, pass
its certificate to each client with `--tls-ca`.

## Back up the server vault

Backups run beside the sync server and use the same SQLite vault directory.
Follow [Back up a SQLite vault](BACKUPS.md) to configure a destination, then
install the included service:

~~~sh
sudo install -m 0644 /opt/cairn/deploy/cairn-backup.service \
  /etc/systemd/system/cairn-backup.service
sudo systemctl daemon-reload
sudo systemctl enable --now cairn-backup
sudo systemctl status cairn-backup
~~~

The service reads backup settings from `/etc/cairn/config.toml` and optional
credentials from `/etc/cairn/backup.env`. Keep the settings file outside
`/var/lib/cairn` so they remain available if the vault directory is removed.
If a local folder target is outside `/var/lib/cairn`, add that folder to
`ReadWritePaths` in the service unit.

## Sync a client vault

Initialize each client with its own `CAIRN_AGENT` and the same
embedder as the server:

~~~sh
export CAIRN_DIR="$HOME/.cairn"
export CAIRN_AGENT="client-a"
cairn init --yes --embed-spec hash
~~~

Copy the server token into the client's secret manager or environment. Then
set the server URL and token for sync commands. You can pass a URL to an
individual command to use a different server:

~~~sh
export CAIRN_URL="https://sync.example.com"
export CAIRN_TOKEN="the-server-token"
cairn push
cairn pull
~~~

The server imports each memory's original agent identity from its sync pack.
Set a different `CAIRN_AGENT` on each client so stored memories keep
the right source identity.
