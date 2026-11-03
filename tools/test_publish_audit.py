"""Checks publication boundaries with synthetic credentials only."""
import unittest
from prepare_github_upload import inspect, source_files


class PublicationTests(unittest.TestCase):
    def test_provider_credential_is_blocked_without_returning_value(self):
        value = ('sk-' + 'a' * 40).encode()
        report = inspect({'bad.txt': value})
        self.assertEqual([{'path': 'bad.txt', 'type': 'provider_key'}], report)
        self.assertNotIn(value.decode(), str(report))

    def test_github_credential_is_blocked(self):
        self.assertTrue(inspect({'bad.txt': ('ghp_' + 'b' * 36).encode()}))

    def test_encrypted_credential_file_is_blocked(self):
        self.assertTrue(inspect({'settings.json': b'{"secret":"encrypted-placeholder"}'}))

    def test_empty_credential_placeholder_is_allowed(self):
        self.assertFalse(inspect({'settings.json': b'{"api_key":""}'}))

    def test_runtime_backups_and_reports_are_not_exported(self):
        files = source_files()
        self.assertFalse(any(name.startswith('feige-agent/data/') for name in files))
        self.assertFalse(any('/backups/' in name or '/verification/' in name for name in files))
        self.assertTrue('feige-agent/app/service/fixtures.json' in files)
        self.assertTrue('feige-fashion-customer-service/scripts/merchant.py' in files)

    def test_current_source_is_clean(self):
        self.assertEqual([], inspect(source_files()))


if __name__ == '__main__':
    unittest.main()
