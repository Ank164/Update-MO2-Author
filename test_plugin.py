"""Offline checks against MO2's bundled Qt; no installed mods are changed."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import unicodedata  # Load the test interpreter's extension before MO2's library path.
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
app.addLibraryPath(str(ROOT / 'dlls'))


class Tests(unittest.TestCase):
    def test_blank_ok_skips_unknown_and_preserves_other_authors_in_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            paths, mods = {}, {}
            for name, author in [('Found', ''), ('Unknown', ''), ('Existing', 'Known Author')]:
                directory = Path(folder) / name
                directory.mkdir()
                paths[name] = directory / 'meta.ini'
                paths[name].write_text(f'[General]\nauthor={author}\n', encoding='utf-8')
                mods[name] = types.SimpleNamespace(
                    absolutePath=lambda directory=directory: str(directory), nexusId=lambda: 0,
                    gameName=lambda: 'SkyrimSE', repository=lambda: '')
            unchanged = {name: paths[name].read_bytes() for name in ('Unknown', 'Existing')}
            obj = plugin.NexusAuthorMetadata()
            refreshed = []
            obj.organizer = types.SimpleNamespace(
                modList=lambda: types.SimpleNamespace(getMod=mods.get), pluginDataPath=lambda: folder,
                refresh=lambda: refreshed.append(True), onNextRefresh=lambda done, immediate: done())
            def lookup(row, done):
                done({'author': 'Fetched Creator', 'url': 'https://dwemermods.com/mods/1'}
                     if row['mod'] == 'Found' else {})
            with patch.object(plugin.DwemerClient, 'lookup', side_effect=lookup), \
                 patch.object(plugin.QTimer, 'singleShot', side_effect=lambda delay, done: done()), \
                 patch.object(plugin, 'confirm_overwrite', return_value=True), \
                 patch.object(plugin, 'ask_author', side_effect=[('ok', '   '), ('ok', '')]):
                obj.start(list(mods))
            self.assertEqual(plugin.metadata(paths['Found'])['author'], 'Fetched Creator')
            self.assertEqual([row['status'] for row in obj.rows], ['Updated', 'Skipped: no author entered', 'Skipped: no author entered'])
            self.assertTrue(all(paths[name].read_bytes() == original for name, original in unchanged.items()))
            self.assertEqual(refreshed, [True])

    def test_blank_ok_accepts_dialog_but_all_remains_disabled(self):
        dialog = plugin.AuthorDialog(None, 'Unknown', 'No author found', 2)
        self.assertTrue(dialog.ok.isEnabled())
        self.assertFalse(dialog.all.isEnabled())
        dialog.ok.click()
        self.assertEqual(dialog.action, 'ok')
        self.assertEqual(dialog.result(), plugin.QDialog.DialogCode.Accepted)

    def test_browser_transport_keeps_requests_separate_and_ignores_cancelled_reply(self):
        from PyQt6.QtCore import QProcess
        with tempfile.TemporaryDirectory() as profile, patch.object(plugin, 'QProcess') as process_type:
            browser = plugin.DwemerBrowser(ROOT, Path(profile))
            process = process_type.return_value
            first, second = unittest.mock.Mock(), unittest.mock.Mock()
            one = browser.get('https://dwemermods.com/mods/1', first)
            two = browser.get('https://dwemermods.com/mods/2', second)
            self.assertNotEqual(one, two)
            process.write.assert_not_called()
            browser.receive({'event': 'ready'})
            self.assertEqual(process.write.call_count, 2)
            sent = [__import__('json').loads(call.args[0]) for call in process.write.call_args_list]
            self.assertEqual([item['url'] for item in sent], ['https://dwemermods.com/mods/1', 'https://dwemermods.com/mods/2'])
            browser.cancel(one)
            browser.receive({'id': one, 'html': 'Wrong row', 'error': ''})
            browser.receive({'id': two, 'html': 'Second row', 'error': ''})
            first.assert_not_called()
            second.assert_called_once_with('Second row', '')
            browser.timer.stop()

    def test_session_client_uses_browser_caches_result_and_cancels_pending_fetch(self):
        callbacks = []
        browser = types.SimpleNamespace(get=unittest.mock.Mock(side_effect=lambda url, done: callbacks.append(done) or 'request'),
                                        cancel=unittest.mock.Mock())
        client = plugin.DwemerClient(browser=browser)
        found = []
        client.get('https://dwemermods.com/mods/1', lambda html, error: found.append((html, error)))
        callbacks[0]('Creator page', '')
        client.get('https://dwemermods.com/mods/1', lambda html, error: found.append((html, error)))
        self.assertEqual(browser.get.call_count, 1)
        self.assertEqual(found, [('Creator page', ''), ('Creator page', '')])
        client.get('https://dwemermods.com/mods/2', lambda html, error: found.append((html, error)))
        client.close()
        browser.cancel.assert_called_once_with('request')
        callbacks[-1]('Late page', '')
        self.assertEqual(len(found), 2)

    def test_helper_allows_only_dwemer_catalogue_and_detail_requests(self):
        spec = importlib.util.spec_from_file_location('browser_helper', REPO / 'nexus_author_support' / 'dwemer_browser.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        for url in ['https://dwemermods.com/mods?search=Example', 'https://dwemermods.com/mods/123?lang=en']:
            self.assertTrue(helper.allowed_url(url))
        for url in ['https://example.com/', 'https://dwemermods.com/login', 'http://dwemermods.com/mods/1',
                    'https://dwemermods.com.evil.test/mods/1', 'https://dwemermods.com@evil.test/mods/1']:
            self.assertFalse(helper.allowed_url(url))

    def test_author_update_waits_until_sign_in_window_is_closed(self):
        obj = plugin.NexusAuthorMetadata()
        obj.browser = types.SimpleNamespace(login_open=True)
        with patch.object(plugin.QMessageBox, 'information') as message:
            obj.start(['Example'])
        self.assertFalse(obj.busy)
        self.assertEqual(message.call_count, 1)

    def test_manual_batch_saves_when_meta_ini_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            mods, paths = {}, {}
            for name in ("RB's set N4 - PBR", "RB's set N8 - PBR"):
                directory = Path(folder) / name
                directory.mkdir()
                paths[name] = directory / 'meta.ini'
                mods[name] = types.SimpleNamespace(
                    absolutePath=lambda directory=directory: str(directory), nexusId=lambda: -1,
                    gameName=lambda: 'SkyrimSE', repository=lambda: '')
            obj = plugin.NexusAuthorMetadata()
            refreshed = []
            obj.organizer = types.SimpleNamespace(
                modList=lambda: types.SimpleNamespace(getMod=mods.get), pluginDataPath=lambda: folder,
                refresh=lambda: refreshed.append(True), onNextRefresh=lambda done, immediate: done())
            with patch.object(plugin.DwemerClient, 'lookup', side_effect=lambda row, done: done({})), \
                 patch.object(plugin.QTimer, 'singleShot', side_effect=lambda delay, done: done()), \
                 patch.object(plugin, 'ask_author', return_value=('all', 'RB')):
                obj.start(list(mods))
            self.assertEqual([row['status'] for row in obj.rows], ['Updated', 'Updated'])
            self.assertTrue(all(plugin.metadata(path)['author'] == 'RB' for path in paths.values()))
            self.assertTrue(all([p.name for p in path.parent.iterdir()] == ['meta.ini'] for path in paths.values()))
            self.assertEqual(refreshed, [True])

    def test_missing_metadata_save_preserves_a_competing_new_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meta.ini'
            original_link = plugin.os.link
            def competing_create(temporary, destination):
                path.write_text('[General]\nauthor=Someone else\n', encoding='utf-8')
                original_link(temporary, destination)
            with patch.object(plugin.os, 'link', side_effect=competing_create):
                with self.assertRaisesRegex(OSError, 'created during update'):
                    plugin.write_author(path, 'RB', expected_author='')
            self.assertEqual(plugin.metadata(path)['author'], 'Someone else')
            self.assertEqual([p.name for p in path.parent.iterdir()], ['meta.ini'])

    def test_selected_dwemer_page_with_named_creator_saves_without_manual_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meta.ini'
            path.write_text('[General]\nauthor=\n', encoding='utf-8')
            mod = types.SimpleNamespace(absolutePath=lambda: folder, nexusId=lambda: 0,
                                        gameName=lambda: 'SkyrimSE', repository=lambda: '')
            obj = plugin.NexusAuthorMetadata()
            obj.organizer = types.SimpleNamespace(
                modList=lambda: types.SimpleNamespace(getMod=lambda name: mod),
                pluginDataPath=lambda: folder, refresh=lambda: None,
                onNextRefresh=lambda done, immediate: done())
            url = 'https://dwemermods.com/mods/5242?lang=en'
            detail = self.dwemer_detail('Firon').replace('/known-authors/?q=creator', '/known-authors/firon')
            def get(client, requested, done):
                done(self.search_card("Firon&#39;sFactory Hetz-Hexe 3BA SMP", 5242)
                     if '/mods?' in requested else detail, '')
            with patch.object(plugin.DwemerClient, 'get', new=get), \
                 patch.object(plugin.QTimer, 'singleShot', side_effect=lambda delay, done: done()), \
                 patch.object(plugin, 'choose_dwemer', return_value=url) as picker, \
                 patch.object(plugin, 'ask_author') as manual:
                obj.start(["Firon'sFactory Hetz-Hexe"])
            self.assertEqual(picker.call_count, 1)
            manual.assert_not_called()
            self.assertEqual(plugin.metadata(path)['author'], 'Firon')
            self.assertEqual(obj.rows[0]['source'], 'dwemer')
            self.assertFalse(obj.busy)

    def test_existing_authors_require_confirmation_before_lookups(self):
        for decision in ('cancel', 'overwrite', 'cancel_manual'):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as folder:
                mods, paths = {}, {}
                for name, author in [('Existing', 'Old Author'), ('Blank', '')]:
                    directory = Path(folder) / name
                    directory.mkdir()
                    paths[name] = directory / 'meta.ini'
                    paths[name].write_text(f'[General]\nauthor={author}\n', encoding='utf-8')
                    mods[name] = types.SimpleNamespace(
                        absolutePath=lambda directory=directory: str(directory), nexusId=lambda: 0,
                        gameName=lambda: 'SkyrimSE', repository=lambda: '')
                obj = plugin.NexusAuthorMetadata()
                refresh = unittest.mock.Mock()
                obj.organizer = types.SimpleNamespace(
                    modList=lambda: types.SimpleNamespace(getMod=mods.get), pluginDataPath=lambda: folder,
                    refresh=refresh, onNextRefresh=lambda done, immediate: done())
                confirmed = []
                def confirm(parent, rows):
                    self.assertEqual([(row['mod'], row['original_author']) for row in rows], [('Existing', 'Old Author')])
                    confirmed.append(True)
                    return decision != 'cancel'
                def lookup(row, done):
                    self.assertTrue(confirmed)
                    self.assertEqual(plugin.metadata(paths['Existing'])['author'], 'Old Author')
                    done({})
                with patch.object(plugin, 'confirm_overwrite', side_effect=confirm) as confirm_box, \
                     patch.object(plugin.DwemerClient, 'lookup', side_effect=lookup) as search, \
                     patch.object(plugin.QTimer, 'singleShot', side_effect=lambda delay, done: done()), \
                     patch.object(plugin, 'ask_author', return_value=('cancel', '') if decision == 'cancel_manual' else ('all', 'New Author')):
                    obj.start(list(mods))
                self.assertEqual(confirm_box.call_count, 1)
                self.assertEqual(search.call_count, 0 if decision == 'cancel' else 2)
                self.assertFalse(obj.busy)
                self.assertEqual(plugin.metadata(paths['Existing'])['author'], 'New Author' if decision == 'overwrite' else 'Old Author')
                self.assertEqual(plugin.metadata(paths['Blank'])['author'], 'New Author' if decision == 'overwrite' else '')
                self.assertEqual(refresh.call_count, int(decision == 'overwrite'))

    def test_overwrite_rechecks_author_snapshot_and_preserves_unrelated_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meta.ini'
            path.write_text('[General]\nauthor=Old\nmodid=123\nnotes=Keep me\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'changed since confirmation'):
                plugin.write_author(path, 'New', expected_author='Wrong snapshot')
            self.assertEqual(plugin.metadata(path)['author'], 'Old')
            self.assertTrue(plugin.write_author(path, 'Créateur, Team', expected_author='Old'))
            self.assertEqual(plugin.metadata(path)['author'], 'Créateur, Team')
            settings = QSettings(str(path), QSettings.Format.IniFormat)
            self.assertEqual(settings.value('modid'), '123')
            self.assertEqual(settings.value('notes'), 'Keep me')
            self.assertFalse(plugin.write_author(path, 'Créateur, Team', expected_author='Créateur, Team'))
            with self.assertRaisesRegex(ValueError, 'changed since confirmation'):
                plugin.write_author(path, 'Another', expected_author='Old')

    def test_overwrite_dialog_cancel_default_and_buttons(self):
        from PyQt6.QtCore import QTimer
        for overwrite in (False, True):
            def click():
                dialog = app.activeModalWidget()
                self.assertEqual(dialog.defaultButton(), dialog.button(plugin.QMessageBox.StandardButton.Cancel))
                if overwrite:
                    next(button for button in dialog.buttons() if button.text() == 'Overwrite').click()
                else:
                    dialog.reject()
            QTimer.singleShot(0, click)
            self.assertEqual(plugin.confirm_overwrite(None, [{'mod': 'Example', 'original_author': 'Old'}]), overwrite)

    def test_dwemer_batch_saves_found_and_manual_authors_or_cancels_all(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as folder:
                mods = {}
                paths = {}
                for name in ('Found', 'Unresolved'):
                    directory = Path(folder) / name
                    directory.mkdir()
                    paths[name] = directory / 'meta.ini'
                    paths[name].write_text('[General]\nauthor=\n', encoding='utf-8')
                    mods[name] = types.SimpleNamespace(
                        absolutePath=lambda directory=directory: str(directory), nexusId=lambda: 0,
                        gameName=lambda: 'SkyrimSE', repository=lambda: '')
                obj = plugin.NexusAuthorMetadata()
                refreshed = []
                obj.organizer = types.SimpleNamespace(
                    modList=lambda: types.SimpleNamespace(getMod=mods.get), pluginDataPath=lambda: folder,
                    refresh=lambda: refreshed.append(True), onNextRefresh=lambda done, immediate: done())
                def lookup(row, done):
                    # Neither earlier lookups nor prompts may write before completion.
                    self.assertTrue(all(plugin.metadata(path)['author'] == '' for path in paths.values()))
                    done({'author': 'Found Creator', 'url': 'https://dwemermods.com/mods/1'}
                         if row['mod'] == 'Found' else {})
                with patch.object(plugin.DwemerClient, 'lookup', side_effect=lookup), \
                     patch.object(plugin.QTimer, 'singleShot', side_effect=lambda delay, done: done()), \
                     patch.object(plugin, 'ask_author', return_value=('cancel', '') if cancel else ('all', 'Manual Creator')):
                    obj.start(list(mods))
                self.assertFalse(obj.busy)
                self.assertEqual(plugin.metadata(paths['Found'])['author'], '' if cancel else 'Found Creator')
                self.assertEqual(plugin.metadata(paths['Unresolved'])['author'], '' if cancel else 'Manual Creator')
                self.assertEqual(len(refreshed), 0 if cancel else 1)

    def dwemer_detail(self, author='Real Creator'):
        return ('<script type="application/ld+json">{"author":{"name":"Uploader"}}</script>'
                '<div class="mod-meta-row"><span>Uploaded by: '
                '<a class="mod-author-link" href="/users/uploader">Uploader</a></span>'
                '<span>Created by: <a class="mod-category-link" href="/known-authors/?q=creator">'
                + author + '</a></span></div>'
                '<a class="mod-category-link" href="/known-authors/?q=other">Description credit</a>')

    def search_card(self, title, modid):
        return f'<h3 class="mod-title"><a href="/mods/{modid}">{title}</a></h3>'

    def dwemer_lookup(self, row, search_html):
        client = plugin.DwemerClient()
        urls, results = [], []
        def get(url, callback):
            urls.append(url)
            callback(search_html if '/mods?' in url else self.dwemer_detail(), '')
        client.get = get
        with patch.object(plugin.QTimer, 'singleShot', side_effect=lambda delay, done: done()):
            client.lookup(row, results.append)
        return urls, results[0]

    def test_dwemer_reads_creator_not_uploader_or_description_credits(self):
        page = plugin.DwemerPage(self.dwemer_detail('Cr&eacute;ateur &amp; Team'))
        self.assertEqual(page.author, 'Créateur & Team')
        self.assertEqual(plugin.DwemerPage('<script>{"author":"Uploader"}</script>').author, '')

    def test_dwemer_saved_url_avoids_name_search(self):
        urls, result = self.dwemer_lookup({'mod': 'Renamed', 'url': 'https://www.dwemermods.com/mods/3354#files'}, '')
        self.assertEqual(urls, ['https://dwemermods.com/mods/3354?lang=en'])
        self.assertEqual(result['author'], 'Real Creator')
        for url in ['https://evil.com/mods/1', 'https://dwemermods.com.evil.com/mods/1',
                    'https://dwemermods.com@evil.com/mods/1', 'https://dwemermods.com/mods', 'http://[']:
            self.assertEqual(plugin.dwemer_url(url), '')

    def test_dwemer_exact_name_and_archive_match(self):
        for row in [{'mod': 'Example Armor'}, {'mod': 'My renamed mod', 'installationFile': r'C:\Downloads\Example Armor.7z'}]:
            urls, result = self.dwemer_lookup(row, self.search_card('Example Armor', 12))
            self.assertEqual(result['author'], 'Real Creator')
            self.assertIn('/mods/12?', urls[-1])

    def test_dwemer_ambiguous_partial_and_paginated_results_need_picker(self):
        for html in [self.search_card('Example Armor', 1) + self.search_card('Example Armor', 2),
                     self.search_card('Example Armor Recolor', 1),
                     self.search_card('Example Armor', 1) + '<a href="/mods?search=Example&amp;page=2">2</a>']:
            urls, result = self.dwemer_lookup({'mod': 'Example Armor'}, html)
            self.assertNotIn('author', result)
            self.assertTrue(result['candidates'])
            self.assertTrue(all('/mods?' in url for url in urls))

    def test_dwemer_search_uses_cleaned_archive_and_skips_other_games(self):
        urls, result = self.dwemer_lookup({'mod': 'Renamed', 'installationFile': 'Example_Armor-12345-1-0.7z'}, '')
        self.assertTrue(any('search=Example+Armor' in url for url in urls))
        self.assertEqual(result, {'candidates': []})
        urls, result = self.dwemer_lookup({'mod': 'Example Armor', 'game': 'Fallout4'}, '')
        self.assertEqual(urls, [])
        self.assertIn('supports Skyrim', result['error'])

    def test_dwemer_result_picker_and_cancel(self):
        for choice in (None, '', 'https://dwemermods.com/mods/12?lang=en'):
            obj = plugin.NexusAuthorMetadata()
            obj.busy = True
            obj.token = 'batch'
            obj.rows = [{'mod': 'Test', 'status': 'Failed: Nexus'}]
            obj.progress = types.SimpleNamespace(hide=lambda: None, show=lambda: None)
            obj.finish = lambda: setattr(obj, 'busy', False)
            obj.dwemer = types.SimpleNamespace(detail=unittest.mock.Mock())
            with patch.object(plugin, 'choose_dwemer', return_value=choice), patch.object(plugin.QTimer, 'singleShot') as schedule:
                obj.dwemer_result('batch', obj.rows[0], {'candidates': [{'title': 'Test', 'url': 'url'}]})
            if choice is None:
                self.assertFalse(obj.busy)
                schedule.assert_not_called()
            elif choice:
                self.assertEqual(obj.dwemer.detail.call_args.args[0], choice)
            else:
                self.assertIn('dwemer_status', obj.rows[0])
                self.assertEqual(schedule.call_count, 1)

    def test_dwemer_picker_can_select_page_manual_or_cancel(self):
        candidates = [{'title': 'Example Armor', 'url': 'https://dwemermods.com/mods/12?lang=en'}]
        for index, accepted, expected in [(1, True, candidates[0]['url']), (0, True, ''), (0, False, None)]:
            with patch.object(plugin.QInputDialog, 'getItem', side_effect=lambda parent, title, label, items, current, editable: (items[index], accepted)):
                self.assertEqual(plugin.choose_dwemer(None, 'Installed name', candidates), expected)

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
                with patch.object(plugin, 'ask_author', return_value=('ok', 'Manual Author')) as prompt, \
                     patch.object(plugin.DwemerClient, 'lookup', side_effect=lambda row, done: done({})), \
                     patch.object(plugin.QTimer, 'singleShot', side_effect=lambda delay, callback: callback()):
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
            with patch.object(plugin, 'ask_author', return_value=('ok', 'Créateur, Team')) as prompt:
                obj.prompt_missing_authors()
            self.assertIn('Local mod', prompt.call_args.args[1])
            obj.apply()
            self.assertEqual(plugin.metadata(path)['author'], 'Créateur, Team')
            self.assertEqual(obj.rows[0]['status'], 'Updated')
            self.assertEqual(refreshed, [True])

    def manual_batch(self):
        obj = plugin.NexusAuthorMetadata()
        obj.rows = [dict(mod=str(i), game='SkyrimSE', modid=i, status=status)
                    for i, status in enumerate([
                        'Failed: unavailable', 'Skipped: Nexus returned no author',
                        'Skipped: no Nexus game/mod ID', 'Ready', 'Skipped: author already present'])]
        return obj

    def test_all_sets_remaining_mods_and_preserves_other_authors(self):
        obj = self.manual_batch()
        untouched = [dict(row) for row in obj.rows[3:]]
        with patch.object(plugin, 'ask_author', return_value=('all', 'Team')) as prompt:
            self.assertTrue(obj.prompt_missing_authors())
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(prompt.call_args.args[3], 3)
        self.assertEqual([row['author'] for row in obj.rows[:3]], ['Team'] * 3)
        self.assertEqual(obj.rows[3:], untouched)

    def test_ok_advances_and_all_only_sets_remaining_mods(self):
        obj = self.manual_batch()
        with patch.object(plugin, 'ask_author', side_effect=[('ok', 'First'), ('all', 'Team')]) as prompt:
            self.assertTrue(obj.prompt_missing_authors())
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual([row['author'] for row in obj.rows[:3]], ['First', 'Team', 'Team'])

    def test_cancel_discards_entire_batch_before_saving(self):
        obj = self.manual_batch()
        obj.busy = True
        obj.dwemer_queue = []
        obj.token = 'test'
        obj.progress = types.SimpleNamespace(hide=lambda: None)
        obj.finish = lambda: setattr(obj, 'busy', False)
        obj.organizer = types.SimpleNamespace(onNextRefresh=unittest.mock.Mock())
        with patch.object(plugin, 'ask_author', side_effect=[('ok', 'First'), ('cancel', '')]) as prompt:
            obj.next_dwemer('test')
        self.assertFalse(obj.busy)
        obj.organizer.onNextRefresh.assert_not_called()
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(obj.rows[0]['status'], 'Cancelled: no change made')
        self.assertEqual(obj.rows[3]['status'], 'Cancelled: no change made')

    def test_dialog_buttons_and_blank_author(self):
        for action in ('ok', 'all', 'cancel'):
            with self.subTest(action=action):
                dialog = plugin.AuthorDialog(None, 'Test mod', 'Nexus unavailable', 3)
                self.assertTrue(dialog.ok.isEnabled())
                self.assertFalse(dialog.all.isEnabled())
                dialog.author.setText('   ')
                self.assertTrue(dialog.ok.isEnabled())
                dialog.author.setText('  Author  ')
                self.assertTrue(dialog.ok.isEnabled())
                self.assertTrue(dialog.all.isEnabled())
                if action == 'cancel':
                    dialog.reject()
                    self.assertEqual(dialog.result(), plugin.QDialog.DialogCode.Rejected)
                else:
                    getattr(dialog, action).click()
                    self.assertEqual(dialog.action, action)
                    self.assertEqual(dialog.result(), plugin.QDialog.DialogCode.Accepted)

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
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[1].text(), 'Sign in to Dwemer…')
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
