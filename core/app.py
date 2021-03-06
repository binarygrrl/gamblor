
from Cookie import Cookie
from random import randint

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.utils.simplejson import loads, dumps
from socketio import socketio_manage
from socketio.mixins import BroadcastMixin
from socketio.namespace import BaseNamespace
from redis import Redis, ConnectionPool

from core.game import registry


redis = Redis(connection_pool=ConnectionPool())
USERS_KEY = "gamblor-users"


class GameNamespace(BaseNamespace, BroadcastMixin):
    """
    Per-user socket.io namespace for event handlers.
    """

    def on_start(self):
        """
        Set up the initial user. We only have access to the
        HTTP environment, so we use the session ID in the cookie
        and look up a user with it. If a valid user is found, we
        add them to the user set in redis, and broadcast their
        join event to everyone else.
        """
        try:
            cookie = Cookie(self.environ["HTTP_COOKIE"])
            session_key = cookie[settings.SESSION_COOKIE_NAME].value
            session = Session.objects.get(session_key=session_key)
            user_id = session.get_decoded().get(SESSION_KEY)
            user = User.objects.get(id=user_id)
        except (KeyError, ObjectDoesNotExist):
            self.user = None
        else:
            self.user = {
                "id": user.id,
                "name": user.username,
                "x": randint(780, 980),
                "y": randint(100, 300),
            }
            self.broadcast_event_not_me("join", self.user)
            redis.hset(USERS_KEY, self.user["id"], dumps(self.user))
        # Send the current set of users to the new socket.
        self.emit("users", [loads(u) for u in redis.hvals(USERS_KEY)])
        for game in registry.values():
            if game.players:
                self.emit("game_users", game.name, game.players.keys())

    def on_chat(self, message):
        if self.user:
            self.broadcast_event("chat", self.user, message)

    def on_move(self, pos):
        if self.user:
            self.user.update(pos)
            redis.hset(USERS_KEY, self.user["id"], dumps(self.user))
            self.broadcast_event_not_me("move", self.user)

    def recv_disconnect(self):
        """
        Socket disconnected - if the user was authenticated, remove
        them from redis and broadcast their leave event.
        """
        self.disconnect()
        if self.user:
            redis.hdel(USERS_KEY, self.user["id"])
            self.broadcast_event_not_me("leave", self.user)

    def on_bet(self, game_name, amount, bet_args):
        """
        Takes a bet for a game.
        """
        try:
            assert self.user is not None  # Must have a user
            assert str(amount).isdigit()  # Amount must be digit
            assert int(amount) > 0        # Amount must be positive
            assert game_name in registry  # Game must be valid
        except AssertionError:
            return
        amount = int(amount)
        user = User.objects.get(id=self.user["id"])
        user.account.balance -= amount
        if user.account.balance < 0:
            self.emit("notice", "You don't have that amount to bet")
        else:
            game = registry[game_name]
            if game.bet(self, amount, bet_args):
                user.account.save()
            self.broadcast_event("game_users", game_name, game.players.keys())


class GameApplication(object):
    """
    Standard socket.io wsgi application.
    """

    def __call__(self, environ, start_response):
        if environ["PATH_INFO"].startswith("/socket.io/"):
            socketio_manage(environ, {"": GameNamespace})
        else:
            start_response('404 Not Found', [])
            return ['<h1>Not Found</h1>']
