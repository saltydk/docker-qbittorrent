import json
from dataclasses import replace
from pathlib import Path
import unittest

from scripts.manage_builds import load_states
from scripts import notification


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.states = load_states(Path(__file__).resolve().parents[1])
        self.states['libtorrent2'] = replace(self.states['libtorrent2'], release='release-5.2.3_v2.0.14', revision=5)

    def render(self, selected=('libtorrent2',), update=None, previous=None, event_name='schedule'):
        return notification.build_data(self.states, selected, update=update, previous=previous or {},
                                       event_name=event_name, event={'head_commit': {'message': 'fix(build): correct inputs'}},
                                       run_id=123, run_attempt=2)

    def test_revision_only_has_arrow_only_on_revision(self):
        data = self.render(update={'variants': {'libtorrent2': {'current': 'release-5.2.3_v2.0.14-4', 'reasons': ['revision']}}, 'images': []})
        self.assertEqual(data['fields'], [{'name': 'libtorrent2', 'value': 'qBittorrent: 5.2.3\nlibtorrent: 2.0.14\nRevision: 4 → 5', 'inline': True}])
        self.assertIn('revision', data['event_details'].lower())
        self.assertEqual((data['run_id'], data['run_attempt']), (123, 2))

    def test_release_changes_split_qbittorrent_and_libtorrent(self):
        for old, expected in [('release-5.2.2_v2.0.14-5', 'qBittorrent: 5.2.2 → 5.2.3\nlibtorrent: 2.0.14\nRevision: 5'),
                              ('release-5.2.3_v2.0.13-5', 'qBittorrent: 5.2.3\nlibtorrent: 2.0.13 → 2.0.14\nRevision: 5')]:
            with self.subTest(old=old):
                data = self.render(update={'variants': {'libtorrent2': {'current': old, 'reasons': ['release']}}, 'images': []})
                self.assertEqual(data['fields'][0]['value'], expected)

    def test_base_change_links_commits_without_claiming_package_versions_changed(self):
        old, new = 'a' * 40, 'b' * 40
        data = self.render(selected=tuple(self.states), update={
            'variants': {name: {'current': state.versioned_tag, 'reasons': ['base', 'packages']} for name, state in self.states.items()},
            'images': [{'inputs': [{'name': 'base image', 'old': f'saltydk/alpine-s6overlay:sha-{old}@sha256:abc', 'new': f'saltydk/alpine-s6overlay:sha-{new}@sha256:def'}], 'changes': []}]})
        self.assertEqual(len(data['fields']), 3)
        self.assertIn(f'/commit/{old}', data['event_details'])
        self.assertIn(f'/commit/{new}', data['event_details'])
        self.assertNotIn('package versions updated', data['event_details'].lower())
        self.assertTrue(all('→' not in f['value'] for f in data['fields']))

    def test_pending_publication_and_packages_are_explicit(self):
        for reason, label in [('pending-publication', 'Publication retry'), ('packages', 'Package locks refreshed')]:
            data = self.render(update={'variants': {'libtorrent2': {'current': self.states['libtorrent2'].versioned_tag, 'reasons': [reason]}}, 'images': []})
            self.assertIn(label, data['event_details'])

    def test_push_uses_commit_and_previous_state(self):
        prior = replace(self.states['libtorrent2'], revision=4)
        data = self.render(event_name='push', previous={'libtorrent2': prior})
        self.assertIn('fix(build): correct inputs', data['event_details'])
        self.assertIn('Revision: 4 → 5', data['fields'][0]['value'])

    def test_manual_build_and_missing_update_report_are_distinct(self):
        self.assertIn('Manual build', self.render(event_name='workflow_dispatch')['event_details'])
        self.assertIn('details unavailable', self.render()['event_details'])

    def test_push_base_update_includes_commit_links(self):
        before = replace(self.states['libtorrent2'], base_image='saltydk/alpine-s6overlay:sha-' + 'a' * 40 + '@sha256:' + 'b' * 64)
        self.states['libtorrent2'] = replace(before, base_image='saltydk/alpine-s6overlay:sha-' + 'c' * 40 + '@sha256:' + 'd' * 64)
        data = self.render(event_name='push', previous={'libtorrent2': before})
        self.assertIn('/commit/' + 'a' * 40, data['event_details'])
        self.assertIn('/commit/' + 'c' * 40, data['event_details'])

    def test_push_unknown_previous_versions_are_explicit(self):
        data = self.render(event_name='push')
        self.assertIn('Previous input details unavailable', data['event_details'])
