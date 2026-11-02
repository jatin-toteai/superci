# Running the GCP launcher on a VM behind Caddy (path A)

The launcher runs on one small always-on VM (an e2-micro in `us-east1`). Caddy on the same VM terminates TLS for the
launcher's DNS name and proxies to the launcher on `127.0.0.1:8080`. The VM creates and deletes the job VMs through the
Compute Engine API, as the service account the VM runs as; it does not need to be near them.

| File | Installed as |
|---|---|
| `install.sh` | run once per install or update, as root |
| `superci-launcher.service` | `/etc/systemd/system/superci-launcher.service` |
| `Caddyfile` | `/etc/caddy/Caddyfile`, with the DNS name written in |
| `env.example` | `/etc/superci-launcher.env` (root-owned, mode 0600), only when that file does not exist |

The service runs `uvicorn app:api --host 127.0.0.1 --port 8080` from the virtual environment in
`/opt/superci-launcher/.venv`, as the unprivileged user `superci-launcher`, restarts when it exits, and starts after the
network is up. Systemd reads `/etc/superci-launcher.env` as root, so the service's user cannot read it.

## What must exist first

- A static external address in `us-east1`, and a DNS A record for the launcher's name pointing at it. The record has to be in
  place before Caddy starts, because Caddy asks for the certificate when it starts.
- The service account `superci-launcher@<project>.iam.gserviceaccount.com` with the role that lets it create and delete the
  job VMs, and the subnet `superci-launcher` in `us-east1`.
- A firewall rule that admits TCP 443 from the internet to the network tag `superci-launcher`. Caddy's documentation lists
  ports 80 and 443; the TLS-ALPN challenge it can use needs only 443. If no certificate is issued with 443 alone
  (`journalctl -u caddy` says why), admit 80 as well.
- A rule for `gcloud compute ssh` and `scp` to reach port 22: from your address, or from Google's IAP range
  `35.235.240.0/20` with `--tunnel-through-iap` added to the `scp` and `ssh` commands below.
- The runner image (`image/`), and the control plane's URL, its plane id, and its keys.

## 1. Create the VM

```
gcloud compute instances create superci-launcher \
  --project <project> --zone us-east1-b --machine-type e2-micro \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
  --subnet superci-launcher --address <static address> \
  --service-account superci-launcher@<project>.iam.gserviceaccount.com --scopes cloud-platform \
  --tags superci-launcher
```

Give it no `--labels`. The launcher deletes the VMs that carry this plane's `superci-plane` label, and the launcher's own VM
must never carry it. Unlike a job VM, it keeps a service account: that is how the launcher reaches the Compute Engine API.

## 2. Copy the launcher to the VM

```
gcloud compute scp --recurse launchers/gcp superci-launcher:~/ --zone us-east1-b
```

`scp --recurse` copies every file in the directory, so run it from a checkout that has no `.venv`: a local virtual
environment is some 200 MB of binaries for another platform. `install.sh` leaves `.venv`, `tests`, `__pycache__` and
`.pytest_cache` out of `/opt/superci-launcher` in any case. Add `--project <project>` to this command and the next unless
`gcloud config` already names the project.

## 3. Install

```
gcloud compute ssh superci-launcher --zone us-east1-b --command 'sudo bash ~/gcp/deploy/install.sh <name>'
```

`<name>` is the launcher's DNS name, for example `launcher.example.com`. Keep the quotes: without them the shell on your own
machine expands `~` to your home directory before `gcloud` runs. `<name>` can also be given as `LAUNCHER_NAME` in the
environment. Nothing remembers the name between runs, so give the same one each time.

The first run installs the packages and Caddy, copies the launcher, builds the virtual environment, installs the unit and
the Caddyfile, writes `/etc/superci-launcher.env` from `env.example`, and stops (exit status 1) with the list of values still to fill in.
The launcher is not started. Fill them in on the VM:

```
gcloud compute ssh superci-launcher --zone us-east1-b
sudoedit /etc/superci-launcher.env
```

`env.example` explains each variable. The control plane's keys go on one line, in single quotes:

```
curl -s <plane-url>/.well-known/jwks.json | jq -c .
```

and the sweep secret is made with `openssl rand -hex 24`. Then run the install command again. This run starts the launcher
and Caddy and ends by checking that `http://127.0.0.1:8080/health` answers `{"agent":"gcp"}`. If it does not, it prints
the service's log. A `KeyError` there names a variable missing from the env file.

## 4. Check it over TLS

From another machine:

```
curl -s https://<name>/health
curl -s -o /dev/null -w '%{http_code}' -X POST https://<name>/launch
```

The first answers `{"agent":"gcp"}`, the second `401`. Then register `https://<name>` as the launcher's URL with the control
plane; the same string is `PUBLIC_URL`.

## Updating

Copy the new `launchers/gcp` as in step 2 and run the install command with the same name. The run is safe to repeat: it
refreshes the files and the dependencies, leaves `/etc/superci-launcher.env` as it is, and restarts both services.
An existing Caddyfile is replaced, so a change to it belongs in `deploy/Caddyfile`.

## When something is wrong

```
systemctl status superci-launcher caddy
journalctl -u superci-launcher -n 100 --no-pager
journalctl -u caddy -n 100 --no-pager
```
