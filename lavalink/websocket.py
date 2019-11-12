import asyncio

import aiohttp

from .events import (TrackEndEvent, TrackExceptionEvent, TrackStuckEvent,
                     WebSocketClosedEvent)
from .stats import Stats


class WebSocket:
    """ Represents the WebSocket connection with Lavalink. """
    def __init__(self, node, host: str, port: int, password: str, resume_key: str, resume_timeout: int):
        self._node = node
        self._lavalink = self._node._manager._lavalink

        self._session = self._lavalink._session
        self._ws = None
        self._message_queue = []

        self._host = host
        self._port = port
        self._password = password
        self._resume_key = resume_key
        self._resume_timeout = resume_timeout

        self._resuming_configured = False

        self._shards = self._lavalink._shard_count
        self._user_id = self._lavalink._user_id

        self._closers = (aiohttp.WSMsgType.close,
                         aiohttp.WSMsgType.closing,
                         aiohttp.WSMsgType.closed)

        self._loop = self._lavalink._loop
        asyncio.ensure_future(self.connect())

    @property
    def connected(self):
        """ Returns whether the websocket is connected to Lavalink. """
        return self._ws is not None and not self._ws.closed

    async def connect(self):
        """ Attempts to establish a connection to Lavalink. """
        headers = {
            'Authorization': self._password,
            'Num-Shards': self._shards,
            'User-Id': str(self._user_id)
        }

        if self._resuming_configured and self._resume_key:
            headers['Resume-Key'] = self._resume_key

        attempt = 0

        while not self.connected:
            attempt += 1

            try:
                self._ws = await self._session.ws_connect('ws://{}:{}'.format(self._host, self._port), headers=headers,
                                                          heartbeat=60)
            except (aiohttp.ClientConnectorError, aiohttp.WSServerHandshakeError) as ce:
                if attempt == 1:
                    self._lavalink._logger.warning('[NODE-{}] Failed to establish connection!'.format(self._node.name))

                    if isinstance(ce, aiohttp.ClientConnectorError):
                        self._lavalink._logger.warning('[NODE-{}] This may indicate that Lavalink is not running, '
                                                       'or is running on a port different '
                                                       'to the one you passed to `add_node`.'.format(self._node.name))
                    elif isinstance(ce, aiohttp.WSServerHandshakeError):
                        if ce.status == 401:  # pylint: disable=R1705
                            # Fully aware I shouldn't disable pylint warnings but I don't like the two separate if
                            # statements otherwise.
                            self._lavalink._logger.warning('[NODE-{}] Authentication failed '
                                                           'while trying to establish a connection to the node.'
                                                           .format(self._node.name))
                            return
                            # We shouldn't try to establish any more connections as correcting this particular error
                            # would require the cog to be reloaded (or the bot to be rebooted), so further attempts
                            # would be futile, and a waste of resources.
                        elif ce.status != 101:
                            self._lavalink._logger.warning('[NODE-{}] The remote server returned code {}, '
                                                           'the expected code was 101. '
                                                           'This usually indicates that the remote server is a '
                                                           'webserver and not Lavalink. '
                                                           'Check your ports, and try again.'
                                                           .format(self._node.name, ce.status))

                backoff = min(10 * attempt, 60)
                await asyncio.sleep(backoff)
            else:
                await self._node._manager._node_connect(self._node)
                asyncio.ensure_future(self._listen())

                if not self._resuming_configured and self._resume_key \
                        and (self._resume_timeout and self._resume_timeout > 0):
                    await self._send(op='configureResuming', key=self._resume_key, timeout=self._resume_timeout)
                    self._resuming_configured = True

                if self._message_queue:
                    for message in self._message_queue:
                        await self._send(**message)

                    self._message_queue.clear()

    async def _listen(self):
        """ Listens for websocket messages. """
        async for msg in self._ws:
            self._lavalink._logger.debug('[NODE-{}] Received WebSocket message: {}'.format(self._node.name, msg.data))

            if msg.type == aiohttp.WSMsgType.text:
                await self._handle_message(msg.json())
            elif msg.type in self._closers:
                await self._websocket_closed(msg.data, msg.extra)
                return
        await self._websocket_closed()

    async def _websocket_closed(self, code: int = None, reason: str = None):
        """
        Handles when the websocket is closed.

        Parameters
        ----------
        code: :class:`int`
            The response code.
        reason: :class:`str`
            Reason why the websocket was closed. Defaults to `None`
        """
        self._ws = None
        await self._node._manager._node_disconnect(self._node, code, reason)
        await self.connect()

    async def _handle_message(self, data: dict):
        """
        Handles the response from the websocket.

        Parameters
        ----------
        data: :class:`dict`
            The data given from Lavalink.
        """
        op = data['op']

        if op == 'stats':
            self._node.stats = Stats(self._node, data)
        elif op == 'playerUpdate':
            player = self._lavalink.player_manager.get(int(data['guildId']))

            if not player:
                return

            await player._update_state(data['state'])
        elif op == 'event':
            await self._handle_event(data)
        else:
            self._lavalink._logger.warning('[NODE-{}] Received unknown op: {}'.format(self._node.name, op))

    async def _handle_event(self, data: dict):
        """
        Handles the event from Lavalink.

        Parameters
        ----------
        data: :class:`dict`
            The data given from Lavalink.
        """
        player = self._lavalink.player_manager.get(int(data['guildId']))

        if not player:
            self._lavalink._logger.warning('[NODE-{}] Received event for non-existent player! GuildId: {}'
                                           .format(self._node.name, data['guildId']))
            return

        event_type = data['type']
        event = None

        if event_type == 'TrackEndEvent':
            event = TrackEndEvent(player, player.current, data['reason'])
        elif event_type == 'TrackStuckEvent':
            event = TrackStuckEvent(player, player.current, data['thresholdMs'])
        elif event_type == 'TrackExceptionEvent':
            event = TrackExceptionEvent(player, player.current, data['error'])
        elif event_type == 'WebSocketClosedEvent':
            event = WebSocketClosedEvent(player, data['code'], data['reason'], data['byRemote'])
        else:
            self._lavalink._logger.warning('[NODE-{}] Unknown event received: {}'.format(self._node.name, event_type))
            return

        await self._lavalink._dispatch_event(event)

        if player:
            await player._handle_event(event)

    async def _send(self, **data):
        """
        Sends a payload to Lavalink.

        Parameters
        ----------
        data: :class:`dict`
            The data sent to Lavalink.
        """
        if self.connected:
            self._lavalink._logger.debug('[NODE-{}] Sending payload {}'.format(self._node.name, str(data)))
            await self._ws.send_json(data)
        else:
            self._lavalink._logger.debug('[NODE-{}] Send called before WebSocket ready!'.format(self._node.name))
            self._message_queue.append(data)
