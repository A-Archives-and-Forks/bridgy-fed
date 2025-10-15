"""Nostr protocol implementation.

https://github.com/nostr-protocol/nostr
https://github.com/nostr-protocol/nips/blob/master/01.md
https://github.com/nostr-protocol/nips#list

Nostr Object key ids are NIP-21 nostr:... URIs.
https://nips.nostr.com/21
"""
import logging

from google.cloud import ndb
from google.cloud.ndb.query import OR
from granary import as1
import granary.nostr
from granary.nostr import (
    bech32_prefix_for,
    id_to_uri,
    KIND_PROFILE,
    KIND_RELAYS,
    nip05_to_npub,
    uri_to_id,
)
from oauth_dropins.webutil import flask_util
from oauth_dropins.webutil import util
from oauth_dropins.webutil.flask_util import get_required_param
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import add, json_dumps, json_loads
from requests import RequestException
import secp256k1
from websockets.exceptions import ConnectionClosedOK
from websockets.sync.client import connect
from werkzeug.exceptions import NotFound

import common
from common import (
    DOMAIN_RE,
    DOMAINS,
    error,
    USER_AGENT,
)
from flask_app import app
import ids
from models import Object, PROTOCOLS, Target, User
from protocol import Protocol
import web

logger = logging.getLogger(__name__)


class Nostr(User, Protocol):
    """Nostr class.

    Key id is NIP-21 nostr:npub... bech32 npub URI.
    https://github.com/nostr-protocol/nips/blob/master/19.md
    """
    ABBREV = 'nostr'
    PHRASE = 'Nostr'
    LOGO_EMOJI = '𓅦'  # ostrich-ish bird
    LOGO_HTML = '<img src="/static/nostr_logo.png">'
    CONTENT_TYPE = 'application/json'
    HAS_COPIES = True
    DEFAULT_TARGET = 'wss://nos.lol'
    REQUIRES_AVATAR = True
    REQUIRES_NAME = True
    DEFAULT_ENABLED_PROTOCOLS = ('web',)
    SUPPORTED_AS1_TYPES = frozenset(
        tuple(as1.ACTOR_TYPES)
        + tuple(as1.POST_TYPES)
        # note that update is supported for actors and articles, but not notes
        # https://github.com/nostr-protocol/nips/issues/646
        # we override check_supported() below to check for this
        + tuple(as1.CRUD_VERBS)
        + ('follow', 'like', 'share', 'stop-following')
    )
    SUPPORTS_DMS = False  # NIP-17
    HTML_PROFILES = False

    relays = ndb.KeyProperty(kind='Object')
    """NIP-65 kind 10002 event with this user's relays."""
    valid_nip05 = ndb.StringProperty()
    """NIP-05 identifier that we've resolved and verified."""

    def _pre_put_hook(self):
        """Validates that the id is a bech32-encoded nostr:npub id."""
        assert self.key.id().startswith('nostr:npub'), self.key.id()
        return super()._pre_put_hook()

    def hex_pubkey(self):
        """Returns the user's hex-encoded Nostr public secp256k1 key.

        Returns:
          str:
        """
        return uri_to_id(self.key.id())

    def npub(self):
        """Returns the user's bech32-encoded ActivityPub public secp256k1 key.

        Returns:
          str:
        """
        return self.key.id().removeprefix('nostr:')

    @ndb.ComputedProperty
    def handle(self):
        """Returns the NIP-05 identity from the user's profile event."""
        if nip05 := self.nip_05():
            return nip05.removeprefix('_@')
        elif self.key:
            return self.key.id().removeprefix('nostr:')

    @ndb.ComputedProperty
    def status(self):
        if not self.obj or not self.obj.as1:
            return 'no-profile'

        if not self.valid_nip05 or self.valid_nip05 != self.nip_05():
            return 'no-nip05'

        return super().status

    def nip_05(self):
        if self.obj and self.obj.nostr and self.obj.nostr.get('kind') == KIND_PROFILE:
            content = json_loads(self.obj.nostr.get('content', '{}'))
            if nip05 := content.get('nip05'):
                return nip05

    def web_url(self):
        if self.obj_key:
            return granary.nostr.Nostr.user_url(
                self.obj_key.id().removeprefix("nostr:"))

    @classmethod
    def owns_id(cls, id):
        return id.startswith('nostr:') or bool(granary.nostr.is_bech32(id))

    @classmethod
    def owns_handle(cls, handle, allow_internal=False):
        if not handle:
            return False

        # TODO: implement allow_internal?
        if (handle.startswith('npub')
                or cls.is_user_at_domain(handle, allow_internal=True)):
            return True

        if web.is_valid_domain(handle):
            return None  # could be a _@ NIP-05

        return False

    @classmethod
    def handle_to_id(cls, handle):
        if cls.owns_handle(handle) is False:
            return None
        elif handle.startswith('npub'):
            return handle

        return granary.nostr.nip05_to_npub(handle)

    @classmethod
    def bridged_web_url_for(cls, user, fallback=False):
        if not isinstance(user, cls) and user.obj:
            if nprofile := user.obj.get_copy(cls):
                return granary.nostr.Nostr.user_url(nprofile)

    @classmethod
    def target_for(cls, obj, shared=False):
        """Returns the first NIP-65 relay for the given object's author."""
        if obj and (id := as1.get_owner(obj.as1)) and id.startswith('nostr:npub'):
            if user := Nostr.get_or_create(id, allow_opt_out=True):
                if user.relays and (relays := user.relays.get()):
                    if relays.nostr:
                        for tag in relays.nostr.get('tags', []):
                            if tag[0] == 'r' and (len(tag) == 2 or tag[2] == 'write'):
                                return tag[1]

    @classmethod
    def check_supported(cls, obj, direction):
        """Update is only supported for actors and articles, not notes."""
        super().check_supported(obj, direction)

        if direction == 'send':
            if obj.type == 'update':
                if inner_type := as1.object_type(as1.get_object(obj.as1)):
                    if inner_type not in list(as1.ACTOR_TYPES) + ['article']:
                        error(f"Bridgy Fed for {cls.LABEL} doesn't support {obj.type} {inner_type} yet", status=204)

    @classmethod
    def create_for(cls, user):
        """Creates a Nostr profile for a non-Nostr user.

        Args:
          user (models.User)
        """
        assert not isinstance(user, cls)

        if npub := user.get_copy(cls):
            return

        logger.info(f'adding Nostr copy user {user.npub()} for {user.key}')
        user.add('copies', Target(uri='nostr:' + user.npub(), protocol='nostr'))
        user.put()

        # create Nostr profile (kind 0 event) if necessary
        if user.obj and user.obj.get_copy(Nostr):
            return

        if not user.obj.as1:
            user.reload_profile()
        cls.send(user.obj, cls.DEFAULT_TARGET, from_user=user)

    def reload_profile(self, **kwargs):
        """Reloads this user's kind 0 profile, NIP-65 relay list, and NIP-05 id.

        https://nips.nostr.com/1#kinds
        https://nips.nostr.com/65
        https://nips.nostr.com/5
        """
        client = granary.nostr.Nostr()
        relay = self.target_for(self.obj) or self.DEFAULT_TARGET
        logger.debug(f'connecting to {relay}')
        with connect(relay, open_timeout=util.HTTP_TIMEOUT,
                     close_timeout=util.HTTP_TIMEOUT) as websocket:
            events = client.query(websocket, {
                'authors': [self.hex_pubkey()],
                'kinds': [KIND_PROFILE, KIND_RELAYS],
            })

        profile = relays = None
        for event in events:
            kind = event.get('kind')
            obj = Object(id=id_to_uri('nevent', event['id']), nostr=event,
                         source_protocol='nostr')

            if kind == KIND_PROFILE and not profile:
                profile = obj
                self.obj_key = profile.put()
            elif kind == KIND_RELAYS and not relays:
                relays = obj
                self.relays = relays.put()

            if profile and relays:
                break

        # check NIP-05
        self.valid_nip05 = None
        if nip05 := self.nip_05():
            try:
                if nip05_to_npub(nip05) == self.npub():
                    self.valid_nip05 = nip05
            except BaseException as e:
                code, _ = util.interpret_http_exception(e)
                if not code:
                    logger.info(e)

        self.put()

    @classmethod
    def set_username(to_cls, user, username):
        """check NIP-05 DNS, then update profile event with nip05?"""
        if not user.is_enabled(to_cls):
            raise ValueError("First, you'll need to bridge your account into Nostr by following this account.")

        npub = user.get_copy(to_cls)
        username = username.removeprefix('@')

        # TODO
        logger.info(f'Setting Nostr NIP-05 for {user.key.id()} to {username}')
        raise NotImplementedError()

    @classmethod
    def fetch(cls, obj, **kwargs):
        """Fetches a Nostr event from a relay.

        Args:
          obj (models.Object): with the id to fetch. Fills data into the ``nostr``
            property.
          kwargs: ignored

        Returns:
          bool: True if the object was fetched and populated successfully,
            False otherwise
        """
        uri = obj.key.id()
        if not cls.owns_id(uri):
            logger.info(f"Nostr can't fetch {uri}")
            return False

        bech32_id = uri.removeprefix('nostr:')
        is_profile = bech32_id.startswith('npub') or bech32_id.startswith('nprofile')
        hex_id = uri_to_id(uri)
        filter = ({'authors': [hex_id], 'kinds': [KIND_PROFILE]} if is_profile
                  else {'ids': [hex_id]})

        client = granary.nostr.Nostr()
        relay = cls.target_for(obj)
        logger.debug(f'connecting to {relay}')
        with connect(relay, open_timeout=util.HTTP_TIMEOUT,
                     close_timeout=util.HTTP_TIMEOUT) as websocket:
            events = client.query(websocket, filter)

        if not events:
            return False

        obj.nostr = events[0]
        return True

    @classmethod
    def _convert(to_cls, obj, from_user=None):
        """Converts a :class:`models.Object` to a Nostr event.

        Args:
          obj (models.Object)
          from_user (models.User): user this object is from

        Returns:
          dict: JSON Nostr event
        """
        obj_as1 = obj.as1
        translated = to_cls.translate_ids(obj_as1)

        # find first relay (target) for referenced user (follow of, in reply to,
        # repost of)
        if as1.object_type(obj_as1) in as1.CRUD_VERBS:
            obj_as1 = as1.get_object(obj_as1)

        remote_relay = ''
        if remote_obj := granary.nostr.Nostr().base_object(obj_as1):
            if id := remote_obj.get('id'):
                if id.startswith('nostr:npub'):
                    obj = Object(our_as1={'objectType': 'person', 'id': id})
                else:
                    obj = Nostr.load(id)
                remote_relay = to_cls.target_for(obj)

        # convert!
        privkey = from_user.nsec() if from_user else None
        return granary.nostr.from_as1(translated, privkey=privkey,
                                      remote_relay=remote_relay)

    @classmethod
    def send(to_cls, obj, relay_url, from_user=None, **kwargs):
        """Sends an event to a relay.

        Events are immutable, so all operations happen by sending a new event,
        including updates and deletes. :meth:`granary.nostr.from_as1` translates all
        of those, so all we have to do here is convert and send the event.
        """
        # TODO: update
        # TODO: delete
        assert from_user

        event = to_cls.convert(obj, from_user=from_user)
        assert event.get('pubkey') == from_user.hex_pubkey(), event
        assert event.get('sig'), event

        logger.debug(f'connecting to {relay_url}')
        with connect(relay_url, open_timeout=util.HTTP_TIMEOUT,
                     close_timeout=util.HTTP_TIMEOUT) as websocket:
            try:
                msg = ['EVENT', event]
                logger.debug(f'{websocket.remote_address} <= {event}')
                websocket.send(json_dumps(msg))
                resp = websocket.recv(timeout=util.HTTP_TIMEOUT)
                logger.debug(f'{websocket.remote_address} => {resp}')
            except ConnectionClosedOK as cc:
                logger.warning(cc)
                return False

        obj.copies = [copy for copy in obj.copies if copy.protocol != 'nostr']
        uri = id_to_uri(bech32_prefix_for(event), event['id'])
        obj.add('copies', Target(uri=uri, protocol=to_cls.LABEL))
        obj.put()

        return True


@app.get('/.well-known/nostr.json')
@flask_util.headers(common.CACHE_CONTROL)
def nip_05():
    """NIP-05 endpoint that serves handles for users bridged into Nostr.

    https://nips.nostr.com/5

    Query params:
      name (str): should only contain a-z0-9-_.

    Returns a JSON object with:
      names: {<name>: <pubkey hex>}
      relays: optional, {<pubkey hex>: [relay urls]}
    """
    name = get_required_param('name')

    if (proto := Protocol.for_request()) and proto != Nostr:
        user = proto.query(OR(proto.handle == name,
                              proto.handle_as_domain == name,
                              proto.key == ndb.Key(proto, name),
                              )).get()
        if user and user.is_enabled(Nostr):
            if npub := user.get_copy(Nostr):
                return {
                    'names': {name: uri_to_id(npub)},
                }

    raise NotFound()
