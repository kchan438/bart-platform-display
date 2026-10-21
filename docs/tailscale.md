# Tailscale touchscreen controls

Open **Settings → Tailscale** to view status, the Tailscale IPv4 address, and
the device name. Status refreshes every five seconds while this view is open.
Opening this page requires a separate 4-12 digit admin PIN, entered with the
masked numeric keypad. Leaving/closing the page or 60 seconds without
input locks it again. Five wrong attempts trigger a 30 second cooldown.
The rest of Settings remains accessible. This is a touchscreen access gate;
existing operating-system administrators retain their normal SSH/sudo access.
Connect reuses the existing login and preferences. Disconnect asks for
confirmation and retains the login; it does not stop local Wi-Fi or the BART app.
Tailscale SSH sessions will end when disconnected. Normal SSH on the local
network remains available, for example `ssh kevinchan@bart-platform-display.local`.

The application uses normal OpenSSH over Tailscale; RemoteViewer is not needed.
Only a running, online Tailscale client is labelled connected. Missing software,
an unavailable daemon, sign-in/approval requirements, and command failures are
shown in the panel. Sign-in and account changes remain an SSH/browser setup step.
The desktop preview cannot change Tailscale connections.

Install Tailscale using its official Linux installer and authenticate the Pi
before enabling these controls. Install the narrow passwordless command rule:

```sh
sudo visudo -cf deploy/sudoers/bart-tailscale
sudo install -o root -g root -m 0440 deploy/sudoers/bart-tailscale /etc/sudoers.d/bart-tailscale
sudo apt-get install python3-argon2  # Raspberry Pi venv uses system site packages
sudo python3 deploy/setup_admin_pin.py
```

The setup prompts privately twice. It saves only a random salt and a
Argon2id hash (19 MiB memory, 2 passes, 1 lane) in
`/etc/bart-platform-display/admin-pin.json`, owned by root and readable only
by root and the display user's group (mode 0640, directory 0750). No PIN is
placed in Git, app settings, command-line arguments, or logs. Run the setup
again over SSH to change a forgotten PIN. Missing or malformed PIN configuration
keeps the Tailscale page locked. Verification runs on a worker thread.
Legacy PBKDF2 records remain supported, but must be recreated with the setup
command to gain faster verification. Re-enter the PIN privately; its previous
hash cannot be converted without it. On the Pi Zero, measured Argon2id
verification was about 2.1 seconds versus 13.1 seconds with PBKDF2.

The rule grants user `kevinchan` only `/usr/bin/tailscale up` and
`/usr/bin/tailscale down` without additional arguments. The app runs these
through `sudo -n`, on a worker thread, with a bounded process timeout. No
logout, auth keys, operator role, route changes, or general root commands are
part of this feature. Any broader pre-existing sudo rights remain separate.

To remove the permission, delete `/etc/sudoers.d/bart-tailscale`.
To verify on hardware, keep local SSH open, disconnect using the panel,
confirm local SSH and the display stay running, reconnect, then check
`tailscale status` and SSH through the Tailscale name from a second device.
The selected connection state is retained by Tailscale across service restarts.
