# Wi-Fi Lifecycle Reliability PRD

- Status: Approved for implementation
- Date: 2026-07-24
- Product: BART Platform Display
- Target: Raspberry Pi Zero W, Raspberry Pi OS 13 (trixie), 480x320 touchscreen
- Base branch at planning time: `feature/wifi-scanning-fix`
- Implementation branch: `fix/wifi-lifecycle-reliability`

## Summary

The on-device Wi-Fi settings must reliably discover nearby networks throughout
the application's lifetime, visibly and verifiably disconnect the active
network without forgetting it, and connect or re-authenticate to supported
networks after password entry.

The implementation must use NetworkManager as the source of truth, keep the
pygame render loop responsive, surface failures instead of navigating as though
they succeeded, and avoid exposing Wi-Fi passwords in logs or process arguments.

## Confirmed production evidence

Live diagnostics established the following:

1. The connected SSID and its NetworkManager profile name differ. The original
   code compared profile names with SSIDs, misclassified the active network as
   unsaved, and selected device-level disconnect.
2. NetworkManager rejected repeated device disconnect attempts with
   `not authorized`. The UI ignored the return value and immediately returned to
   the network list.
3. NetworkManager initially returned seven nearby SSIDs, then degraded to only
   the connected mesh SSID during the same runtime.
4. A simultaneous raw `iw` scan still found the original nearby SSIDs. The
   wireless hardware, regulatory domain, and driver remained functional.
5. NetworkManager permissions for Wi-Fi scanning, network control, and profile
   modification were `auth`, not `yes`. A headless systemd service cannot
   satisfy an interactive PolicyKit authentication prompt.
6. Power saving was enabled, but the successful raw scan means it is not proven
   to be the root cause. It must not be changed without failed soak-test
   evidence after the application and permission fixes.

Private SSIDs, BSSIDs, UUIDs, and the device address are intentionally excluded
from this document.

## Goals

- Fresh scans continue working after startup, idle periods, connection attempts,
  and disconnects.
- A transient or degraded scan does not abruptly erase recently valid results.
- The UI distinguishes fresh, partial/stale, empty, failed, unauthorized, and
  unavailable scan states.
- Disconnect releases the active connection while preserving its profile,
  password, and the enabled/scannable state of the Wi-Fi adapter.
- Disconnect success is verified and visibly acknowledged.
- Saved profiles are identified by actual wireless SSID and UUID, not profile
  display name.
- Saved credentials are tried first; authentication failure prompts for a
  replacement password.
- Open, WPA2 Personal, and WPA3 Personal networks can be connected.
- Operation status reflects connecting, authenticating, obtaining an address,
  connected, disconnecting, and terminal errors.
- Passwords are never logged and are not placed in command arguments where
  NetworkManager supports stdin or password-file transport.
- Only the minimum NetworkManager actions are authorized for the service user.

## Non-goals

- Enterprise/802.1X authentication
- Captive-portal login flows
- Hidden-network entry
- WEP or WPA1 support
- Forgetting/deleting saved networks
- A Wi-Fi radio on/off control
- Replacing NetworkManager with direct `iw`/`wpa_supplicant` management
- Firmware, kernel, regulatory-domain, or power-save changes without new
  hardware evidence

## User stories

### Scan

As a user, I can open Wi-Fi settings at any time and see nearby networks without
restarting the application.

As a user, if a scan temporarily fails, I see a useful error and recent results
instead of an unexplained empty list.

### Disconnect

As a user, I can disconnect the active network and see clear progress and
confirmation. The saved password remains available for a later reconnect, and
the adapter remains able to scan.

### Connect and authenticate

As a user, I can select an open or WPA2/WPA3 Personal network, enter its
password when required, and see meaningful connection stages.

As a user, when a saved password is no longer valid, I am prompted to replace it
and retry rather than receiving an unexplained failure or duplicate profile.

## Functional requirements

### Network identity

Each visible network must include:

- SSID
- Selected/active BSSID
- Wi-Fi interface
- Signal percentage
- Security description
- Saved state
- Active state
- Profile name, when saved
- Profile UUID, when saved
- Fresh/stale state and last-seen timestamp

Saved profiles must be discovered by querying their
`802-11-wireless.ssid`. All activation and deactivation commands must address
profiles by UUID.

### Operation coordination

- Only one scan, connect, or disconnect operation may be active at a time.
- Conflicting touchscreen controls must be disabled during an operation.
- Stale background work must not overwrite newer state.
- Every operation returns a structured result containing success, stable error
  code, user-facing message, and any relevant data.
- Unexpected exceptions must terminate the job with a visible failure instead
  of leaving the UI permanently busy.

### Fresh scanning

1. Confirm that NetworkManager reports the Wi-Fi radio enabled. Do not
   automatically toggle it.
2. Resolve the real Wi-Fi device, excluding Wi-Fi Direct pseudo-devices.
3. Read NetworkManager's `LastScan` value.
4. Request a targeted rescan for that interface.
5. Wait with a bounded timeout for `LastScan` to advance.
6. Read NetworkManager's cached AP list only after completion.
7. De-duplicate by SSID, selecting the active AP or strongest AP.
8. Preserve the connected SSID even if a degraded scan omits it.
9. Merge recently seen missing SSIDs for a short bounded TTL and mark the
   aggregate result partial.
10. Expire missing results after the TTL.

The application must not use `sudo iw scan` in production. Raw scanning remains
diagnostic evidence only.

### Disconnect

1. Resolve the active connection UUID from the Wi-Fi device.
2. Confirm that the selected SSID maps to that UUID.
3. Run `nmcli connection down uuid <uuid>`.
4. Poll until the UUID is no longer active.
5. Preserve the connection profile and password.
6. Keep the Wi-Fi radio enabled and scannable.
7. Show `Disconnecting...`, then a verified success message.
8. On failure, remain on the detail view and display the classified error.
9. If another saved network activates, reflect its actual state rather than
   claiming the device is offline.

Device-level disconnect is prohibited for this UI action because it changes the
adapter's automatic activation state and previously selected the wrong semantic.

### Connect and authenticate

- Saved network without a supplied password:
  activate the saved profile UUID.
- Saved network with a replacement password:
  provide the secret through a mode-0600 password file and activate the same
  UUID.
- New protected network:
  use NetworkManager's interactive/stdin credential flow so the password does
  not appear in argv.
- New open network:
  connect without a password.
- Use the selected interface. Do not persistently pin a newly created profile
  to one BSSID, because that would break roaming across mesh access points.
- Preserve password whitespace and supported symbols.
- Verify the active UUID/SSID, device state, and IPv4 assignment before
  reporting success.
- Refresh saved profile identity after NetworkManager creates a new profile.
- Authentication failure on a protected network must request password entry.
- Unsupported security must be identified before attempting a connection.

### UI requirements

- Rescan is disabled while any Wi-Fi operation is active.
- Network rows are not tappable during a conflicting operation.
- Back navigation is disabled during connect/disconnect.
- Status messages are visible, width-bounded, and color coded.
- Disconnect remains on the detail page until it succeeds.
- Successful connect/disconnect returns to the list and rescans.
- A successful post-action scan retains the action confirmation.
- A failed post-action scan takes precedence over the confirmation and explains
  that recent results are being shown.
- Saved authentication failure opens the password keyboard automatically.
- Enterprise, WEP, and WPA1 networks show `UNSUPPORTED`.

## Error categories

- `busy`
- `radio_disabled`
- `radio_unavailable`
- `scan_timeout`
- `scan_failed`
- `not_authorized`
- `authentication_required`
- `authentication_failed`
- `network_not_found`
- `identity_failed`
- `unsupported`
- `dhcp_failed`
- `connect_failed`
- `disconnect_failed`
- `disconnect_timeout`
- `unexpected`

Raw NetworkManager details may be written to the journal only when they contain
no secret. Passwords and password-file contents must never be logged.

## Minimal PolicyKit deployment

Install a rule scoped to the systemd service user and only these actions:

- `org.freedesktop.NetworkManager.wifi.scan`
- `org.freedesktop.NetworkManager.network-control`
- `org.freedesktop.NetworkManager.settings.modify.own`
- `org.freedesktop.NetworkManager.settings.modify.system`

Do not grant every `org.freedesktop.NetworkManager.*` action. Do not run the
application as root.

The repository must include the rule as a deployment asset plus installation,
verification, and rollback instructions.

## Implementation phases

1. Create the fix branch and preserve unrelated/untracked user files.
2. Introduce UUID-aware profiles, active-connection state, structured scan
   results, and one operation lock.
3. Implement explicit rescan plus `LastScan` completion and bounded recent
   result retention.
4. Implement UUID-based verified disconnect.
5. Implement saved/new connection and password-retry flows.
6. Update the settings UI to poll jobs and render visible terminal outcomes.
7. Add the minimal PolicyKit rule and replace broad permission guidance.
8. Expand unit tests.
9. Install and verify on the Pi with physical touchscreen recovery available.
10. Run the hardware soak and review sanitized journals.

## Test requirements

Automated tests must cover:

- Escaped SSIDs and BSSIDs
- Multiple BSSIDs per SSID
- Active AP preference
- Profile-name/SSID mismatch
- Connected network omitted from a scan
- Recent-result retention and expiry
- Disabled-radio reporting without toggling
- Operation contention
- `LastScan` advancement
- UUID-based disconnect
- Disconnect authorization failure
- Saved UUID activation
- Saved authentication failure and password retry
- Password absence from argv
- Mode-0600 temporary secret file and cleanup
- Unsupported security detection

Hardware validation must cover:

- Initial scan compared with a raw diagnostic scan
- Repeated rescans
- Rescan after 5, 15, and 30 minutes
- Verified disconnect with profile retained
- Scan immediately after disconnect
- Saved reconnect
- Wrong password followed by correct password
- New open network
- WPA2 Personal
- WPA3 Personal or WPA2/WPA3 transition mode
- Password containing spaces and symbols
- Application and NetworkManager journal review

## Acceptance criteria

- Nearby networks remain discoverable after the 30-minute soak without an app
  restart.
- A degraded scan shows recent results with an explicit partial/error status.
- Disconnect no longer invokes device-level disconnect.
- Disconnect does not remove or alter the saved profile.
- Disconnect failure is visible and does not navigate away.
- After PolicyKit installation, required permissions report `yes`.
- A saved network reconnects without password entry.
- A stale saved password triggers password entry and a successful retry updates
  the usable profile without duplicates.
- Successful connection is not reported until an IPv4 address exists.
- No password is present in process arguments, application logs, or
  NetworkManager logs generated by the application.
- Automated tests pass and the touchscreen scenarios pass on real hardware.

## Rollout and rollback

1. Run tests before deployment.
2. Copy the minimal PolicyKit rule and verify permissions.
3. Deploy the branch to the Pi.
4. Restart only the application service.
5. Run scan/connect/disconnect smoke tests with physical access available.
6. Run the soak test.

Rollback consists of restoring the prior application revision, removing the
project-specific PolicyKit rule, reloading PolicyKit, and restarting the
application. No saved Wi-Fi profiles should need restoration.

## Execution brief for a new Codex chat

Read this PRD completely, then inspect the current worktree and repository
instructions. Continue on `fix/wifi-lifecycle-reliability`; never work directly
on `main`. Preserve unrelated changes, especially the existing untracked
`.claude/` directory.

Implement every in-scope requirement, run the available test suite, review the
full diff, and report remaining hardware-only verification separately. Do not
install the PolicyKit rule or intentionally disrupt the Pi's Wi-Fi connection
without confirming the user still has physical recovery access.
