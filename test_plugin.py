"""Offline checks against MO2's bundled Qt; no installed mods are changed."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('MO2_DIR', str(REPO.parents[1])))
os.environ['QT_QPA_PLATFORM'] = 'windows'
os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(ROOT / 'platforms')
handles = [os.add_dll_directory(str(ROOT / p)) for p in ('dlls', 'plugins/plugin_python/dlls', '.')]
sys.path.insert(0, str(ROOT / 'plugins/plugin_python/libs'))
sys.modules['mobase'] = types.SimpleNamespace(IPlugin=object, VersionInfo=lambda *args: args)
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import QApplication, QMenu, QTreeView, QAbstractItemView
spec = importlib.util.spec_from_file_location('plugin', REPO / 'nexus_author_metadata.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
app = QApplication([])


class Tests(unittest.TestCase):
    def test_batch_falls_back_when_nexus_unavailable_or_mod_has_no_id(self):
        for modid in (0, 123):
            with self.subTest(modid=modid), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'meta.ini'
                path.write_text('[General]\nauthor=\n', encoding='utf-8')
                mod = types.SimpleNamespace(absolutePath=lambda: folder, nexusId=lambda: modid,
                                            gameName=lambda: 'SkyrimSE', repository=lambda: 'Nexus')
                obj = plugin.NexusAuthorMetadata()
                bridge = unittest.mock.Mock(side_effect=RuntimeError('Offline'))
                obj.organizer = types.SimpleNamespace(
                    modList=lambda: types.SimpleNamespace(getMod=lambda name: mod),
                    pluginDataPath=lambda: folder, refresh=lambda: None,
                    onNextRefresh=lambda callback, immediate: callback(), createNexusBridge=bridge)
                with patch.object(plugin.QInputDialog, 'getText', return_value=('Manual Author', True)) as prompt:
                    obj.start(['Local mod'])
                self.assertEqual(prompt.call_count, 1)
                self.assertEqual(bridge.call_count, int(modid > 0))
                self.assertEqual(plugin.metadata(path)['author'], 'Manual Author')
                self.assertFalse(obj.busy)

    def test_manual_fallback_saves_author_without_nexus_id(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meta.ini'
            path.write_text('[General]\nauthor=\nmodid=0\ngameName=\n', encoding='utf-8')
            mod = types.SimpleNamespace(absolutePath=lambda: folder, nexusId=lambda: 0, gameName=lambda: '')
            obj = plugin.NexusAuthorMetadata()
            obj.window = None
            obj.busy = True
            obj.token = 'test'
            obj.rows = [{'mod': 'Local mod', 'path': str(path), 'game': '', 'modid': 0,
                         'status': 'Skipped: no Nexus game/mod ID'}]
            refreshed = []
            obj.organizer = types.SimpleNamespace(
                modList=lambda: types.SimpleNamespace(getMod=lambda name: mod),
                pluginDataPath=lambda: folder, refresh=lambda: refreshed.append(True))
            obj.finish = lambda: None
            with patch.object(plugin.QInputDialog, 'getText', return_value=('  Créateur, Team  ', True)) as prompt:
                obj.prompt_missing_authors()
            self.assertIn('Local mod', prompt.call_args.args[2])
            obj.apply()
            self.assertEqual(plugin.metadata(path)['author'], 'Créateur, Team')
            self.assertEqual(obj.rows[0]['status'], 'Updated')
            self.assertEqual(refreshed, [True])

    def test_manual_fallback_skips_cancel_blank_and_existing_authors(self):
        obj = plugin.NexusAuthorMetadata()
        obj.window = None
        obj.rows = [dict(mod=str(i), game='SkyrimSE', modid=i, status=status)
                    for i, status in enumerate([
                        'Failed: unavailable', 'Skipped: Nexus returned no author',
                        'Ready', 'Skipped: author already present'])]
        original = [dict(row) for row in obj.rows]
        with patch.object(plugin.QInputDialog, 'getText', side_effect=[('Ignored', False), ('   ', True)]) as prompt:
            obj.prompt_missing_authors()
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(obj.rows, original)

    def test_timeout_offers_manual_fallback_and_preserves_fetched_authors(self):
        obj = plugin.NexusAuthorMetadata()
        obj.busy = True
        obj.current = ('SkyrimSE', 1)
        obj.groups = {obj.current: [{'status': ''}], ('SkyrimSE', 2): [{'status': ''}]}
        obj.queue = [('SkyrimSE', 2)]
        ready = {'status': 'Ready', 'author': 'Fetched'}
        obj.rows = [ready, *obj.groups[obj.current], *obj.groups[obj.queue[0]]]
        with patch.object(plugin.QTimer, 'singleShot') as schedule:
            obj.timeout()
        self.assertEqual(ready['status'], 'Ready')
        self.assertIn('timed out', obj.rows[1]['status'])
        self.assertIn('timeout', obj.rows[2]['status'])
        self.assertEqual(obj.queue, [])
        self.assertIsNone(obj.current)
        self.assertEqual(schedule.call_args.args[1], obj.next_request)

    def test_blank_repository_with_nexus_id_is_eligible(self):
        self.assertEqual(plugin.nexus_skip_reason('skyrimse', 35978, ''), '')
        self.assertEqual(plugin.nexus_skip_reason('SkyrimSE', 35978, ' Nexus '), '')
        self.assertEqual(plugin.nexus_skip_reason('SkyrimSE', 0, ''), 'Skipped: no Nexus game/mod ID')
        self.assertEqual(plugin.nexus_skip_reason('SkyrimSE', 35978, 'Other'), 'Skipped: non-Nexus repository')

    def test_qt_metadata_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meta.ini'
            original = b'[General]\r\nmodid=123\r\ngameName=SkyrimSE\r\nauthor=\r\nnotes="a,b"\r\n\r\n[Plugins]\r\nTest\\value=hello\r\n'
            path.write_bytes(original)
            before = QSettings(str(path), QSettings.Format.IniFormat)
            expected = {key: before.value(key) for key in before.allKeys() if key != 'author'}
            del before
            author = 'Créateur, "Name" \\ Team'
            self.assertTrue(plugin.write_author(path, author))
            after = QSettings(str(path), QSettings.Format.IniFormat)
            self.assertEqual(after.value('author'), author)
            self.assertEqual({key: after.value(key) for key in after.allKeys() if key != 'author'}, expected)
            self.assertFalse(plugin.write_author(path, 'Replacement'))
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['meta.ini'])

    def test_nexus_game_name_reply_completes_request(self):
        from PyQt6.QtCore import QTimer
        obj = plugin.NexusAuthorMetadata()
        obj.busy = True
        obj.token = 'batch'
        obj.current = ('SkyrimSE', 108546)
        row = {}
        obj.groups = {obj.current: [row]}
        obj.timer = QTimer()
        obj.timer.start(60000)
        obj.total = 1
        obj.queue = []
        values = []
        obj.progress = types.SimpleNamespace(setValue=values.append)
        obj.next_request = lambda: None
        obj.received('skyrimspecialedition', 108546, 'batch', {'author': 'Author'})
        self.assertEqual(row, {'status': 'Ready', 'author': 'Author'})
        self.assertFalse(obj.timer.isActive())
        self.assertIsNone(obj.current)
        self.assertEqual(values, [1])

    def test_nexus_error_game_name_stops_batch(self):
        from PyQt6.QtCore import QTimer
        obj = plugin.NexusAuthorMetadata()
        obj.busy = True
        obj.token = 'batch'
        obj.current = ('SkyrimSE', 1)
        first, second = {}, {}
        obj.groups = {obj.current: [first], ('SkyrimSE', 2): [second]}
        obj.queue = [('SkyrimSE', 2)]
        obj.timer = QTimer()
        obj.total = 2
        obj.progress = types.SimpleNamespace(setValue=lambda value: None)
        obj.next_request = lambda: None
        obj.failed('skyrimspecialedition', 1, 0, 'batch', 429, 'Rate limit')
        self.assertEqual(first['status'], 'Failed: Rate limit')
        self.assertFalse(obj.queue)
        self.assertIn('rate limit', second['status'])

    def test_menu_selection_and_no_duplicate(self):
        mod = types.SimpleNamespace(isSeparator=lambda: False, isForeign=lambda: False, isBackup=lambda: False, isOverwrite=lambda: False)
        obj = plugin.NexusAuthorMetadata()
        obj.organizer = types.SimpleNamespace(modList=lambda: types.SimpleNamespace(getMod=lambda name: mod))
        view = QTreeView()
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.setObjectName('modList')
        model = QStandardItemModel()
        model.appendRow(QStandardItem('First'))
        model.appendRow(QStandardItem('Second'))
        view.setModel(model)
        view.selectAll()
        menu = QMenu(view)
        obj.add_menu(menu, view)
        obj.add_menu(menu, view)
        actions = [action for action in menu.actions() if not action.isSeparator()]
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0].isEnabled())
        captured = []
        obj.start = lambda names: captured.extend(names)
        actions[0].trigger()
        app.processEvents()
        self.assertEqual(captured, ['First', 'Second'])

    def test_late_response_is_ignored(self):
        obj = plugin.NexusAuthorMetadata()
        obj.busy = False
        obj.received('SkyrimSE', 1, 'stale', {'author': 'Wrong'})
        obj.failed('SkyrimSE', 1, 0, 'stale', 429, 'limit')


if __name__ == '__main__':
    unittest.main()
