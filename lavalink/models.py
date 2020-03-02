import typing
from abc import ABC, abstractmethod
from random import randrange
from time import time

from .events import (NodeChangedEvent, PlayerUpdateEvent,  # noqa: F401
                     QueueEndEvent, TrackEndEvent, TrackExceptionEvent,
                     TrackStartEvent, TrackStuckEvent)
from .exceptions import InvalidTrack


class AudioTrack:
    """
    Represents the AudioTrack sent to Lavalink.

    Parameters
    ----------
    data: :class:`dict`
        The data to initialise an AudioTrack from.
    requester: :class:`any`
        The requester of the track.
    extra: :class:`dict`
        Any extra information to store in this AudioTrack.

    Attributes
    ----------
    track: :class:`str`
        The base64-encoded string representing a Lavalink-readable AudioTrack.
    identifier: :class:`str`
        The track's id. For example, a youtube track's identifier will look like dQw4w9WgXcQ.
    is_seekable: :class:`bool`
        Whether the track supports seeking.
    author: :class:`str`
        The track's uploader.
    duration: :class:`int`
        The duration of the track, in milliseconds.
    stream: :class:`bool`
        Whether the track is a live-stream.
    title: :class:`str`
        The title of the track.
    uri: :class:`str`
        The full URL of track.
    extra: :class:`dict`
        Any extra properties given to this AudioTrack will be stored here.
    """
    __slots__ = ('track', 'identifier', 'is_seekable', 'author', 'duration', 'stream', 'title', 'uri', 'requester',
                 'extra')

    def __init__(self, data: dict, requester: int, **extra):
        try:
            self.track = data['track']
            self.identifier = data['info']['identifier']
            self.is_seekable = data['info']['isSeekable']
            self.author = data['info']['author']
            self.duration = data['info']['length']
            self.stream = data['info']['isStream']
            self.title = data['info']['title']
            self.uri = data['info']['uri']
            self.requester = requester
            self.extra = extra
        except KeyError as ke:
            missing_key, = ke.args
            raise InvalidTrack('Cannot build a track from partial data! (Missing key: {})'.format(missing_key))

    def __getitem__(self, name):
        return super().__getattribute__(name)

    def __repr__(self):
        return '<AudioTrack title={0.title} identifier={0.identifier}>'.format(self)


class BasePlayer(ABC):
    """
    Represents the BasePlayer all players must be inherited from.

    Attributes
    ----------
    guild_id: :class:`str`
        The guild id of the player.
    node: :class:`Node`
        The node that the player is connected to.
    """
    def __init__(self, guild_id, node):
        self.guild_id = str(guild_id)
        self.node = node
        self._original_node = None  # This is used internally for failover.
        self._voice_state = {}
        self.channel_id = None

    @abstractmethod
    async def _handle_event(self, event):
        raise NotImplementedError

    async def _update_state(self, state: dict):
        self._last_update = time() * 1000
        self._last_position = state.get('position', 0)
        self.position_timestamp = state.get('time', 0)

        event = PlayerUpdateEvent(self, self._last_position, self.position_timestamp)
        await self.node._dispatch_event(event)

    async def cleanup(self):
        pass

    async def _voice_server_update(self, data):
        self._voice_state.update({
            'event': data
        })

        await self._dispatch_voice_update()

    async def _voice_state_update(self, data):
        self._voice_state.update({
            'sessionId': data['session_id']
        })

        self.channel_id = data['channel_id']

        if not self.channel_id:  # We're disconnecting
            self._voice_state.clear()
            return

        await self._dispatch_voice_update()

    async def _dispatch_voice_update(self):
        if {'sessionId', 'event'} == self._voice_state.keys():
            await self.node._send(op='voiceUpdate', guildId=self.guild_id, **self._voice_state)

    @abstractmethod
    async def change_node(self, node):
        raise NotImplementedError


class BasicPlayer(BasePlayer):
    def __init__(self, guild_id, node):
        super().__init__(guild_id, node)

        self.volume = 100
        self.paused = False
        self.equalizer = [0.0 for x in range(15)]  # 0-14, -0.25 - 1.0
        self.current = None

    @property
    def is_playing(self):
        """ Returns the player's track state. """
        return self.is_connected and self.current is not None

    @property
    def is_connected(self):
        """ Returns whether the player is connected to a voicechannel or not. """
        return self.channel_id is not None

    @property
    def position(self):
        """ Returns the position in the track, adjusted for Lavalink's 5-second stats interval. """
        if not self.is_playing:
            return 0

        if self.paused:
            return min(self._last_position, self.current.duration)

        difference = time() * 1000 - self._last_update
        return min(self._last_position + difference, self.current.duration)

    async def _handle_event(self, event):
        """
        Handles the given event as necessary.

        Parameters
        ----------
        event: :class:`Event`
            The event that will be handled.
        """
        if isinstance(event, (TrackStuckEvent, TrackExceptionEvent)) or \
                isinstance(event, TrackEndEvent) and event.reason == 'FINISHED':
            self.current = None

    async def change_node(self, node):
        if self.node.available:
            await self.node._send(op='destroy', guildId=self.guild_id)

        old_node = self.node
        self.node = node

        if self._voice_state:
            await self._dispatch_voice_update()

        if self.current:
            await self.node._send(op='play', guildId=self.guild_id, track=self.current.track, startTime=self.position)
            self._last_update = time() * 1000

            if self.paused:
                await self.node._send(op='pause', guildId=self.guild_id, pause=self.paused)

        if self.volume != 100:
            await self.node._send(op='volume', guildId=self.guild_id, volume=self.volume)

        if any(self.equalizer):  # If any bands of the equalizer was modified
            payload = [{'band': b, 'gain': g} for b, g in enumerate(self.equalizer)]
            await self.node._send(op='equalizer', guildId=self.guild_id, bands=payload)

        await self.node._dispatch_event(NodeChangedEvent(self, old_node, node))

    async def play(self, track: AudioTrack, start_time: int = 0, end_time: int = 0, no_replace: bool = False):
        """
        Plays the given track.

        Parameters
        ----------
        track: :class:`AudioTrack`
            The track to play. If left unspecified, this will default
            to the first track in the queue. Defaults to `None` so plays the next
            song in queue.
        start_time: Optional[:class:`int`]
            Setting that determines the number of milliseconds to offset the track by.
            If left unspecified, it will start the track at its beginning. Defaults to `0`,
            which is the normal start time.
        end_time: Optional[:class:`int`]
            Settings that determines the number of milliseconds the track will stop playing.
            By default track plays until it ends as per encoded data. Defaults to `0`, which is
            the normal end time.
        no_replace: Optional[:class:`bool`]
            If set to true, operation will be ignored if a track is already playing or paused.
            Defaults to `False`
        """
        self._last_update = 0
        self._last_position = 0
        self.position_timestamp = 0
        self.paused = False

        options = {'startTime': start_time, 'endTime': end_time, 'noReplace': no_replace}

        self.current = track
        await self.node._send(op='play', guildId=self.guild_id, track=track.track, **options)
        await self.node._dispatch_event(TrackStartEvent(self, track))

    async def stop(self):
        """ Stops the player. """
        await self.node._send(op='stop', guildId=self.guild_id)
        self.current = None

    async def set_pause(self, pause: bool):
        """
        Sets the player's paused state.

        Parameters
        ----------
        pause: :class:`bool`
            Whether to pause the player or not.
        """
        await self.node._send(op='pause', guildId=self.guild_id, pause=pause)
        self.paused = pause

    async def set_volume(self, vol: int):
        """
        Sets the player's volume

        Note
        ----
        A limit of 1000 is imposed by Lavalink.

        Parameters
        ----------
        vol: :class:`int`
            The new volume level.
        """
        await self.node._send(op='volume', guildId=self.guild_id, volume=self.volume)
        self.volume = vol

    async def seek(self, position: int):
        """
        Seeks to a given position in the track.

        Parameters
        ----------
        position: :class:`int`
            The new position to seek to in milliseconds.
        """
        await self.node._send(op='seek', guildId=self.guild_id, position=position)

    async def set_gains(self, *gain_list):
        """
        Modifies the player's equalizer settings.

        Parameters
        ----------
        gain_list: :class:`any`
            A list of tuples denoting (`band`, `gain`).
        """
        update_package = []

        for value in gain_list:
            if not isinstance(value, tuple):
                raise TypeError('gain_list must be a list of tuples')

            band = value[0]
            gain = value[1]

            gain = max(min(float(gain), 1.0), -0.25)
            update_package.append({'band': band, 'gain': gain})
            self.equalizer[band] = gain

        await self.node._send(op='equalizer', guildId=self.guild_id, bands=update_package)
