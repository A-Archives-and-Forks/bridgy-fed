"""Unit tests for nostr_hub.py."""
from datetime import datetime, timedelta
from threading import Barrier
import time
from unittest import skip
from unittest.mock import patch

from granary.nostr import (
    id_and_sign,
    id_to_uri,
    KIND_DELETE,
    KIND_NOTE,
    KIND_PROFILE,
    KIND_REACTION,
    KIND_RELAYS,
    uri_for,
    uri_to_id,
)
from granary.tests.test_nostr import (
    FakeConnection,
    NOW_TS,
    NPUB_URI,
    NSEC_URI,
    PRIVKEY,
    PUBKEY,
)
from oauth_dropins.webutil import util
from oauth_dropins.webutil.testutil import NOW
from oauth_dropins.webutil.util import json_dumps, json_loads
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

import common
import ids
from models import Object
import nostr_hub
from nostr_hub import AUTHOR_FILTER_KINDS
from nostr import Nostr, NostrRelay
from protocol import DELETE_TASK_DELAY
from .testutil import Fake, TestCase
from web import Web

BOB_PUBKEY = 'be7e55eb264470903bbcf1d02ea417b5e1d2cd788cd6155f8e0b361a2bea76ed'
BOB_NPUB_URI = 'nostr:npub1hel9t6exg3cfqwau78gzafqhkhsa9ntc3ntp2huwpvmp52l2wmksdr85t7'
BOB_NSEC_URI = 'nostr:nsec1al80skcswjnwpukq3cw24x9rvdwyel8qls6kcled3q9ethqflu4q30070v'
EVE_PUBKEY = 'bd19ea0297facfe0e766f08995a0a92ca1ea52bf5f664fe2487f7894a7b0a7ff'
EVE_NPUB_URI = 'nostr:npub1h5v75q5hlt87pemx7zyetg9f9js7554ltanylcjg0aufffas5lls5m6tcf'
EVE_NSEC_URI = 'nostr:nsec1ger8dg42xau7ctdaduv6wse8apzueqgye3l7ta6dcj4j7w07lqdq4d9rey'
FRANK_PUBKEY = '2032dba5fdf02ba4223381075da4ba7dc6cf976aacb2ca658f13e00d834a0e29'
FRANK_NPUB_URI = 'nostr:npub1yqedhf0a7q46gg3nsyr4mf960hrvl9m24jev5ev0z0sqmq62pc5stypxxz'
FRANK_NSEC_URI = 'nostr:nsec12hj6ylwt5kypmq6hs7tssy3h68hdy5kvwj9qwhgv60vh6qdud8vsd5c3ln'


@patch('secrets.token_urlsafe', return_value='sub123')
@patch('oauth_dropins.webutil.appengine_config.tasks_client.create_task')
class NostrHubTest(TestCase):
    def setUp(self):
        super().setUp()
        common.RUN_TASKS_INLINE = False

        self.alice = self.make_user(
            'fake:alice', cls=Fake, enabled_protocols=['nostr'],
            nostr_key_bytes=bytes.fromhex(PRIVKEY))

        self.bob = self.make_nostr('bob', BOB_NSEC_URI, BOB_NPUB_URI)

    def make_nostr(self, name, nsec, npub, **props):
        nip05 = f'{name}@example.com'
        profile = Object(id=f'nostr:nprofile{name}', nostr=id_and_sign({
            'kind': KIND_PROFILE,
            'pubkey': uri_to_id(npub),
            'content': json_dumps({
                'name': name.capitalize(),
                'picture': f'http://{name}/pic',
                'nip05': nip05,
            }),
        }, privkey=nsec))
        return self.make_user(npub, cls=Nostr, enabled_protocols=['fake'],
                              obj_key=profile.put(), valid_nip05=nip05, **props)

    def serve_and_subscribe(self, events):
        nostr_hub.init(subscribe=False)

        FakeConnection.to_receive = [
            ['EVENT', 'sub123', event] for event in events
        ] + [['EOSE', 'sub123']]

        nostr_hub.subscribe('wss://reelaay', limit=len(events) + 1)

        self.assertEqual(['wss://reelaay'], FakeConnection.relays)

    def test_init_load_users(self, _, __):
        nostr_hub.init(subscribe=False)
        self.assertEqual(set((PUBKEY,)), nostr_hub.bridged_pubkeys)
        self.assertEqual(set((BOB_PUBKEY,)), nostr_hub.nostr_pubkeys)

        eve = self.make_user('fake:eve', cls=Fake, enabled_protocols=['nostr'],
                             nostr_key_bytes=bytes.fromhex(uri_to_id(EVE_NSEC_URI)))
        frank = self.make_nostr('frank', FRANK_NSEC_URI, FRANK_NPUB_URI)

        nostr_hub.init(subscribe=False)
        self.assertEqual(set((PUBKEY, EVE_PUBKEY)), nostr_hub.bridged_pubkeys)
        self.assertEqual(set((BOB_PUBKEY, FRANK_PUBKEY)), nostr_hub.nostr_pubkeys)

    def test_init_subscribe_to_relays(self, _, __):
        self.assertEqual([], FakeConnection.relays)
        nostr_hub.init()
        FakeConnection.connected.acquire(timeout=10)
        self.assertEqual([Nostr.DEFAULT_TARGET], FakeConnection.relays)

        relays_a = Object(id='nostr:neventa', nostr={
            'kind': KIND_RELAYS,
            'tags': [['r', 'wss://a']],
        }).put()
        self.bob.relays = relays_a
        self.bob.put()

        FakeConnection.reset()
        nostr_hub.init()
        FakeConnection.connected.acquire(timeout=10)
        self.assertEqual(['wss://a'], FakeConnection.relays)

        eve = self.make_nostr('eve', EVE_NSEC_URI, EVE_NPUB_URI, relays=relays_a)

        FakeConnection.reset()
        nostr_hub.init()
        FakeConnection.connected.acquire(timeout=.1)  # should time out
        self.assertEqual([], FakeConnection.relays)

        relays_b = Object(id='nostr:neventb', nostr={
            'kind': KIND_RELAYS,
            'tags': [['r', 'wss://b']],
        }).put()
        frank = self.make_nostr('frank', FRANK_NSEC_URI, FRANK_NPUB_URI,
                                relays=relays_b)

        FakeConnection.reset()
        nostr_hub.init()
        FakeConnection.connected.acquire(timeout=10)
        self.assertEqual(['wss://b'], FakeConnection.relays)

    @patch('nostr_hub.RECONNECT_DELAY', timedelta(seconds=.01))
    def test_load_new_user_makes_existing_subscribers_reconnect(self, _, __):
        util.now = datetime.now

        recving = Barrier(2)
        def recv(**kwargs):
            recving.wait()
            raise TimeoutError()

        with patch.object(FakeConnection, 'recv', side_effect=recv):
            nostr_hub.init()
            recving.wait()

            bob_req = [
                'REQ', 'sub123',
                {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
                {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
            ]
            self.assertEqual([bob_req], FakeConnection.sent)
            FakeConnection.sent = []

            relays = Object(id='nostr:neventa', nostr={
                'kind': KIND_RELAYS,
                'tags': [['r', Nostr.DEFAULT_TARGET]],
            }).put()
            eve = self.make_nostr('eve', EVE_NSEC_URI, EVE_NPUB_URI, relays=relays)

            nostr_hub.init(subscribe=False)
            recving.wait()
            recving.wait()

        close = ['CLOSE', 'sub123']
        both_req = [
            'REQ', 'sub123',
            {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
            {'authors': [EVE_PUBKEY, BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
        ]
        self.assertEqual([close, both_req], FakeConnection.sent)

    def test_subscribe_connection_closed_reconnect(self, mock_create_task, _):
        event = id_and_sign({
            'pubkey': BOB_PUBKEY,
            'kind': KIND_NOTE,
            'content': 'Hello Alice!',
            'tags': [['p', PUBKEY]],
            'created_at': 678,
        }, privkey=BOB_NSEC_URI)

        self.serve_and_subscribe([event])

        FakeConnection.relays = []
        self.serve_and_subscribe([])

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
             ],
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS), 'since': 678},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS, 'since': 678},
             ],
        ], FakeConnection.sent)

        self.assertEqual(678, NostrRelay.get_by_id('wss://reelaay').since)

    def test_subscribe_stored_relay_with_since(self, _, __):
        event = id_and_sign({
            'pubkey': BOB_PUBKEY,
            'kind': KIND_NOTE,
            'content': 'Hello Alice!',
            'created_at': 678,
        }, privkey=BOB_NSEC_URI)

        relay = NostrRelay(id='wss://reelaay', since=321,
                           updated=NOW - timedelta(seconds=999))
        relay.put()
        self.serve_and_subscribe([event])

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS), 'since': 321},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS, 'since': 321},
             ],
        ], FakeConnection.sent)

        self.assertEqual(678, relay.key.get().since)

    @patch('nostr_hub.RECONNECT_DELAY', timedelta(seconds=.01))
    def test_load_no_new_users_doesnt_reconnect(self, _, __):
        util.now = datetime.now

        recving = Barrier(2)
        def recv(**kwargs):
            recving.wait()
            raise TimeoutError()

        with patch.object(FakeConnection, 'recv', side_effect=recv):
            nostr_hub.init()
            recving.wait()

            req = [
                'REQ', 'sub123',
                {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
                {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
            ]
            self.assertEqual([req], FakeConnection.sent)
            FakeConnection.sent = []

            nostr_hub.init(subscribe=False)
            recving.wait()
            recving.wait()

        self.assertEqual([], FakeConnection.sent)

    def test_subscribe_reply_to_bridged_user(self, mock_create_task, _):
        event = id_and_sign({
            'pubkey': EVE_PUBKEY,
            'kind': KIND_NOTE,
            'content': 'Hello Alice!',
            'tags': [['p', PUBKEY]],
            'created_at': NOW_TS,
        }, privkey=EVE_NSEC_URI)

        self.serve_and_subscribe([event])

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
             ]
        ], FakeConnection.sent)
        self.assert_task(mock_create_task, 'receive',
                         id=id_to_uri('note', event['id']),
                         source_protocol='nostr',
                         authed_as=EVE_NPUB_URI,
                         nostr=event)

    def test_subscribe_post_from_native_nostr_user(self, mock_create_task, _):
        # Create a post event from Bob - need to use test PUBKEY that matches NSEC_URI
        event = id_and_sign({
            'pubkey': BOB_PUBKEY,
            'kind': KIND_NOTE,
            'content': 'Hello world!',
            'created_at': NOW_TS,
        }, privkey=BOB_NSEC_URI)

        self.serve_and_subscribe([event])

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
             ]
        ], FakeConnection.sent)
        self.assert_task(mock_create_task, 'receive',
                         id=id_to_uri('note', event['id']),
                         source_protocol='nostr',
                         authed_as=BOB_NPUB_URI,
                         nostr=event)

    def test_subscribe_mention_protocol_bot(self, mock_create_task, _):
        # Create a protocol bot with a valid hex pubkey
        bot = self.make_user('fa.brid.gy', cls=Web, enabled_protocols=['nostr'],
                             nostr_key_bytes=bytes.fromhex(uri_to_id(EVE_NSEC_URI)))
        bot_pubkey = EVE_PUBKEY

        event = id_and_sign({
            'pubkey': FRANK_PUBKEY,
            'kind': KIND_NOTE,
            'content': 'Hello @fa.brid.gy!',
            'tags': [['p', bot_pubkey]],
            'created_at': NOW_TS,
        }, privkey=FRANK_NSEC_URI)

        self.serve_and_subscribe([event])

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY, bot_pubkey], 'kinds': list(Nostr.SUPPORTED_KINDS)},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
             ]
        ], FakeConnection.sent)
        self.assert_task(mock_create_task, 'receive',
                         id=id_to_uri('note', event['id']),
                         source_protocol='nostr',
                         authed_as=FRANK_NPUB_URI,
                         nostr=event)

    def test_subscribe_unrelated_event(self, mock_create_task, _):
        event = id_and_sign({
            'pubkey': EVE_PUBKEY,
            'kind': KIND_NOTE,
            'content': 'Just chatting',
            'tags': [['p', 'abc123']],
            'created_at': NOW_TS,
        }, EVE_NSEC_URI)

        self.serve_and_subscribe([event])

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
             ]
        ], FakeConnection.sent)
        mock_create_task.assert_not_called()

    def test_subscribe_invalid_events(self, mock_create_task, _):
        events = [
            id_and_sign({
                'pubkey': 'bad_not_hex',
                'kind': KIND_NOTE,
                'content': 'bad pubkey',
            }, privkey=NSEC_URI),
            {
                'pubkey': PUBKEY,
                'id': 'bad_not_hex',
                'kind': KIND_NOTE,
                'content': 'bad id',
                'sig': 'unused',
            },
            id_and_sign({
                'pubkey': PUBKEY,
                'kind': KIND_NOTE,
                'content': 'bad sig',
            }, privkey=NSEC_URI),
        ]
        events[1]['sig'] = 'bad'

        self.serve_and_subscribe(events)

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
             ]
        ], FakeConnection.sent)
        mock_create_task.assert_not_called()

    def test_subscribe_unsupported_kind(self, mock_create_task, _):
        event = id_and_sign({
            'pubkey': BOB_PUBKEY,
            'kind': 99,
            'content': 'Hello world!',
            'created_at': NOW_TS,
        }, privkey=BOB_NSEC_URI)

        self.serve_and_subscribe([event])

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
             ]
        ], FakeConnection.sent)
        mock_create_task.assert_not_called()

    def test_subscribe_delete_event(self, mock_create_task, _):
        event = id_and_sign({
            'pubkey': BOB_PUBKEY,
            'kind': KIND_DELETE,
            'content': '',
            'tags': [
                ['e', 'eventToDelete123'],
                ['p', PUBKEY],
            ],
            'created_at': NOW_TS,
        }, privkey=BOB_NSEC_URI)

        self.serve_and_subscribe([event])

        self.assertEqual([
            ['REQ', 'sub123',
             {'#p': [PUBKEY], 'kinds': list(Nostr.SUPPORTED_KINDS)},
             {'authors': [BOB_PUBKEY], 'kinds': AUTHOR_FILTER_KINDS},
             ]
        ], FakeConnection.sent)

        delayed_eta = NOW_TS + DELETE_TASK_DELAY.total_seconds()
        self.assert_task(mock_create_task, 'receive',
                         id=uri_for(event),
                         source_protocol='nostr',
                         authed_as=BOB_NPUB_URI,
                         nostr=event,
                         eta_seconds=delayed_eta)
