"""Support for interacting with Spotify Connect."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any, Concatenate, override

from spotifyaio import (
    Episode,
    Item,
    ItemType,
    PlaybackState,
    RepeatMode as SpotifyRepeatMode,
    Track,
)
from spotifyaio.models import ProductType
from yarl import URL

from homeassistant.components.media_player import (
    ATTR_MEDIA_ENQUEUE,
    BrowseMedia,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .browse_media import async_browse_media_internal, async_search_media_internal
from .const import (
    MEDIA_PLAYER_PREFIX,
    MEDIA_TYPE_USER_SAVED_TRACKS,
    PLAYABLE_MEDIA_TYPES,
)
from .coordinator import (
    SpotifyConfigEntry,
    SpotifyCoordinator,
    SpotifyDeviceCoordinator,
)
from .entity import SpotifyEntity

_LOGGER = logging.getLogger(__name__)

SUPPORT_SPOTIFY = (
    MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.SEARCH_MEDIA  # Correctif Domo DZ : recherche
)

# Correctif Domo DZ : quand rien ne joue, garder la navigation, la recherche
# et le lancement d'un média (le core ne laissait que le choix de l'appareil).
SUPPORT_SPOTIFY_IDLE = (
    MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.SEARCH_MEDIA
    | MediaPlayerEntityFeature.PLAY_MEDIA
)

REPEAT_MODE_MAPPING_TO_HA = {
    SpotifyRepeatMode.CONTEXT: RepeatMode.ALL,
    SpotifyRepeatMode.OFF: RepeatMode.OFF,
    SpotifyRepeatMode.TRACK: RepeatMode.ONE,
}

REPEAT_MODE_MAPPING_TO_SPOTIFY = {
    value: key for key, value in REPEAT_MODE_MAPPING_TO_HA.items()
}
AFTER_REQUEST_SLEEP = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpotifyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Spotify based on a config entry."""
    data = entry.runtime_data
    assert entry.unique_id is not None
    spotify = SpotifyMediaPlayer(
        data.coordinator,
        data.devices,
    )
    async_add_entities([spotify])


def ensure_item[_R](
    func: Callable[[SpotifyMediaPlayer, Item], _R],
) -> Callable[[SpotifyMediaPlayer], _R | None]:
    """Ensure that the currently playing item is available."""

    def wrapper(self: SpotifyMediaPlayer) -> _R | None:
        if not self.currently_playing or not self.currently_playing.item:
            return None
        return func(self, self.currently_playing.item)

    return wrapper


def async_refresh_after[_T: SpotifyEntity, **_P](
    func: Callable[Concatenate[_T, _P], Awaitable[None]],
) -> Callable[Concatenate[_T, _P], Coroutine[Any, Any, None]]:
    """Define a wrapper to yield and refresh after."""

    async def _async_wrap(self: _T, *args: _P.args, **kwargs: _P.kwargs) -> None:
        await func(self, *args, **kwargs)
        await asyncio.sleep(AFTER_REQUEST_SLEEP)
        await self.coordinator.async_refresh()

    return _async_wrap


class SpotifyMediaPlayer(SpotifyEntity, MediaPlayerEntity):
    """Representation of a Spotify controller."""

    _attr_media_image_remotely_accessible = False
    _attr_name = None
    _attr_translation_key = "spotify"

    def __init__(
        self,
        coordinator: SpotifyCoordinator,
        device_coordinator: SpotifyDeviceCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.devices = device_coordinator
        self._attr_unique_id = coordinator.current_user.user_id
        # Correctif Domo DZ : dernier appareil vu en lecture, pour y relancer
        # la musique quand on choisit un média alors que rien ne joue.
        self._last_device_id: str | None = None
        self._remember_device()

    def _remember_device(self) -> None:
        """Retenir l'appareil de la lecture en cours (correctif Domo DZ)."""
        data = self.coordinator.data
        playback = data.current_playback if data else None
        # getattr : ne jamais faire échouer la création de l'entité pour ça.
        if device_id := getattr(getattr(playback, "device", None), "device_id", None):
            self._last_device_id = device_id

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._remember_device()
        super()._handle_coordinator_update()

    async def _async_idle_device_id(self) -> str | None:
        """Choisir l'appareil où lancer un média quand rien ne joue.

        Correctif Domo DZ : le core prenait devices.data[0], le premier appareil
        d'une liste que Spotify renvoie dans un ordre quelconque et rafraîchie
        toutes les 5 minutes ; la musique pouvait partir sur le mauvais appareil,
        voire quitter l'Echo qui jouait (lecture illisible vue comme arrêtée).
        Ordre retenu : l'appareil actif, puis le dernier appareil utilisé, puis
        le premier par ordre alphabétique ; jamais un appareil restreint.
        """
        await self.devices.async_refresh()
        devices = self.devices.data or []
        if active := next((d for d in devices if d.is_active), None):
            if active.is_restricted:
                raise HomeAssistantError(
                    f"L'appareil Spotify actif « {active.name} » n'accepte pas les"
                    " commandes à distance : choisissez un autre appareil dans la"
                    " liste des sources, puis réessayez."
                )
            # device_id absent : Spotify garde l'appareil actif.
            return active.device_id
        usable = sorted(
            (d for d in devices if d.device_id and not d.is_restricted),
            key=lambda d: d.name.casefold(),
        )
        if not usable:
            raise HomeAssistantError(
                "Aucun appareil Spotify disponible : ouvrez l'application Spotify"
                " sur l'appareil voulu, puis réessayez."
            )
        for device in usable:
            if device.device_id == self._last_device_id:
                return device.device_id
        return usable[0].device_id

    @property
    def currently_playing(self) -> PlaybackState | None:
        """Return the current playback."""
        return self.coordinator.data.current_playback

    @property
    @override
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Return the supported features."""
        if self.coordinator.current_user.product != ProductType.PREMIUM:
            return MediaPlayerEntityFeature(0)
        if not self.currently_playing:
            return SUPPORT_SPOTIFY_IDLE
        if self.currently_playing.device.is_restricted:
            return MediaPlayerEntityFeature.SELECT_SOURCE
        return SUPPORT_SPOTIFY

    @property
    @override
    def state(self) -> MediaPlayerState:
        """Return the playback state."""
        if not self.currently_playing:
            return MediaPlayerState.IDLE
        if self.currently_playing.is_playing:
            return MediaPlayerState.PLAYING
        return MediaPlayerState.PAUSED

    @property
    @override
    def volume_level(self) -> float | None:
        """Return the device volume."""
        if not self.currently_playing:
            return None
        return self.currently_playing.device.volume_percent / 100

    @property
    @ensure_item
    @override
    def media_content_id(self, item: Item) -> str:  # noqa: PLR0206
        """Return the media URL."""
        return item.uri

    @property
    @ensure_item
    @override
    def media_content_type(self, item: Item) -> str:  # noqa: PLR0206
        """Return the media type."""
        return MediaType.PODCAST if item.type == ItemType.EPISODE else MediaType.MUSIC

    @property
    @ensure_item
    @override
    def media_duration(self, item: Item) -> int:  # noqa: PLR0206
        """Duration of current playing media in seconds."""
        return round(item.duration_ms / 1000)

    @property
    @override
    def media_position(self) -> int | None:
        """Position of current playing media in seconds."""
        if not self.currently_playing or self.currently_playing.progress_ms is None:
            return None
        return round(self.currently_playing.progress_ms / 1000)

    @property
    @override
    def media_position_updated_at(self) -> dt.datetime | None:
        """When was the position of the current playing media valid."""
        if not self.currently_playing:
            return None
        return self.coordinator.data.position_updated_at

    @property
    @ensure_item
    @override
    def media_image_url(self, item: Item) -> str | None:  # noqa: PLR0206
        """Return the media image URL."""
        if item.type == ItemType.EPISODE:
            if TYPE_CHECKING:
                assert isinstance(item, Episode)
            if item.images:
                return item.images[0].url
            if item.show and item.show.images:
                return item.show.images[0].url
            return None
        if TYPE_CHECKING:
            assert isinstance(item, Track)
        if not item.album.images:
            return None
        return item.album.images[0].url

    @property
    @ensure_item
    @override
    def media_title(self, item: Item) -> str:  # noqa: PLR0206
        """Return the media title."""
        return item.name

    @property
    @ensure_item
    @override
    def media_artist(self, item: Item) -> str:  # noqa: PLR0206
        """Return the media artist."""
        if item.type == ItemType.EPISODE:
            if TYPE_CHECKING:
                assert isinstance(item, Episode)
            return item.show.name

        if TYPE_CHECKING:
            assert isinstance(item, Track)
        return ", ".join(artist.name for artist in item.artists)

    @property
    @ensure_item
    @override
    def media_album_name(self, item: Item) -> str | None:  # noqa: PLR0206
        """Return the media album."""
        if item.type == ItemType.EPISODE:
            return None

        if TYPE_CHECKING:
            assert isinstance(item, Track)
        return item.album.name

    @property
    @ensure_item
    @override
    def media_track(self, item: Item) -> int | None:  # noqa: PLR0206
        """Track number of current playing media, music track only."""
        if item.type == ItemType.EPISODE:
            return None
        if TYPE_CHECKING:
            assert isinstance(item, Track)
        return item.track_number

    @property
    @override
    def media_playlist(self) -> str | None:
        """Title of Playlist currently playing."""
        if self.coordinator.data.dj_playlist:
            return "DJ"
        if self.coordinator.data.playlist is None:
            return None
        return self.coordinator.data.playlist.name

    @property
    @override
    def source(self) -> str | None:
        """Return the current playback device."""
        if not self.currently_playing:
            return None
        return self.currently_playing.device.name

    @property
    @override
    def source_list(self) -> list[str] | None:
        """Return a list of source devices."""
        # Spotify returns the devices in no particular order. Sort them so an
        # unchanged set of devices does not read as a capability change.
        return sorted(device.name for device in self.devices.data)

    @property
    @override
    def shuffle(self) -> bool | None:
        """Shuffling state."""
        if not self.currently_playing:
            return None
        return self.currently_playing.shuffle

    @property
    @override
    def repeat(self) -> RepeatMode | None:
        """Return current repeat mode."""
        if not self.currently_playing:
            return None
        return REPEAT_MODE_MAPPING_TO_HA.get(self.currently_playing.repeat_mode)

    @async_refresh_after
    @override
    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume level."""
        await self.coordinator.client.set_volume(int(volume * 100))

    @async_refresh_after
    @override
    async def async_media_play(self) -> None:
        """Start or resume playback."""
        await self.coordinator.client.start_playback()

    @async_refresh_after
    @override
    async def async_media_pause(self) -> None:
        """Pause playback."""
        await self.coordinator.client.pause_playback()

    @async_refresh_after
    @override
    async def async_media_previous_track(self) -> None:
        """Skip to previous track."""
        await self.coordinator.client.previous_track()

    @async_refresh_after
    @override
    async def async_media_next_track(self) -> None:
        """Skip to next track."""
        await self.coordinator.client.next_track()

    @async_refresh_after
    @override
    async def async_media_seek(self, position: float) -> None:
        """Send seek command."""
        await self.coordinator.client.seek_track(int(position * 1000))

    @async_refresh_after
    @override
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media."""
        media_type = media_type.removeprefix(MEDIA_PLAYER_PREFIX)

        enqueue: MediaPlayerEnqueue = kwargs.get(
            ATTR_MEDIA_ENQUEUE, MediaPlayerEnqueue.REPLACE
        )

        kwargs = {}

        # Spotify can't handle URI's with query strings or anchors
        # Yet, they do generate those types of URI in their official clients.
        media_id = str(URL(media_id).with_query(None).with_fragment(None))

        if media_type in {MediaType.TRACK, MediaType.EPISODE, MediaType.MUSIC}:
            kwargs["uris"] = [media_id]
        elif media_type in PLAYABLE_MEDIA_TYPES:
            context_uri = media_id

            if media_type == MEDIA_TYPE_USER_SAVED_TRACKS:
                user_data = await self.coordinator.client.get_current_user()
                context_uri = f"spotify:user:{user_data.user_id}:collection"

            kwargs["context_uri"] = context_uri
        else:
            _LOGGER.error("Media type %s is not supported", media_type)
            return

        if not self.currently_playing and (
            device_id := await self._async_idle_device_id()
        ):
            kwargs["device_id"] = device_id

        if enqueue == MediaPlayerEnqueue.ADD:
            if media_type not in {
                MediaType.TRACK,
                MediaType.EPISODE,
                MediaType.MUSIC,
            }:
                raise ValueError(
                    f"Media type {media_type} is not supported when enqueue is ADD"
                )
            await self.coordinator.client.add_to_queue(
                media_id, kwargs.get("device_id")
            )
            return

        await self.coordinator.client.start_playback(**kwargs)

    @async_refresh_after
    @override
    async def async_select_source(self, source: str) -> None:
        """Select playback device."""
        for device in self.devices.data:
            if device.name == source:
                if TYPE_CHECKING:
                    assert device.device_id is not None
                await self.coordinator.client.transfer_playback(device.device_id)
                return

    @async_refresh_after
    @override
    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Enable/Disable shuffle mode."""
        await self.coordinator.client.set_shuffle(state=shuffle)

    @async_refresh_after
    @override
    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Set repeat mode."""
        if repeat not in REPEAT_MODE_MAPPING_TO_SPOTIFY:
            raise ValueError(f"Unsupported repeat mode: {repeat}")
        await self.coordinator.client.set_repeat(REPEAT_MODE_MAPPING_TO_SPOTIFY[repeat])

    @override
    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper."""

        return await async_browse_media_internal(
            self.hass,
            self.coordinator.client,
            media_content_type,
            media_content_id,
        )

    @override
    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        """Rechercher sur Spotify (correctif Domo DZ)."""
        return await async_search_media_internal(self.coordinator.client, query)

    @callback
    def _handle_devices_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not self.enabled:
            return
        self.async_write_ha_state()

    @override
    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.devices.async_add_listener(self._handle_devices_update)
        )
