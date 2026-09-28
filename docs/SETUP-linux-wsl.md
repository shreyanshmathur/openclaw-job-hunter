# Setup on Linux and Windows (WSL2)

Everything in the README works on Linux and on Windows 11 through WSL2, with these differences.

## Display: the browser lanes need one

The scout, applier, outreach and replies lanes drive a visible (headed) browser. On a desktop Linux session
this just works. On a server without a display, or in WSL2 without WSLg, the browser lanes stay off:
`preflight` refuses them and `./jobhunter status` says that only API discovery, evaluation, code route email
and the Sheet run on this host. That is still useful: jobs are found through public APIs, scored, and emails
are drafted and sent after your approval.

The installer creates the `jobhunter` browser profile (`openclaw browser create-profile --name jobhunter`) and
sets it to headed. If `./jobhunter doctor` reports the profile as headless, set
`browser.profiles.jobhunter.headless` to `false` in your OpenClaw config.

## Linux

```bash
sudo apt-get update && sudo apt-get install -y python3 git curl      # Debian and Ubuntu
python3 --version                                                     # 3.9 or newer
```

Keep the Gateway running after you log out:

```bash
sudo loginctl enable-linger "$(whoami)"
openclaw gateway status
```

Copying Chrome logins is a macOS feature. On Linux the consent step still asks per site (default No) and
records your answers; you then log in by hand inside the agent's window: `./jobhunter browser consent`, which
opens each allowed site, or later `./jobhunter browser login <site>` for a site you allowed.

## Windows 11 with WSL2

1. In PowerShell as administrator: `wsl --install -d Ubuntu-24.04`, then restart.
2. In Ubuntu, turn on systemd: add these two lines to `/etc/wsl.conf`, then run `wsl --shutdown` in
   PowerShell and open Ubuntu again.

```text
[boot]
systemd=true
```

3. Continue with the Linux steps above, inside Ubuntu. Clone the repo into your Linux home folder
   (`cd ~`), not under `/mnt/c`.
4. WSLg (Windows 11) provides a display for the browser. Check with `echo $DISPLAY $WAYLAND_DISPLAY`: one of
   them must be set.
5. To keep the Gateway running without an open terminal, enable lingering as above. To start WSL when Windows
   starts, create a scheduled task that runs `wsl.exe -d Ubuntu-24.04 --exec dbus-launch true` at log on.

## Claude on Linux

The Claude Code installer (`curl -fsSL https://claude.ai/install.sh | bash`) and `claude auth login` work the
same way; the login opens a browser on the Windows side under WSL2. The API key route
(`./install.sh --api-key`) avoids the browser login entirely.

## No stay-awake helper

The stay-awake LaunchAgent is macOS only. On Linux, use your desktop's power settings, or run the Gateway on a
machine that does not sleep.
