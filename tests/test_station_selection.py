import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

from bartdisplay import config, departures, stations


class StationDataTests(unittest.TestCase):
    def test_directory_is_unique_and_alphabetical(self):
        with mock.patch.object(stations, '_request', return_value=[
            {'abbr': 'MONT', 'name': 'Montgomery St.'},
            {'abbr': 'ANTC', 'name': 'Antioch'},
            {'abbr': '12TH', 'name': '12th St. Oakland City Center'},
            {'abbr': 'MONT', 'name': 'Montgomery St.'},
        ]):
            self.assertEqual([code for code, _ in stations.fetch_stations()],
                             ['12TH', 'ANTC', 'MONT'])

    def test_platforms_combine_directions_and_sort_numerically(self):
        with mock.patch.object(stations, '_request', return_value={
            'abbr': 'TEST', 'north_platforms': {'platform': ['3', '2', '10']},
            'south_platforms': {'platform': '2'},
        }):
            self.assertEqual(stations.fetch_platforms('TEST'), ['2', '3', '10'])

    def test_missing_platform_metadata_does_not_invent_platforms(self):
        with mock.patch.object(stations, '_request', return_value={'abbr': 'TEST'}):
            with self.assertRaises(ValueError):
                stations.fetch_platforms('TEST')


class SelectionConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'config.json'
        path.write_text(json.dumps({'station': 'MONT', 'station_name': 'Montgomery St.',
                                    'platform': '2', 'api_key': 'keep-me', 'extra': True}))
        self.path = path
        patcher = mock.patch.object(config, 'CONFIG_PATH', str(path))
        patcher.start()
        self.addCleanup(patcher.stop)
        state = mock.patch.object(config, '_cfg', {})
        state.start()
        self.addCleanup(state.stop)
        config.load()

    def test_selection_survives_reload_and_preserves_other_fields(self):
        config.set_selection('EMBR', 'Embarcadero', '1')
        config.load()
        self.assertEqual(config.get_selection(), ('EMBR', '1'))
        self.assertEqual(config.get_station_name(), 'EMBARCADERO')
        disk = json.loads(self.path.read_text())
        self.assertEqual(disk['api_key'], 'keep-me')
        self.assertTrue(disk['extra'])

    def test_failed_save_preserves_previous_selection(self):
        with mock.patch.object(config.os, 'replace', side_effect=OSError):
            with self.assertRaises(OSError):
                config.set_selection('EMBR', 'Embarcadero', '1')
        self.assertEqual(config.get_selection(), ('MONT', '2'))
        self.assertEqual(json.loads(self.path.read_text())['station'], 'MONT')


class DepartureSwitchTests(unittest.TestCase):
    def test_old_inflight_response_cannot_publish_after_switch(self):
        started, finish = threading.Event(), threading.Event()
        target = ['MONT', '2']

        def fetch(selection):
            self.assertEqual(selection, ('MONT', '2'))
            started.set()
            finish.wait(2)
            return [{'destination': 'OLD', 'minutes': ['1']}]

        with mock.patch.object(config, 'get_selection', side_effect=lambda: tuple(target)), \
                mock.patch.object(departures, '_fetch', side_effect=fetch):
            worker = threading.Thread(target=departures._poll_once)
            worker.start()
            self.assertTrue(started.wait(2))
            target[:] = ['EMBR', '1']
            departures.refresh()
            finish.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(departures.snapshot(), ([], True))
            self.assertTrue(departures._wake.is_set())
        with mock.patch.object(departures, '_fetch', return_value=[{'destination': 'NEW'}]):
            departures._poll_once()
        self.assertEqual(departures.snapshot(), ([{'destination': 'NEW'}], False))

    def test_old_inflight_failure_does_not_clear_new_loading_state(self):
        started, finish = threading.Event(), threading.Event()
        target = ['MONT', '2']

        def fetch(selection):
            started.set()
            finish.wait(2)
            raise OSError('offline')

        with mock.patch.object(config, 'get_selection', side_effect=lambda: tuple(target)), \
                mock.patch.object(departures, '_fetch', side_effect=fetch):
            worker = threading.Thread(target=departures._poll_once)
            worker.start()
            self.assertTrue(started.wait(2))
            target[:] = ['MCAR', '4']
            departures.refresh()
            finish.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(departures.snapshot(), ([], True))

    def test_fetch_filters_platform_even_with_single_estimate_shape(self):
        response = mock.Mock()
        response.json.return_value = {'root': {'station': {'etd': [
            {'destination': 'Wanted', 'estimate': {'platform': '3', 'minutes': '5'}},
            {'destination': 'Other', 'estimate': [{'platform': '1', 'minutes': '1'}]},
        ]}}}
        with mock.patch.object(departures.requests, 'get', return_value=response) as get:
            result = departures._fetch(('TEST', '3'))
        self.assertEqual(result, [{'destination': 'WANTED', 'minutes': ['5']}])
        self.assertEqual(get.call_args.kwargs['params']['orig'], 'TEST')


class PickerTests(unittest.TestCase):
    def setUp(self):
        from bartdisplay.ui.station_picker import StationPicker
        self.picker = StationPicker()
        self.picker.active = True

    def test_cancel_does_not_save(self):
        with mock.patch.object(config, 'set_selection') as save:
            self.picker.close()
        save.assert_not_called()

    def test_apply_saves_complete_selection_and_refreshes(self):
        self.picker.selected = ('EMBR', 'Embarcadero')
        self.picker.platforms = ['1', '2']
        self.picker.platform = '1'
        with mock.patch.object(config, 'set_selection') as save, \
                mock.patch.object(departures, 'refresh') as refresh:
            self.picker._apply()
        save.assert_called_once_with('EMBR', 'Embarcadero', '1')
        refresh.assert_called_once()
        self.assertFalse(self.picker.active)

    def test_save_error_keeps_picker_open_and_does_not_refresh(self):
        self.picker.selected = ('EMBR', 'Embarcadero')
        self.picker.platforms = ['1']
        self.picker.platform = '1'
        with mock.patch.object(config, 'set_selection', side_effect=OSError), \
                mock.patch.object(departures, 'refresh') as refresh:
            self.picker._apply()
        refresh.assert_not_called()
        self.assertTrue(self.picker.active)
        self.assertIn('Could not save', self.picker.error)

    def test_back_discards_late_platform_result(self):
        self.picker.view = 'platforms'
        self.picker._request_id = 1
        self.picker._back()
        self.picker._results.put((1, ['3'], ''))
        self.picker.update()
        self.assertEqual(self.picker.platforms, [])
        self.assertEqual(self.picker.view, 'stations')

    def test_new_station_requires_platform_choice(self):
        self.picker.view = 'platforms'
        self.picker.selected = ('EMBR', 'Embarcadero')
        self.picker._results.put((0, ['1', '2'], ''))
        with mock.patch.object(config, 'get_selection', return_value=('MONT', '2')):
            self.picker.update()
        self.assertIsNone(self.picker.platform)

    def test_scroll_is_clamped_and_scrolling_cannot_select_a_row(self):
        self.picker.entries = [(str(i), str(i)) for i in range(50)]
        self.picker._scroll(100)
        self.assertEqual(self.picker.offset, 46)
        self.picker._scroll(-100)
        self.assertEqual(self.picker.offset, 0)
        button = mock.Mock()
        self.picker.handle({'kind': 'press', 'x': 50, 'y': 100})
        self.picker.handle({'kind': 'drag', 'x': 50, 'y': 40, 'dy': -60})
        self.picker._buttons = [button]
        self.picker.handle({'kind': 'release', 'x': 50, 'y': 100, 'tap': True})
        button.on_tap.assert_not_called()

    def test_platform_label_opens_current_station_without_station_list(self):
        with mock.patch.object(config, 'get_station', return_value='MCAR'), \
                mock.patch.object(config, 'get_station_name', return_value='MACARTHUR'), \
                mock.patch.object(self.picker, '_load') as load:
            self.picker.open_platforms()
        self.assertEqual(self.picker.view, 'platforms')
        self.assertEqual(self.picker.selected, ('MCAR', 'MACARTHUR'))
        self.assertFalse(self.picker._return_to_stations)
        load.assert_called_once()
        self.picker._results.put((self.picker._request_id, ['1', '2', '3', '4'], ''))
        with mock.patch.object(config, 'get_selection', return_value=('MCAR', '3')):
            self.picker.update()
        self.assertEqual(self.picker.platform, '3')

    def test_scrollbar_drag_reaches_last_station_without_selecting(self):
        self.picker.entries = [(str(i), str(i)) for i in range(50)]
        self.picker.handle({'kind': 'press', 'x': 440, 'y': 110})
        self.picker.handle({'kind': 'drag', 'x': 440, 'y': 220, 'dy': 110})
        self.picker.handle({'kind': 'release', 'x': 440, 'y': 220, 'tap': True})
        self.assertEqual(self.picker.offset, 46)
        self.assertIsNone(self.picker.selected)
        self.picker.handle({'kind': 'press', 'x': 440, 'y': 110})
        self.assertLess(self.picker.offset, 46)

    def test_cancel_and_reopen_ignore_late_metadata(self):
        with mock.patch.object(config, 'get_station', return_value='MONT'), \
                mock.patch.object(config, 'get_station_name', return_value='MONTGOMERY'), \
                mock.patch.object(self.picker, '_load'):
            self.picker.open_platforms()
            token = self.picker._request_id
            self.picker.close()
            self.picker.open()
        self.picker._results.put((token, ['3', '4'], ''))
        self.picker.update()
        self.assertEqual(self.picker.entries, [])
        self.assertEqual(self.picker.platforms, [])

    def test_invalid_platform_cannot_save(self):
        self.picker.selected = ('MONT', 'Montgomery St.')
        self.picker.platforms = ['1', '2']
        self.picker.platform = '4'
        with mock.patch.object(config, 'set_selection') as save:
            self.picker._apply()
        save.assert_not_called()
        self.assertTrue(self.picker.active)

    def test_rendered_controls_follow_station_platform_apply_flow(self):
        import pygame
        from bartdisplay import board, display
        pygame.font.init()
        font = pygame.font.Font(str(Path(config.PROJECT_ROOT) /
                                    'fonts/PressStart2P-Regular.ttf'), 17)
        with mock.patch.object(display, 'screen', pygame.Surface((480, 320))), \
                mock.patch.object(display, 'font_xs', font), \
                mock.patch.object(config, 'get_selection', return_value=('MONT', '2')), \
                mock.patch.object(config, 'get_station', return_value='MONT'):
            self.assertTrue(board.station_tapped(
                {'kind': 'release', 'tap': True, 'x': 30, 'y': 20}))
            self.assertFalse(board.station_tapped(
                {'kind': 'release', 'tap': True, 'x': 450, 'y': 20}))
            self.picker.entries = [('EMBR', 'Embarcadero')]
            self.picker.render()
            with mock.patch.object(self.picker, '_load'):
                self.picker.handle({'kind': 'release', 'tap': True, 'x': 50, 'y': 80})
            self.assertEqual(self.picker.selected, ('EMBR', 'Embarcadero'))
            self.picker.platforms = ['1', '2']
            self.picker.render()
            self.picker.handle({'kind': 'release', 'tap': True, 'x': 50, 'y': 80})
            self.assertEqual(self.picker.platform, '1')
            self.picker.render()
            with mock.patch.object(config, 'set_selection') as save, \
                    mock.patch.object(departures, 'refresh'):
                self.picker.handle({'kind': 'release', 'tap': True, 'x': 380, 'y': 250})
            save.assert_called_once_with('EMBR', 'Embarcadero', '1')
            self.assertFalse(self.picker.active)


if __name__ == '__main__':
    unittest.main()
