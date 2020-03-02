from ...events import TrackStuckEvent, TrackEndEvent, TrackExceptionEvent, \
    TrackStartEvent, QueueEndEvent
from ...models import BasicPlayer, AudioTrack

from random import randrange
import collections
import typing


class AdvancedPlayer(BasicPlayer):
    """
    The player that Lavalink.py defaults to use.

    Attributes
    ----------
    guild_id: :class:`int`
        The guild id of the player.
    node: :class:`Node`
        The node that the player is connected to.
    paused: :class:`bool`
        Whether or not a player is paused.
    position_timestamp: :class:`int`
        The position of how far a track has gone.
    volume: :class:`int`
        The volume at which the player is playing at.
    shuffle: :class:`bool`
        Whether or not to mix the queue up in a random playing order.
    repeat: :class:`bool`
        Whether or not to continuously to play a track.
    equalizer: :class:`list`
        The changes to audio frequencies on tracks.
    queue: :class:`list`
        The order of which tracks are played.
    current: :class:``AudioTrack`
        The track that is playing currently.
    """
    def __init__(self, guild_id, node):
        super().__init__(guild_id, node)

        self._user_data = {}

        self.paused = False
        self._last_update = 0
        self._last_position = 0
        self.position_timestamp = 0
        self.volume = 100
        self.shuffle = False
        self.repeat = False
        self.equalizer = [0.0 for x in range(15)]  # 0-14, -0.25 - 1.0

        self.queue = collections.deque([])
        self.current = None

    def cleanup(self):
        self.queue.clear()
        self._user_data.clear()

    def store(self, key: object, value: object):
        """
        Stores custom user data.

        Parameters
        ----------
        key: :class:`object`
            The key of the object to store.
        value: :class:`object`
            The object to associate with the key.
        """
        self._user_data.update({key: value})

    def fetch(self, key: object, default=None):
        """
        Retrieves the related value from the stored user data.

        Parameters
        ----------
        key: :class:`object`
            The key to fetch.
        default: Optional[:class:`any`]
            The object that should be returned if the key doesn't exist. Defaults to `None`.

        Returns
        -------
        :class:`any`
        """
        return self._user_data.get(key, default)

    def delete(self, key: object):
        """
        Removes an item from the the stored user data.

        Parameters
        ----------
        key: :class:`object`
            The key to delete.
        """
        try:
            del self._user_data[key]
        except KeyError:
            pass

    def add(self, requester: int, track: typing.Union[dict, AudioTrack], index: int = None):
        """
        Adds a track to the queue.

        Parameters
        ----------
        requester: :class:`int`
            The ID of the user who requested the track.
        track: :class:`dict`
            A dict representing a track returned from Lavalink.
        index: Optional[:class:`int`]
            The index at which to add the track.
            If index is left unspecified, the default behaviour is to append the track. Defaults to `None`.
        """
        at = AudioTrack(track, requester) if isinstance(track, dict) else track

        if index is None:
            self.queue.append(at)
        else:
            self.queue.insert(index, at)

    async def play(self, track: typing.Union[dict, AudioTrack] = None, start_time: int = 0,
                   end_time: int = 0, no_replace: bool = False):
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
        if track is not None and isinstance(track, dict):
            track = AudioTrack(track, 0)

        if self.repeat and self.current:
            self.queue.append(self.current)

        self._last_update = 0
        self._last_position = 0
        self.position_timestamp = 0
        self.paused = False

        if not track:
            if not self.queue:
                await self.stop()  # Also sets current to None.
                await self.node._dispatch_event(QueueEndEvent(self))
                return

            pop_at = randrange(len(self.queue)) if self.shuffle else 0
            track = self.queue.pop(pop_at)

        options = {}

        if start_time:
            if 0 > start_time > track.duration:
                raise ValueError('start_time is either less than 0 or greater than the track\'s duration')
            options['startTime'] = start_time

        if end_time:
            if 0 > end_time > track.duration:
                raise ValueError('end_time is either less than 0 or greater than the track\'s duration')
            options['endTime'] = end_time

        if no_replace:
            options['noReplace'] = no_replace

        self.current = track
        await self.node._send(op='play', guildId=self.guild_id, track=track.track, **options)
        await self.node._dispatch_event(TrackStartEvent(self, track))

    async def set_gain(self, band: int, gain: float = 0.0):
        """
        Sets the equalizer band gain to the given amount.

        Parameters
        ----------
        band: :class:`int`
            Band number (0-14).
        gain: Optional[:class:`float`]
            A float representing gain of a band (-0.25 to 1.00). Defaults to 0.0.
        """
        await self.set_gains((band, gain))

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

            if not -1 < value[0] < 15:
                raise IndexError('{} is an invalid band, must be 0-14'.format(band))

            gain = max(min(float(gain), 1.0), -0.25)
            update_package.append({'band': band, 'gain': gain})
            self.equalizer[band] = gain

        await self.node._send(op='equalizer', guildId=self.guild_id, bands=update_package)

    async def reset_equalizer(self):
        """ Resets equalizer to default values. """
        await self.set_gains(*[(x, 0.0) for x in range(15)])

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
            await self.play()
