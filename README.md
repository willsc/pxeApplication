# pxe-app

`pxe-app` is a FastAPI control plane for unattended bare-metal desktop provisioning through dnsmasq, iPXE, and HTTP-served installer assets. It supports BIOS and UEFI x86_64 clients and renders per-host install configuration for RHEL-family, Ubuntu, Debian, and Windows 10/11 installs.

## What It Provides

- Unauthenticated PXE endpoints for iPXE scripts, installer config, registration, and installer callbacks.
- Authenticated operator UI and JSON API for images, profiles, hosts, reinstall requests, decommissioning, and audit events.
- Postgres by default via Docker Compose, with SQLite still available for local development overrides.
- Signed `httponly` session cookies, bcrypt password hashes, CSRF checks on state-changing admin routes, and per-host unguessable install tokens.
- Strict Jinja2 rendering for Kickstart, Ubuntu autoinstall, Debian preseed, and Windows Autounattend templates.
- Append-only boot audit events for unknown boots, script fetches, config fetches, registrations, and callbacks.

## Quick Start

1. Run the setup script. Use `--ui-port` to choose the operator UI and pxe-app port.

   ```bash
   sudo scripts/setup.sh --host-ip 192.168.10.5 --pxe-network 192.168.10.0 --ui-port 9000
   ```

   The default `--dhcp-mode auto` probes the network. If an existing DHCP server responds, dnsmasq is configured as proxyDHCP. If no DHCP server responds, dnsmasq is configured as the DHCP server for the PXE VLAN. To overwrite an existing `.env`, add `--force`. To build and start the containers immediately, add `--start`.

2. For complete first-run setup, including container startup, iPXE bootloader download, Ubuntu Desktop/Server ISO import, and Image row creation, use `--start --prepare-media`.

   ```bash
   sudo scripts/setup.sh \
     --host-ip 192.168.10.5 \
     --pxe-network 192.168.10.0 \
     --ui-port 9000 \
     --dhcp-mode auto \
     --start \
     --prepare-media
   ```

   This downloads `undionly.kpxe`, `ipxe.efi`, and `wimboot`; imports Ubuntu Desktop 22/24/26 and Ubuntu Server 22/24/26; writes boot assets under `tftproot/`; then starts dnsmasq. It downloads multiple large ISOs.

3. To include Windows 11 during complete setup, either pass a Microsoft temporary ISO URL or put the ISO under this repo, for example `data/isos/Win11.iso`.

   ```bash
   sudo scripts/setup.sh \
     --host-ip 192.168.10.5 \
     --pxe-network 192.168.10.0 \
     --ui-port 9000 \
     --start \
     --prepare-media \
     --windows-url 'https://software.download.prss.microsoft.com/...'
   ```

   Windows ISO URLs are intentionally not scraped. Generate the temporary URL from Microsoft's official Windows 11 ISO download page, then pass it to `--windows-url`. If no Windows ISO or URL is provided, setup still prepares iPXE and Ubuntu media and prints the Windows download instructions.

4. Alternatively, copy and edit the environment file manually.

   ```bash
   cp .env.example .env
   ```

5. If you do not use `--prepare-media`, put boot files under `tftproot/`. At minimum, provide `undionly.kpxe` and `ipxe.efi`. Put OS kernels, initrds, `wimboot`, BCD, `boot.sdi`, and `boot.wim` under the paths you will reference in Images.

6. If you skipped `scripts/setup.sh`, update `dnsmasq/dnsmasq.conf` and replace the example `192.0.2.0/24` network and `192.0.2.10` PXE host IP.

7. Start the stack if setup did not already do it.

   ```bash
   docker compose up --build
   ```

8. Open `http://<pxe-host-ip>:<ui-port>`, login, create or review Images, then enroll Hosts.
   A default Profile is created automatically for every Image.

## DHCP Modes

The setup script supports three DHCP modes:

- `auto`: probes the PXE VLAN for DHCP offers. Existing DHCP found means proxyDHCP; no offers means full DHCP server mode.
- `proxy`: use this when another DHCP server already leases addresses. dnsmasq only provides PXE boot metadata.
- `server`: use this on an isolated PXE VLAN with no DHCP service. dnsmasq leases addresses and provides PXE boot metadata.

Examples:

```bash
sudo scripts/setup.sh --host-ip 192.168.10.5 --pxe-network 192.168.10.0 --dhcp-mode auto
scripts/setup.sh --host-ip 192.168.10.5 --pxe-network 192.168.10.0 --dhcp-mode proxy
scripts/setup.sh --host-ip 192.168.10.5 --pxe-network 192.168.10.0 --dhcp-mode server --dhcp-range-start 192.168.10.100 --dhcp-range-end 192.168.10.200 --dhcp-router 192.168.10.1 --dhcp-dns 192.168.10.1
```

`auto` requires root or equivalent capability because DHCP probing binds UDP port 68. If probing cannot run, setup fails instead of silently enabling a DHCP server. You can inspect DHCP manually with:

```bash
sudo scripts/check_dhcp.sh --interface eth0 --timeout 5
```

## Importing OS Media

The importer downloads or reads an ISO, extracts the PXE boot files into `tftproot/`, and creates the matching Image row in the database. Local use requires `bsdtar` from `libarchive-tools`; the Docker image includes it.

All standard media in one command after the stack is running:

```bash
docker compose run --rm pxe-app scripts/prepare_media.sh --windows-url '<official-windows-11-url>'
```

iPXE bootloaders only:

```bash
scripts/import_iso.sh bootloaders
```

Ubuntu Desktop 22, 24, and 26 LTS in one command:

```bash
scripts/import_iso.sh ubuntu-desktops --replace
```

Individual Ubuntu Desktop imports:

```bash
scripts/import_iso.sh ubuntu --edition desktop --version 22 --replace
scripts/import_iso.sh ubuntu --edition desktop --version 24 --replace
scripts/import_iso.sh ubuntu --edition desktop --version 26 --replace
```

Ubuntu Server:

```bash
scripts/import_iso.sh ubuntu --edition server --version 26 --replace
```

The Ubuntu aliases currently resolve to `22.04.5`, `24.04.4`, and `26.04`.

Windows 11 helper:

```bash
scripts/import_iso.sh windows-download-help
```

Windows 11:

```bash
scripts/import_iso.sh windows --iso ~/Downloads/Win11.iso --wimboot /path/to/wimboot --name windows-11 --replace
```

You can also pass a Microsoft-provided temporary ISO URL:

```bash
scripts/import_iso.sh windows --url 'https://software.download.prss.microsoft.com/...' --wimboot /path/to/wimboot --sha256 <expected-hash>
```

With Docker, run the same importer inside the app container:

```bash
docker compose run --rm pxe-app pxe-import-media ubuntu-desktops --replace
docker compose run --rm pxe-app scripts/prepare_media.sh --windows-url '<official-windows-11-url>'
```

Windows ISO URLs are intentionally not scraped from Microsoft. Use the official Microsoft download page or Media Creation Tool, then provide the local ISO or official temporary URL to the importer.

## Asset Management

Assets are inventory records linked one-to-one with hosts. They track asset tag, serial number, owner, department, location, manufacturer/model, status, notes, metadata, and build history. Successful installer callbacks automatically create an asset for a host if one does not already exist.

Use the UI at `/assets`, or the API:

- `GET /api/assets`
- `POST /api/assets`
- `PATCH /api/assets/{id}`
- `GET /api/assets/{id}/builds`

Host build history is also available at `GET /api/hosts/{id}/builds`.

## Automatic Profiles

Every Image gets one managed default Profile automatically. This happens when an Image is created through the UI/API, when media is imported with `pxe-import-media`, and at application startup for existing Images. Generated Profiles include OS-specific installer defaults and a generated install password, so operators do not need to fill out a profile form before assigning Hosts.

Open a generated Profile only when you need overrides such as SSH keys, installer variables, template path, root/admin password, or Ansible post-install settings.

## Unattended PXE Enrollment

By default, `PXE_UNATTENDED_AUTO_ENROLL=true`. An unknown PXE client MAC is registered automatically, assigned the unattended default Profile, moved to `READY`, and immediately given the iPXE installer script. Pending registered hosts are promoted the same way on their next boot.

Use the Profiles page to choose the unattended default Profile. If none is selected yet, the app chooses the first available Profile and persists it. To pin it from configuration instead, set:

```bash
PXE_UNATTENDED_DEFAULT_PROFILE_NAME=ubuntu-24.04-desktop
```

Disable fully unattended unknown-host installs with:

```bash
PXE_UNATTENDED_AUTO_ENROLL=false
```

Keep this enabled only on an isolated provisioning network because clients matching the PXE boot path can be reimaged without operator confirmation.

## Ansible Post-Installation

Profiles can optionally run an Ansible playbook after a host reports `status=done`. Configure this in the UI at `Profiles -> configure` under the Ansible column.

Playbooks live under `ansible/playbooks/`. The stored playbook path must be relative to that directory, which prevents the service from executing arbitrary filesystem paths. A minimal example is included at `ansible/playbooks/postinstall-example.yml`.

Ansible target selection:

- The target address is `host.variables.ansible_host`, then `host.variables.ip_address`, then `host.hostname`.
- The generated inventory always includes the `pxe_app` group unless overridden.
- Additional inventory variables can be set in the profile post-install form, or per host under `variables.ansible`.
- For Windows hosts, set inventory variables such as `ansible_connection=winrm`, `ansible_winrm_transport=ntlm` or `basic`, `ansible_port=5986`, and the required credentials/secrets through your chosen secret handling process.

Manual run:

```bash
curl -X POST http://<pxe-host-ip>:<ui-port>/api/hosts/<host-id>/run-ansible \
  -H "X-CSRF-Token: <token>" \
  --cookie "pxe_session=<session>"
```

Useful endpoints:

- `GET /api/profiles/{id}/post-install`
- `PUT /api/profiles/{id}/post-install`
- `POST /api/hosts/{id}/run-ansible`
- `GET /api/ansible-runs`
- `POST /api/ansible-runs/{id}/retry`

Docker includes `ansible-core`, `pywinrm`, `openssh-client`, and `sshpass`. For production, prefer SSH keys mounted under `data/keys/` and reference them from the post-install config. Host key checking is disabled by default because newly imaged machines usually do not have stable SSH host keys yet; enable it per profile once your host key process is in place.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
PXE_LISTEN_PORT=9000 PXE_INITIAL_ADMIN_USERNAME=admin PXE_INITIAL_ADMIN_PASSWORD='replace-with-12-chars' pxe-app
pytest
```

For local development without Postgres, set:

```bash
export PXE_DATABASE_URL=sqlite:///./data/pxe-app-dev.db
```

If you did not seed an admin through environment variables:

```bash
pxe-admin create-admin admin
```

## Data Model

- `User`: operator accounts for the web UI and admin API.
- `Image`: installer asset locations, OS type, repo URL, kernel args, or Windows WIM paths.
- `Profile`: reusable variables, root/admin password, SSH keys, image link, and optional template path.
- `Host`: MAC, hostname, profile assignment, host variables, state, and install token.
- `BootEvent`: audit record for PXE requests and installer callbacks.

## Host Lifecycle

`PENDING` hosts are registered but not installable unless unattended auto-enrollment is enabled. In unattended mode, a pending host is assigned the default Profile and moved to `READY` automatically when it PXE boots. When the installer fetches config, the host becomes `INSTALLING`. A callback with `status=done` marks it `PROVISIONED`; `status=failed` marks it `FAILED`. Operators can request reinstall, which rotates the token and returns the host to `READY`, or manually set `DECOMMISSIONED`.

## Image Path Examples

RHEL-family:

```text
os_type: rhel
kernel_path: rocky/9/vmlinuz
initrd_path: rocky/9/initrd.img
repo_url: http://mirror.example/rocky/9/BaseOS/x86_64/os
```

Ubuntu:

```text
os_type: ubuntu
kernel_path: ubuntu/24.04/casper/vmlinuz
initrd_path: ubuntu/24.04/casper/initrd
```

Debian:

```text
os_type: debian
kernel_path: debian/12/linux
initrd_path: debian/12/initrd.gz
```

Windows:

```text
os_type: windows
bootloader_path: windows/wimboot
bcd_path: windows/boot/BCD
boot_sdi_path: windows/boot/boot.sdi
wim_path: windows/sources/boot.wim
```

## Template Variables

Every installer template receives:

- `image`: the selected Image row.
- `profile`: the selected Profile row.
- `host`: host metadata plus `config_url`, `callback_url`, and `install_token`.
- `vars`: merged `profile.variables` and `host.variables`, with host values winning.
- `authorized_keys`: list of SSH public keys.
- `root_password`: plaintext profile password when set.
- `root_password_hash`: SHA-512 crypt hash for Linux installers when `root_password` is set.
- `random_token`: fresh one-shot token available to templates.

Templates use Jinja2 `StrictUndefined`, so missing required variables fail clearly instead of generating a broken unattended install file.

## Security Notes

- Keep PXE endpoints on an isolated provisioning VLAN. PXE clients and installers fetch over HTTP unless you build and deploy an HTTPS-capable iPXE binary.
- Treat the database as secret. Profile root/admin passwords are stored in plaintext so templates can hash or inject them for OS-specific unattended installers.
- Rotate `PXE_SECRET_KEY` to invalidate all sessions.
- Use a reverse proxy with TLS for the operator UI if it is reachable beyond the provisioning host.
- The unknown-host policy is controlled by `PXE_UNKNOWN_HOST_POLICY`: `menu`, `register`, or `localboot`.

## API Notes

State-changing admin API calls require an authenticated session and a CSRF token in `X-CSRF-Token` unless submitted through the built-in UI forms. Fetch `GET /api/auth/me` after login to obtain a token for API clients. The boot API is deliberately unauthenticated and relies on network isolation plus install tokens.

Useful endpoints:

- `GET /healthz`
- `GET /boot.ipxe`
- `GET /api/boot/config/{token}`
- `POST /api/boot/callback/{token}`
- `GET /api/hosts`
- `POST /api/hosts/{id}/request-install`
- `GET /api/hosts/{id}/events`
