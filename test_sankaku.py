"""Regression checks for Sankaku renewal without account credentials or a database."""
import base64
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from adapters import BooruSource, SANKAKU_API
from relational import Relational, save_sankaku_session


def source(refresh=None, **settings):
    return BooruSource(dict(api='sankaku', url='https://chan.sankakucomplex.com',
                            api_url=SANKAKU_API, **settings), refresh_sankaku=refresh)


class Response(io.BytesIO):
    headers = {}


class SankakuTests(unittest.TestCase):
    def setUp(self):
        governor = patch('adapters.governor')
        governor.start()
        self.addCleanup(governor.stop)
        opener = patch('adapters.build_opener')
        self.opener = opener.start()
        self.addCleanup(opener.stop)

    def test_unauthorized_retries_even_with_one_attempt(self):
        refresh = Mock(return_value='new-access')
        client = source(refresh, api_key='old-access', cookie='refreshToken=old-refresh; lang=en')
        authorizations = []

        def open_posts(req, timeout):
            authorizations.append(req.get_header('Authorization'))
            self.assertEqual(req.get_header('Cookie'), 'lang=en')
            if len(authorizations) == 1:
                raise HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO())
            return Response(b'{"data":[]}')

        self.opener.return_value.open.side_effect = open_posts
        client.fetch('rating:s', delay=0, retries=1)
        refresh.assert_called_once_with('old-access', 45)
        self.assertEqual(authorizations, ['Bearer old-access', 'Bearer new-access'])

    def test_refreshes_before_search_without_a_current_token(self):
        expired = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip('=')
        for token in ('', f'header.{expired}.signature'):
            with self.subTest(expired=bool(token)):
                refresh = Mock(return_value='new-access')
                client = source(refresh, api_key=token, cookie='refreshToken=old-refresh',
                                requires_credentials=True)
                self.opener.return_value.open.return_value = Response(b'{"data":[]}')
                client.fetch('rating:s', delay=0, retries=1)
                refresh.assert_called_once_with(token, 45)
                req = self.opener.return_value.open.call_args.args[0]
                self.assertEqual(req.get_header('Authorization'), 'Bearer new-access')
                self.assertFalse(req.has_header('Cookie'))

    def test_exchange_sends_only_refresh_token_and_accepts_rotation(self):
        client = source(cookie='accessToken=old-access; refreshToken=old-refresh; lang=en')
        response = {'success': True, 'access_token': 'new-access', 'refresh_token': 'new-refresh'}
        self.opener.return_value.open.return_value = Response(json.dumps(response).encode())
        self.assertEqual(client.exchange_sankaku_token(10),
                         ('new-access', 'lang=en; refreshToken=new-refresh'))
        req = self.opener.return_value.open.call_args.args[0]
        self.assertEqual(req.full_url, SANKAKU_API + '/auth/token')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(json.loads(req.data), {'refresh_token': 'old-refresh'})
        self.assertIsNone(self.opener.call_args.args[0].redirect_request(
            req, None, 307, '', {}, 'https://other.example/auth/token'))

    def test_refresh_rejection_explains_how_to_replace_cookie(self):
        client = source(cookie='refreshToken=old-refresh')
        self.opener.return_value.open.side_effect = HTTPError(
            SANKAKU_API + '/auth/token', 401, 'Unauthorized', {}, io.BytesIO())
        with self.assertRaisesRegex(ValueError, 'Save a fresh refreshToken'):
            client.exchange_sankaku_token(10)

    def test_rotated_pair_is_loaded_from_the_current_credential_directory(self):
        db = Mock()
        db.raw_execute.side_effect = [
            Mock(description=[('api',), ('credential_ref',)], fetchone=Mock(
                return_value=('sankaku', 'source-1-test'))),
            Mock(fetchall=Mock(return_value=[])),
            Mock(fetchall=Mock(return_value=[])),
            Mock(fetchall=Mock(return_value=[])),
            Mock(fetchone=Mock(return_value=None)),
        ]
        with TemporaryDirectory() as root, patch.dict(os.environ, {'BOORU_CREDENTIAL_DIR': root}):
            path = Path(root, 'source-1-test')
            path.mkdir()
            (path / 'cookie').write_text('refreshToken=old-refresh')
            save_sankaku_session(path.name, 'new-access', 'refreshToken=new-refresh')
            config = Relational.source_config(db, 1)
        self.assertEqual(config['api_key'], 'new-access')
        self.assertEqual(config['cookie'], 'refreshToken=new-refresh')
