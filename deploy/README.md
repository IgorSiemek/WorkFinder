# Production security

The CX23 uses `/usr/local/share/workfinder/compose.yaml` and `Dockerfile`, owned
by root. The deployment SSH account can invoke one forced command with a
40-character commit SHA. The root wrapper accepts the current `origin/main`,
checks infrastructure against the installed files, creates a database backup,
builds the image and waits for health checks. Failed startups restart the previous
image. The deployment account has no Docker group membership or interactive SSH.

An administrator installs reviewed infrastructure updates using `sudo install`.
Install `Dockerfile`, `deploy/compose.production.yaml` and
`deploy/seccomp-chromium.json` under `/usr/local/share/workfinder`, with the
Compose file named `compose.yaml`. Install `deploy/scripts/*` under
`/usr/local/sbin`. Install `deploy/apparmor-workfinder` as
`/etc/apparmor.d/workfinder-browser` and load it with `sudo apparmor_parser -r`.
Infrastructure changes require updating these installed files before deployment.

Chromium runs with its sandbox enabled. The AppArmor policy is based on the
[Moby Docker profile](https://github.com/moby/profiles/blob/main/apparmor/template.go).
The seccomp policy comes from
[Playwright v1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json).
The added user-namespace permission and `SYS_CHROOT` capability bounding-set entry
allow Chromium's namespace sandbox to start on Ubuntu 24.04. The unprivileged
application process has zero effective capabilities. Its root filesystem is
read-only, with writable temporary directories and the existing data volume.

Production GitHub variables contain the deployment account and the verified SSH
host public key. The private deployment key stays in the `production` environment
secret. SSH rejects changes to the pinned host key.

Administrator access uses the owner's existing SSH key and the `wfadmin` account.
UFW allows inbound TCP 22, 80 and 443. Direct root SSH and password authentication
are disabled. Fail2ban protects SSH. The portfolio service runs as `portfolio`,
uses Node 24 LTS and binds to `127.0.0.1:3000` behind Nginx.
