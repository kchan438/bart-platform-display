import unittest
from unittest import mock

from bartdisplay import wifi


def connected(uuid='uuid-home', reason='0 (No reason given)'):
    return wifi.ActiveConnection(
        'wlan0',
        state='100 (connected)',
        uuid=uuid,
        reason=reason,
    )


def disconnected(reason='53 (SSID not found)'):
    return wifi.ActiveConnection(
        'wlan0',
        state='30 (disconnected)',
        reason=reason,
    )


def connecting(uuid='uuid-home', reason='0 (No reason given)'):
    return wifi.ActiveConnection(
        'wlan0',
        state='50 (connecting)',
        uuid=uuid,
        reason=reason,
    )


def needing_auth(uuid='uuid-home', reason='0 (No reason given)'):
    return wifi.ActiveConnection(
        'wlan0',
        state='60 (need authentication)',
        uuid=uuid,
        reason=reason,
    )


class ConnectionSupervisorTests(unittest.TestCase):
    def setUp(self):
        wifi.stop_connection_supervisor()
        wifi._reauth_required.clear()
        wifi._recovery_latch = None
        self.supervisor = wifi.ConnectionSupervisor(
            poll_sec=1.0,
            grace_sec=10.0,
            retry_delay_sec=1.0,
            max_retries=2,
        )
        self.supervisor.watch_connected(
            'uuid-home',
            'Holmes Guest',
            'wlan0',
        )

    def tearDown(self):
        self.supervisor.stop()
        wifi.stop_connection_supervisor()
        wifi._reauth_required.clear()
        wifi._recovery_latch = None

    def test_transient_drop_waits_then_recovers_exact_uuid(self):
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(disconnected(), '', 'ok', ''),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                    return_value=(
                        'reconnected',
                        'reconnected',
                        'Wi-Fi reconnected',
                        'uuid-home',
                        0,
                    ),
                ) as recover:
            status = self.supervisor.step(now=100.0)
            self.assertEqual(status.phase, 'grace')
            recover.assert_not_called()

            status = self.supervisor.step(now=111.0)

        recover.assert_called_once_with(
            'uuid-home',
            'Holmes Guest',
            'wlan0',
            expected_generation=self.supervisor._generation,
        )
        self.assertEqual(status.phase, 'reconnected')
        self.assertEqual(status.active_uuid, 'uuid-home')

    def test_networkmanager_self_recovery_prevents_manual_retry(self):
        observations = [
            (disconnected(), '', 'ok', ''),
            (connected(), '192.168.1.10/24', 'ok', ''),
        ]
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    side_effect=observations,
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            self.supervisor.step(now=100.0)
            status = self.supervisor.step(now=105.0)

        recover.assert_not_called()
        self.assertEqual(status.phase, 'reconnected')
        self.assertIn('Holmes Guest', status.message)

    def test_active_link_without_ipv4_is_treated_as_dhcp_failure(self):
        no_address = (connected(), '', 'ok', '')
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=no_address,
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                    return_value=(
                        'reconnected',
                        'reconnected',
                        'Wi-Fi reconnected',
                        'uuid-home',
                        0,
                    ),
                ) as recover:
            first = self.supervisor.step(now=100.0)
            second = self.supervisor.step(now=111.0)

        self.assertEqual(first.phase, 'grace')
        self.assertEqual(first.reason_code, 5)
        recover.assert_called_once()
        self.assertEqual(second.phase, 'reconnected')

    def test_connecting_then_disconnected_keeps_recovery_deadline(self):
        observations = [
            (connecting(), '', 'ok', ''),
            (disconnected(), '', 'ok', ''),
        ]
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    side_effect=observations,
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                    return_value=(
                        'reconnected',
                        'reconnected',
                        'Wi-Fi reconnected',
                        'uuid-home',
                        0,
                    ),
                ) as recover:
            first = self.supervisor.step(now=100.0)
            second = self.supervisor.step(now=111.0)

        self.assertEqual(first.phase, 'grace')
        recover.assert_called_once()
        self.assertEqual(second.phase, 'reconnected')

    def test_other_profile_activation_is_never_fought(self):
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(connecting('uuid-fallback'), '', 'ok', ''),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            first = self.supervisor.step(now=100.0)
            second = self.supervisor.step(now=200.0)

        recover.assert_not_called()
        self.assertEqual(first.phase, 'grace')
        self.assertEqual(second.phase, 'grace')
        self.assertIn('another saved network', second.message)

    def test_authentication_reason_never_retries(self):
        auth_failure = disconnected(
            '9 (Supplicant configuration failed)'
        )
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(auth_failure, '', 'ok', ''),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            status = self.supervisor.step(now=100.0)
            self.supervisor.step(now=200.0)

        recover.assert_not_called()
        self.assertEqual(status.phase, 'attention')
        self.assertEqual(status.code, 'authentication_failed')
        self.assertIn('uuid-home', wifi._reauth_required)

    def test_need_auth_state_prompts_instead_of_waiting_forever(self):
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(needing_auth(), '', 'ok', ''),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            status = self.supervisor.step(now=100.0)
            later = self.supervisor.step(now=200.0)

        recover.assert_not_called()
        self.assertEqual(status.phase, 'attention')
        self.assertEqual(status.code, 'authentication_required')
        self.assertEqual(later.phase, 'attention')
        self.assertIn('uuid-home', wifi._reauth_required)

    def test_other_profile_need_auth_does_not_poison_watched_profile(self):
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(
                        needing_auth('uuid-fallback'),
                        '',
                        'ok',
                        '',
                    ),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            status = self.supervisor.step(now=100.0)

        recover.assert_not_called()
        self.assertEqual(status.phase, 'grace')
        self.assertNotIn('uuid-home', wifi._reauth_required)

    def test_intentional_disconnect_never_retries(self):
        self.supervisor.note_intentional_disconnect('uuid-home')
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(
                        disconnected('39 (User requested)'),
                        '',
                        'ok',
                        '',
                    ),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            status = self.supervisor.step(now=100.0)
            self.supervisor.step(now=200.0)

        recover.assert_not_called()
        self.assertEqual(status.phase, 'disconnected')
        self.assertEqual(status.code, 'user_disconnected')
        self.assertEqual(status.profile_uuid, '')

    def test_another_active_profile_is_adopted_without_recovery(self):
        other = connected('uuid-fallback')
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(other, '192.168.1.20/24', 'ok', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_profile_ssid',
                    return_value='ASUS_C',
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            status = self.supervisor.step(now=100.0)

        recover.assert_not_called()
        self.assertEqual(status.phase, 'connected')
        self.assertEqual(status.profile_uuid, 'uuid-fallback')
        self.assertIn('ASUS_C', status.message)

    def test_new_connect_hook_wins_race_during_active_adoption(self):
        def profile_ssid(_profile_uuid):
            self.supervisor.watch_connected(
                'uuid-current',
                'iPhone',
                'wlan0',
            )
            return 'ASUS_C'

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(
                        connected('uuid-observed'),
                        '192.168.1.20/24',
                        'ok',
                        '',
                    ),
                ), \
                mock.patch.object(
                    wifi,
                    '_profile_ssid',
                    side_effect=profile_ssid,
                ):
            status = self.supervisor.step(now=100.0)

        self.assertEqual(status.phase, 'connected')
        self.assertEqual(status.profile_uuid, 'uuid-current')
        self.assertEqual(status.active_uuid, 'uuid-current')
        self.assertIn('iPhone', status.message)

    def test_intentional_disconnect_hook_wins_race_before_adoption(self):
        original_adopt = self.supervisor._adopt_active

        def adopt_after_disconnect(*args, **kwargs):
            self.supervisor.note_intentional_disconnect('uuid-home')
            return original_adopt(*args, **kwargs)

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(
                        connected(),
                        '192.168.1.10/24',
                        'ok',
                        '',
                    ),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_adopt_active',
                    side_effect=adopt_after_disconnect,
                ):
            self.supervisor.step(now=100.0)

        self.assertEqual(self.supervisor._intentional_uuid, 'uuid-home')

        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(
                        disconnected('39 (User requested)'),
                        '',
                        'ok',
                        '',
                    ),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            status = self.supervisor.step(now=101.0)

        recover.assert_not_called()
        self.assertEqual(status.phase, 'disconnected')
        self.assertEqual(status.code, 'user_disconnected')

    def test_recovery_attempts_are_capped(self):
        failure = (
            'failed',
            'network_not_found',
            'Network is not currently detected',
            '',
            53,
        )
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(disconnected(), '', 'ok', ''),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                    return_value=failure,
                ) as recover:
            self.supervisor.step(now=100.0)
            first = self.supervisor.step(now=111.0)
            second = self.supervisor.step(now=113.0)
            final = self.supervisor.step(now=120.0)

        self.assertEqual(recover.call_count, 2)
        self.assertEqual(first.phase, 'grace')
        self.assertEqual(second.phase, 'failed')
        self.assertEqual(final.phase, 'failed')

    def test_final_recovery_error_is_not_replaced_on_next_poll(self):
        failure = (
            'failed',
            'not_authorized',
            'Not authorized',
            '',
            53,
        )
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(disconnected(), '', 'ok', ''),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                    return_value=failure,
                ):
            self.supervisor.step(now=100.0)
            self.supervisor.step(now=111.0)
            final = self.supervisor.step(now=113.0)
            later = self.supervisor.step(now=200.0)

        self.assertEqual(final.code, 'not_authorized')
        self.assertEqual(later.code, 'not_authorized')
        self.assertEqual(later.message, final.message)

    def test_operation_lock_contention_defers_recovery(self):
        wifi._operation_lock.acquire()
        try:
            result = self.supervisor._attempt_recovery(
                'uuid-home',
                'Holmes Guest',
                'wlan0',
            )
        finally:
            wifi._operation_lock.release()

        self.assertEqual(result[0], 'defer')
        self.assertEqual(result[1], 'busy')

    def test_missing_profile_is_not_recreated(self):
        with mock.patch.object(
                wifi,
                'active_connection',
                return_value=disconnected(),
        ), mock.patch.object(
                wifi,
                '_profile_exists',
                return_value=False,
        ), mock.patch.object(
                wifi,
                '_activate_profile',
        ) as activate:
            result = self.supervisor._attempt_recovery(
                'uuid-home',
                'Holmes Guest',
                'wlan0',
            )

        activate.assert_not_called()
        self.assertEqual(result[0], 'failed')
        self.assertEqual(result[1], 'profile_missing')

    def test_changed_profile_identity_is_not_activated(self):
        with mock.patch.object(
                wifi,
                'active_connection',
                return_value=disconnected(),
        ), mock.patch.object(
                wifi,
                '_profile_exists',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_profile_ssid_detailed',
                return_value=(True, 'Different network', ''),
        ), mock.patch.object(
                wifi,
                '_activate_profile',
        ) as activate:
            result = self.supervisor._attempt_recovery(
                'uuid-home',
                'Holmes Guest',
                'wlan0',
            )

        activate.assert_not_called()
        self.assertEqual(result[0], 'terminal')
        self.assertEqual(result[1], 'profile_identity_changed')

    def test_failed_activation_is_cancelled_before_lock_is_released(self):
        with mock.patch.object(
                wifi,
                'active_connection',
                side_effect=[disconnected(), disconnected()],
        ), mock.patch.object(
                wifi,
                '_profile_exists',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_profile_ssid_detailed',
                return_value=(True, 'Holmes Guest', ''),
        ), mock.patch.object(
                wifi,
                '_activate_profile',
                return_value=(124, '', 'timed out'),
        ), mock.patch.object(
                wifi,
                '_cancel_activation',
                return_value=True,
        ) as cancel, mock.patch.object(
                wifi,
                '_unblock_profile_autoconnect',
                return_value=(0, '', ''),
        ) as unblock:
            result = self.supervisor._attempt_recovery(
                'uuid-home',
                'Holmes Guest',
                'wlan0',
            )

        cancel.assert_called_once_with('uuid-home', 'wlan0')
        unblock.assert_called_once_with('uuid-home')
        self.assertEqual(result[0], 'failed')

    def test_authentication_failure_does_not_unblock_autoconnect(self):
        with mock.patch.object(
                wifi,
                'active_connection',
                side_effect=[disconnected(), disconnected()],
        ), mock.patch.object(
                wifi,
                '_profile_exists',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_profile_ssid_detailed',
                return_value=(True, 'Holmes Guest', ''),
        ), mock.patch.object(
                wifi,
                '_activate_profile',
                return_value=(1, '', 'Secrets were required'),
        ), mock.patch.object(
                wifi,
                '_cancel_activation',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_unblock_profile_autoconnect',
        ) as unblock:
            result = self.supervisor._attempt_recovery(
                'uuid-home',
                'Holmes Guest',
                'wlan0',
            )

        unblock.assert_not_called()
        self.assertEqual(result[0], 'attention')
        self.assertEqual(result[1], 'authentication_failed')

    def test_failed_autoconnect_unblock_stops_recovery_visibly(self):
        with mock.patch.object(
                wifi,
                'active_connection',
                side_effect=[disconnected(), disconnected()],
        ), mock.patch.object(
                wifi,
                '_profile_exists',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_profile_ssid_detailed',
                return_value=(True, 'Holmes Guest', ''),
        ), mock.patch.object(
                wifi,
                '_activate_profile',
                return_value=(124, '', 'timed out'),
        ), mock.patch.object(
                wifi,
                '_cancel_activation',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_unblock_profile_autoconnect',
                return_value=(1, '', 'readback failed'),
        ):
            result = self.supervisor._attempt_recovery(
                'uuid-home',
                'Holmes Guest',
                'wlan0',
            )

        self.assertEqual(result[0], 'terminal')
        self.assertEqual(result[1], 'autoconnect_failed')
        self.assertIn('automatic reconnect', result[2])

    def test_unverified_cleanup_latches_and_stops_retries(self):
        with mock.patch.object(
                wifi,
                'active_connection',
                side_effect=[disconnected(), disconnected()],
        ), mock.patch.object(
                wifi,
                '_profile_exists',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_profile_ssid_detailed',
                return_value=(True, 'Holmes Guest', ''),
        ), mock.patch.object(
                wifi,
                '_activate_profile',
                return_value=(124, '', 'timed out'),
        ), mock.patch.object(
                wifi,
                '_cancel_activation',
                return_value=False,
        ):
            result = self.supervisor._attempt_recovery(
                'uuid-home',
                'Holmes Guest',
                'wlan0',
            )

        self.assertEqual(result[0], 'terminal')
        self.assertEqual(result[1], 'cleanup_failed')
        self.assertIsNotNone(wifi._recovery_latch)
        self.assertEqual(wifi._recovery_latch.profile_uuid, 'uuid-home')

    def test_reconciled_saved_cleanup_restores_intended_autoconnect_policy(
            self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                network = wifi.Network(
                    'Holmes Guest',
                    saved=True,
                    profile_uuid='uuid-home',
                    autoconnect=enabled,
                )
                wifi._set_recovery_latch(
                    'wlan0',
                    'uuid-home',
                    network=network,
                    reason='test cleanup',
                )
                with mock.patch.object(
                        wifi,
                        '_cancel_activation',
                        return_value=True,
                ), mock.patch.object(
                        wifi,
                        '_unblock_profile_autoconnect',
                        return_value=(0, '', ''),
                ) as unblock:
                    self.assertTrue(wifi._reconcile_recovery())

                unblock.assert_called_once_with(
                    'uuid-home',
                    enabled=enabled,
                )
                self.assertIsNone(wifi._recovery_latch)

    def test_reconciled_saved_cleanup_stays_latched_when_unblock_fails(
            self):
        network = wifi.Network(
            'Holmes Guest',
            saved=True,
            profile_uuid='uuid-home',
            autoconnect=True,
        )
        wifi._set_recovery_latch(
            'wlan0',
            'uuid-home',
            network=network,
            reason='test cleanup',
        )
        latch = wifi._recovery_latch

        with mock.patch.object(
                wifi,
                '_cancel_activation',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_unblock_profile_autoconnect',
                return_value=(1, '', 'readback failed'),
        ):
            self.assertFalse(wifi._reconcile_recovery())

        self.assertIs(wifi._recovery_latch, latch)

    def test_reconciled_auth_cleanup_remains_blocked(self):
        network = wifi.Network(
            'Holmes Guest',
            saved=True,
            profile_uuid='uuid-home',
            autoconnect=True,
        )
        wifi._set_recovery_latch(
            'wlan0',
            'uuid-home',
            network=network,
            reason='authentication failed',
            reauth_required=True,
        )

        with mock.patch.object(
                wifi,
                '_cancel_activation',
                return_value=True,
        ), mock.patch.object(
                wifi,
                '_unblock_profile_autoconnect',
        ) as unblock:
            self.assertTrue(wifi._reconcile_recovery())

        unblock.assert_not_called()
        self.assertIn('uuid-home', wifi._reauth_required)
        self.assertIsNone(wifi._recovery_latch)

    def test_recovery_does_not_activate_after_reconciled_auth_latch(self):
        network = wifi.Network(
            'Holmes Guest',
            saved=True,
            profile_uuid='uuid-home',
            autoconnect=True,
        )
        wifi._set_recovery_latch(
            'wlan0',
            'uuid-home',
            network=network,
            reason='authentication failed',
            reauth_required=True,
        )

        with mock.patch.object(
                wifi,
                '_cancel_activation',
                return_value=True,
        ), mock.patch.object(
                wifi,
                'active_connection',
        ) as active, mock.patch.object(
                wifi,
                '_activate_profile',
        ) as activate:
            result = self.supervisor._attempt_recovery(
                'uuid-home',
                'Holmes Guest',
                'wlan0',
            )

        active.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(result[0], 'attention')
        self.assertEqual(result[1], 'authentication_required')
        self.assertEqual(result[4], 7)
        self.assertIn('uuid-home', wifi._reauth_required)

    def test_verified_adoption_clears_both_auth_blocks(self):
        self.supervisor._auth_blocked.add('uuid-home')
        wifi._reauth_required.add('uuid-home')

        adopted = self.supervisor._adopt_active(
            connected(),
            'wlan0',
            '192.168.1.10/24',
            expected_generation=self.supervisor._generation,
        )

        self.assertTrue(adopted)
        self.assertNotIn('uuid-home', self.supervisor._auth_blocked)
        self.assertNotIn('uuid-home', wifi._reauth_required)

    def test_state_query_failure_never_mutates_connection(self):
        failure = wifi.ActiveConnection(
            'wlan0',
            query_ok=False,
            error_code='state_query_failed',
            error_message='Could not read Wi-Fi state',
        )
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    self.supervisor,
                    '_observe',
                    return_value=(failure, '', 'ok', ''),
                ), \
                mock.patch.object(
                    self.supervisor,
                    '_attempt_recovery',
                ) as recover:
            status = self.supervisor.step(now=100.0)

        recover.assert_not_called()
        self.assertEqual(status.phase, 'failed')
        self.assertEqual(status.code, 'state_query_failed')
        self.assertEqual(status.profile_uuid, 'uuid-home')


class AutoconnectPolicyTests(unittest.TestCase):
    def test_already_enabled_profile_is_only_read_and_verified(self):
        with mock.patch.object(
                wifi,
                '_profile_autoconnect',
                side_effect=[
                    (0, 'yes\n', '', True),
                    (0, 'yes\n', '', True),
                ],
        ) as read, mock.patch.object(
                wifi,
                '_enable_profile_autoconnect',
        ) as enable:
            result = wifi._ensure_profile_autoconnect('uuid-home')

        self.assertEqual(result[0], 0)
        self.assertEqual(read.call_count, 2)
        enable.assert_not_called()

    def test_disabled_profile_is_enabled_and_read_back(self):
        with mock.patch.object(
                wifi,
                '_profile_autoconnect',
                side_effect=[
                    (0, 'no\n', '', False),
                    (0, 'yes\n', '', True),
                ],
        ), mock.patch.object(
                wifi,
                '_enable_profile_autoconnect',
                return_value=(0, '', ''),
        ) as enable:
            result = wifi._ensure_profile_autoconnect('uuid-home')

        self.assertEqual(result[0], 0)
        enable.assert_called_once_with('uuid-home')

    def test_false_readback_is_a_visible_policy_failure(self):
        with mock.patch.object(
                wifi,
                '_profile_autoconnect',
                side_effect=[
                    (0, 'no\n', '', False),
                    (0, 'no\n', '', False),
                ],
        ), mock.patch.object(
                wifi,
                '_enable_profile_autoconnect',
                return_value=(0, '', ''),
        ):
            result = wifi._ensure_profile_autoconnect('uuid-home')

        self.assertNotEqual(result[0], 0)
        self.assertIn('disabled', result[2])

    def test_unblock_forces_modify_then_verifies_even_when_enabled(self):
        with mock.patch.object(
                wifi,
                '_enable_profile_autoconnect',
                return_value=(0, '', ''),
        ) as enable, mock.patch.object(
                wifi,
                '_profile_autoconnect',
                return_value=(0, 'yes\n', '', True),
        ) as read:
            result = wifi._unblock_profile_autoconnect('uuid-home')

        self.assertEqual(result[0], 0)
        enable.assert_called_once_with('uuid-home')
        read.assert_called_once_with('uuid-home')

    def test_unblock_false_readback_is_a_visible_failure(self):
        with mock.patch.object(
                wifi,
                '_enable_profile_autoconnect',
                return_value=(0, '', ''),
        ), mock.patch.object(
                wifi,
                '_profile_autoconnect',
                return_value=(0, 'no\n', '', False),
        ):
            result = wifi._unblock_profile_autoconnect('uuid-home')

        self.assertNotEqual(result[0], 0)
        self.assertIn('disabled', result[2])

    def test_unblock_can_rewrite_and_preserve_disabled_policy(self):
        with mock.patch.object(
                wifi,
                '_run',
                return_value=(0, '', ''),
        ) as run, mock.patch.object(
                wifi,
                '_profile_autoconnect',
                return_value=(0, 'no\n', '', False),
        ):
            result = wifi._unblock_profile_autoconnect(
                'uuid-home',
                enabled=False,
            )

        self.assertEqual(result[0], 0)
        run.assert_called_once_with(
            [
                '--wait',
                str(wifi._NMCLI_PROFILE_WAIT_SEC),
                'connection',
                'modify',
                'uuid',
                'uuid-home',
                'connection.autoconnect',
                'no',
            ],
            timeout=wifi._NMCLI_PROFILE_TIMEOUT_SEC,
        )


class SupervisorIntegrationTests(unittest.TestCase):
    def setUp(self):
        wifi._reauth_required.clear()
        wifi._autoconnect_restore.clear()

    def test_verified_connection_registers_supervisor(self):
        net = wifi.Network(
            'Holmes Guest',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)
        profiles = wifi.ProfileDiscoveryResult({
            'Holmes Guest': [
                wifi.SavedProfile(
                    'Holmes Guest',
                    'holmes-profile',
                    'uuid-home',
                ),
            ],
        })
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=profiles,
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-home', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_ensure_profile_autoconnect',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_supervisor_watch_connected',
                ) as watch:
            wifi._connect_worker(job, net, None)

        self.assertTrue(job.ok)
        watch.assert_called_once_with(
            'uuid-home',
            'Holmes Guest',
            'wlan0',
        )

    def test_autoconnect_warning_connection_is_still_supervised(self):
        net = wifi.Network(
            'Holmes Guest',
            security='WPA2',
            saved=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('connect', net.ssid)
        profiles = wifi.ProfileDiscoveryResult({
            'Holmes Guest': [
                wifi.SavedProfile(
                    'Holmes Guest',
                    'holmes-profile',
                    'uuid-home',
                ),
            ],
        })
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    '_saved_profiles_detailed',
                    return_value=profiles,
                ), \
                mock.patch.object(
                    wifi,
                    '_activate_profile',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_verify_connected',
                    return_value=(True, 'uuid-home', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_ensure_profile_autoconnect',
                    return_value=(1, '', 'readback failed'),
                ), \
                mock.patch.object(
                    wifi,
                    '_supervisor_watch_connected',
                ) as watch:
            wifi._connect_worker(job, net, None)

        self.assertTrue(job.ok)
        self.assertEqual(job.code, 'connected_warning')
        watch.assert_called_once_with(
            'uuid-home',
            'Holmes Guest',
            'wlan0',
        )

    def test_verified_disconnect_marks_user_intent(self):
        net = wifi.Network(
            'Holmes Guest',
            saved=True,
            active=True,
            iface='wlan0',
            profile_uuid='uuid-home',
        )
        job = wifi.WifiJob('disconnect', net.ssid)
        with mock.patch.object(wifi, '_HAVE_NMCLI', True), \
                mock.patch.object(
                    wifi,
                    'active_connection',
                    side_effect=[
                        connected(),
                        disconnected('39 (User requested)'),
                    ],
                ), \
                mock.patch.object(
                    wifi,
                    '_run',
                    return_value=(0, '', ''),
                ), \
                mock.patch.object(
                    wifi,
                    '_supervisor_note_intentional_disconnect',
                ) as note, \
                mock.patch.object(
                    wifi,
                    '_unblock_profile_autoconnect',
                ) as unblock:
            wifi._disconnect_worker(job, net)

        self.assertTrue(job.ok)
        note.assert_called_once_with('uuid-home')
        unblock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
