"""Support for Spotify media browsing."""

from enum import StrEnum
import logging
from typing import TYPE_CHECKING, Any, TypedDict

import aiohttp
from mashumaro.exceptions import InvalidFieldValue, MissingField
import orjson
from spotifyaio import (
    Artist,
    BasePlaylist,
    SimplifiedAlbum,
    SimplifiedTrack,
    SpotifyClient,
    SpotifyConnectionError,
    SpotifyForbiddenError,
    SpotifyNotFoundError,
    Track,
)
from spotifyaio.models import (
    Episode,
    ItemType,
    SearchType,
    SimplifiedArtist,
    SimplifiedEpisode,
)
import yarl

from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaType,
    SearchError,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    MEDIA_PLAYER_PREFIX,
    MEDIA_TYPE_SHOW,
    MEDIA_TYPE_USER_SAVED_TRACKS,
    PLAYABLE_MEDIA_TYPES,
)
from .coordinator import _describe_error
from .util import fetch_image_url

BROWSE_LIMIT = 48


_LOGGER = logging.getLogger(__name__)


class ItemPayload(TypedDict):
    """TypedDict for item payload."""

    name: str
    type: str
    uri: str
    id: str | None
    thumbnail: str | None


def _get_artist_item_payload(artist: Artist) -> ItemPayload:
    return {
        "id": artist.artist_id,
        "name": artist.name,
        "type": MediaType.ARTIST,
        "uri": artist.uri,
        "thumbnail": fetch_image_url(artist.images),
    }


def _get_album_item_payload(album: SimplifiedAlbum) -> ItemPayload:
    return {
        "id": album.album_id,
        "name": album.name,
        "type": MediaType.ALBUM,
        "uri": album.uri,
        "thumbnail": fetch_image_url(album.images),
    }


def _get_playlist_item_payload(playlist: BasePlaylist) -> ItemPayload:
    return {
        "id": playlist.playlist_id,
        "name": playlist.name,
        "type": MediaType.PLAYLIST,
        "uri": playlist.uri,
        "thumbnail": fetch_image_url(playlist.images),
    }


def _get_track_item_payload(
    track: SimplifiedTrack, show_thumbnails: bool = True
) -> ItemPayload:
    return {
        "id": track.track_id,
        "name": track.name,
        "type": MediaType.TRACK,
        "uri": track.uri,
        "thumbnail": (
            fetch_image_url(track.album.images)
            if show_thumbnails and isinstance(track, Track)
            else None
        ),
    }


def _get_episode_item_payload(episode: SimplifiedEpisode) -> ItemPayload:
    return {
        "id": episode.episode_id,
        "name": episode.name,
        "type": MediaType.EPISODE,
        "uri": episode.uri,
        "thumbnail": fetch_image_url(episode.images),
    }


class BrowsableMedia(StrEnum):
    """Enum of browsable media."""

    CURRENT_USER_PLAYLISTS = "current_user_playlists"
    CURRENT_USER_FOLLOWED_ARTISTS = "current_user_followed_artists"
    CURRENT_USER_SAVED_ALBUMS = "current_user_saved_albums"
    CURRENT_USER_SAVED_TRACKS = MEDIA_TYPE_USER_SAVED_TRACKS
    CURRENT_USER_SAVED_SHOWS = "current_user_saved_shows"
    CURRENT_USER_RECENTLY_PLAYED = "current_user_recently_played"
    CURRENT_USER_TOP_ARTISTS = "current_user_top_artists"
    CURRENT_USER_TOP_TRACKS = "current_user_top_tracks"
    # Correctif Domo DZ : page « Rechercher » (la barre de recherche du
    # navigateur n'apparaît pas sur la page racine de la bibliothèque).
    SEARCH = "domo_search"


LIBRARY_MAP = {
    BrowsableMedia.SEARCH.value: "Rechercher",
    BrowsableMedia.CURRENT_USER_PLAYLISTS.value: "Playlists",
    BrowsableMedia.CURRENT_USER_FOLLOWED_ARTISTS.value: "Artists",
    BrowsableMedia.CURRENT_USER_SAVED_ALBUMS.value: "Albums",
    BrowsableMedia.CURRENT_USER_SAVED_TRACKS.value: "Liked songs",
    BrowsableMedia.CURRENT_USER_SAVED_SHOWS.value: "Podcasts",
    BrowsableMedia.CURRENT_USER_RECENTLY_PLAYED.value: "Recently played",
    BrowsableMedia.CURRENT_USER_TOP_ARTISTS.value: "Top Artists",
    BrowsableMedia.CURRENT_USER_TOP_TRACKS.value: "Top Tracks",
}

CONTENT_TYPE_MEDIA_CLASS: dict[str, Any] = {
    BrowsableMedia.SEARCH.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.TRACK,
    },
    BrowsableMedia.CURRENT_USER_PLAYLISTS.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.PLAYLIST,
    },
    BrowsableMedia.CURRENT_USER_FOLLOWED_ARTISTS.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.ARTIST,
    },
    BrowsableMedia.CURRENT_USER_SAVED_ALBUMS.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.ALBUM,
    },
    BrowsableMedia.CURRENT_USER_SAVED_TRACKS.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.TRACK,
    },
    BrowsableMedia.CURRENT_USER_SAVED_SHOWS.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.PODCAST,
    },
    BrowsableMedia.CURRENT_USER_RECENTLY_PLAYED.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.TRACK,
    },
    BrowsableMedia.CURRENT_USER_TOP_ARTISTS.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.ARTIST,
    },
    BrowsableMedia.CURRENT_USER_TOP_TRACKS.value: {
        "parent": MediaClass.DIRECTORY,
        "children": MediaClass.TRACK,
    },
    MediaType.PLAYLIST: {
        "parent": MediaClass.PLAYLIST,
        "children": MediaClass.TRACK,
    },
    MediaType.ALBUM: {"parent": MediaClass.ALBUM, "children": MediaClass.TRACK},
    MediaType.ARTIST: {"parent": MediaClass.ARTIST, "children": MediaClass.ALBUM},
    MediaType.EPISODE: {"parent": MediaClass.EPISODE, "children": None},
    MEDIA_TYPE_SHOW: {"parent": MediaClass.PODCAST, "children": MediaClass.EPISODE},
    MediaType.TRACK: {"parent": MediaClass.TRACK, "children": None},
}


class MissingMediaInformation(BrowseError):
    """Missing media required information."""


class UnknownMediaType(BrowseError):
    """Unknown media type."""


async def async_browse_media(
    hass: HomeAssistant,
    media_content_type: str | None,
    media_content_id: str | None,
    *,
    can_play_artist: bool = True,
) -> BrowseMedia:
    """Browse Spotify media."""
    parsed_url = None
    info = None

    # Check if caller is requesting the root nodes
    if media_content_type is None and media_content_id is None:
        config_entries = hass.config_entries.async_entries(
            DOMAIN, include_disabled=False, include_ignore=False
        )
        children = [
            BrowseMedia(
                title=config_entry.title,
                media_class=MediaClass.APP,
                media_content_id=f"{MEDIA_PLAYER_PREFIX}{config_entry.entry_id}",
                media_content_type=f"{MEDIA_PLAYER_PREFIX}library",
                thumbnail="/api/brands/integration/spotify/logo.png",
                can_play=False,
                can_expand=True,
            )
            for config_entry in config_entries
        ]
        return BrowseMedia(
            title="Spotify",
            media_class=MediaClass.APP,
            media_content_id=MEDIA_PLAYER_PREFIX,
            media_content_type="spotify",
            thumbnail="/api/brands/integration/spotify/logo.png",
            can_play=False,
            can_expand=True,
            children=children,
        )

    if media_content_id is None or not media_content_id.startswith(MEDIA_PLAYER_PREFIX):
        raise BrowseError("Invalid Spotify URL specified")

    # The config entry id is the host name of the URL, the Spotify URI is the name
    parsed_url = yarl.URL(media_content_id)
    config_entry_id = parsed_url.host

    if (
        config_entry_id is None
        # config entry ids can be upper or lower case. Yarl always returns host
        # names in lower case, so we need to look for the config entry in both
        or (
            entry := hass.config_entries.async_get_entry(config_entry_id)
            or hass.config_entries.async_get_entry(config_entry_id.upper())
        )
        is None
        or entry.state is not ConfigEntryState.LOADED
    ):
        raise BrowseError("Invalid Spotify account specified")
    media_content_id = parsed_url.name
    info = entry.runtime_data

    result = await async_browse_media_internal(
        hass,
        info.coordinator.client,
        media_content_type,
        media_content_id,
        can_play_artist=can_play_artist,
    )

    # Build new URLs with config entry specifiers
    result.media_content_id = str(parsed_url.with_name(result.media_content_id))
    if result.children:
        for child in result.children:
            child.media_content_id = str(parsed_url.with_name(child.media_content_id))
    return result


async def async_browse_media_internal(
    hass: HomeAssistant,
    spotify: SpotifyClient,
    media_content_type: str | None,
    media_content_id: str | None,
    *,
    can_play_artist: bool = True,
) -> BrowseMedia:
    """Browse spotify media."""
    if media_content_type in (None, f"{MEDIA_PLAYER_PREFIX}library"):
        return await library_payload(can_play_artist=can_play_artist)

    # Strip prefix
    if media_content_type:
        media_content_type = media_content_type.removeprefix(MEDIA_PLAYER_PREFIX)

    payload = {
        "media_content_type": media_content_type,
        "media_content_id": media_content_id,
    }
    response = await build_item_response(
        spotify,
        payload,
        can_play_artist=can_play_artist,
    )
    if response is None:
        raise BrowseError(f"Media not found: {media_content_type} / {media_content_id}")
    return response


async def build_item_response(  # noqa: C901
    spotify: SpotifyClient,
    payload: dict[str, str | None],
    *,
    can_play_artist: bool,
) -> BrowseMedia | None:
    """Create response payload for the provided media query."""
    media_content_type = payload["media_content_type"]
    media_content_id = payload["media_content_id"]

    if media_content_type is None or media_content_id is None:
        return None

    title: str | None = None
    image: str | None = None
    items: list[ItemPayload] = []

    if media_content_type == BrowsableMedia.CURRENT_USER_PLAYLISTS:
        if playlists := await spotify.get_playlists_for_current_user():
            items = [_get_playlist_item_payload(playlist) for playlist in playlists]
    elif media_content_type == BrowsableMedia.CURRENT_USER_FOLLOWED_ARTISTS:
        if artists := await spotify.get_followed_artists():
            items = [_get_artist_item_payload(artist) for artist in artists]
    elif media_content_type == BrowsableMedia.CURRENT_USER_SAVED_ALBUMS:
        if saved_albums := await spotify.get_saved_albums():
            items = [
                _get_album_item_payload(saved_album.album)
                for saved_album in saved_albums
            ]
    elif media_content_type == BrowsableMedia.CURRENT_USER_SAVED_TRACKS:
        title = LIBRARY_MAP.get(media_content_type)
        if saved_tracks := await spotify.get_saved_tracks():
            items = [
                _get_track_item_payload(saved_track.track)
                for saved_track in saved_tracks
            ]
    elif media_content_type == BrowsableMedia.CURRENT_USER_SAVED_SHOWS:
        if saved_shows := await spotify.get_saved_shows():
            items = [
                {
                    "id": saved_show.show.show_id,
                    "name": saved_show.show.name,
                    "type": MEDIA_TYPE_SHOW,
                    "uri": saved_show.show.uri,
                    "thumbnail": fetch_image_url(saved_show.show.images),
                }
                for saved_show in saved_shows
            ]
    elif media_content_type == BrowsableMedia.CURRENT_USER_RECENTLY_PLAYED:
        if recently_played_tracks := await spotify.get_recently_played_tracks():
            items = [
                _get_track_item_payload(item.track) for item in recently_played_tracks
            ]
    elif media_content_type == BrowsableMedia.CURRENT_USER_TOP_ARTISTS:
        if top_artists := await spotify.get_top_artists():
            items = [_get_artist_item_payload(artist) for artist in top_artists]
    elif media_content_type == BrowsableMedia.CURRENT_USER_TOP_TRACKS:
        if top_tracks := await spotify.get_top_tracks():
            items = [_get_track_item_payload(track) for track in top_tracks]
    elif media_content_type == MediaType.PLAYLIST:
        try:
            playlist = await spotify.get_playlist(media_content_id)
        except (
            MissingField,
            InvalidFieldValue,
            SpotifyForbiddenError,
            SpotifyNotFoundError,
        ) as err:
            # Correctif Domo DZ : playlist que Spotify ne détaille plus (playlist
            # éditoriale non possédée, core#167322). Le nœud reste jouable ; les
            # vraies pannes (réseau, délai) remontent toujours en erreur.
            _LOGGER.warning(
                "Playlist Spotify '%s' illisible (%s), contenu non affiché",
                media_content_id,
                type(err).__name__,
            )
            playlist = None
            title = "Playlist (contenu non fourni par Spotify)"
        if playlist:
            title = playlist.name
            image = playlist.images[0].url if playlist.images else None
            for playlist_item in playlist.items.items:
                if playlist_item.track.type is ItemType.TRACK:
                    if TYPE_CHECKING:
                        assert isinstance(playlist_item.track, Track)
                    items.append(_get_track_item_payload(playlist_item.track))
                elif playlist_item.track.type is ItemType.EPISODE:
                    if TYPE_CHECKING:
                        assert isinstance(playlist_item.track, Episode)
                    items.append(_get_episode_item_payload(playlist_item.track))
    elif media_content_type == MediaType.ALBUM:
        if album := await spotify.get_album(media_content_id):
            title = album.name
            image = album.images[0].url if album.images else None
            items = [
                _get_track_item_payload(track, show_thumbnails=False)
                for track in album.tracks
            ]
    elif media_content_type == MediaType.ARTIST:
        if (artist_albums := await spotify.get_artist_albums(media_content_id)) and (
            artist := await spotify.get_artist(media_content_id)
        ):
            title = artist.name
            image = artist.images[0].url if artist.images else None
            items = [_get_album_item_payload(album) for album in artist_albums]
    elif media_content_type == BrowsableMedia.SEARCH:
        # Correctif Domo DZ : page vide, le contenu vient de la recherche. Le
        # titre « Spotify » donne une aide lisible (« Search Spotify »).
        title = "Spotify"
    elif media_content_type == MEDIA_TYPE_SHOW:
        if (show_episodes := await spotify.get_show_episodes(media_content_id)) and (
            show := await spotify.get_show(media_content_id)
        ):
            title = show.name
            image = show.images[0].url if show.images else None
            items = [_get_episode_item_payload(episode) for episode in show_episodes]

    try:
        media_class = CONTENT_TYPE_MEDIA_CLASS[media_content_type]
    except KeyError:
        _LOGGER.debug("Unknown media type received: %s", media_content_type)
        return None

    if title is None:
        title = LIBRARY_MAP.get(media_content_id, "Unknown")

    can_play = media_content_type in PLAYABLE_MEDIA_TYPES and (
        media_content_type != MediaType.ARTIST or can_play_artist
    )

    if TYPE_CHECKING:
        assert title
    browse_media = BrowseMedia(
        can_expand=True,
        can_play=can_play,
        children_media_class=media_class["children"],
        media_class=media_class["parent"],
        media_content_id=media_content_id,
        media_content_type=f"{MEDIA_PLAYER_PREFIX}{media_content_type}",
        thumbnail=image,
        title=title,
        # Correctif Domo DZ : barre de recherche sur la seule page
        # « Rechercher » (sur « Liked songs » ou « Playlists », elle laissait
        # croire qu'on filtrait la page alors qu'elle cherche dans tout Spotify).
        can_search=media_content_type == BrowsableMedia.SEARCH,
        search_media_classes=(
            SEARCH_MEDIA_CLASSES
            if media_content_type == BrowsableMedia.SEARCH
            else None
        ),
    )

    browse_media.children = []
    for item in items:
        try:
            browse_media.children.append(
                item_payload(item, can_play_artist=can_play_artist)
            )
        except MissingMediaInformation, UnknownMediaType:
            continue

    return browse_media


def item_payload(item: ItemPayload, *, can_play_artist: bool) -> BrowseMedia:
    """Create response payload for a single media item.

    Used by async_browse_media.
    """
    media_type = item["type"]
    media_id = item["uri"]

    try:
        media_class = CONTENT_TYPE_MEDIA_CLASS[media_type]
    except KeyError as err:
        _LOGGER.debug("Unknown media type received: %s", media_type)
        raise UnknownMediaType from err

    can_expand = media_type not in [
        MediaType.TRACK,
        MediaType.EPISODE,
    ]

    can_play = (
        media_type in PLAYABLE_MEDIA_TYPES
        and (media_type != MediaType.ARTIST or can_play_artist)
        and media_type != BrowsableMedia.CURRENT_USER_SAVED_TRACKS
    )

    return BrowseMedia(
        can_expand=can_expand,
        can_play=can_play,
        children_media_class=media_class["children"],
        media_class=media_class["parent"],
        media_content_id=media_id,
        media_content_type=f"{MEDIA_PLAYER_PREFIX}{media_type}",
        title=item["name"],
        thumbnail=item["thumbnail"],
    )


async def library_payload(*, can_play_artist: bool) -> BrowseMedia:
    """Create response payload to describe contents of a specific library.

    Used by async_browse_media.
    """
    browse_media = BrowseMedia(
        can_expand=True,
        can_play=False,
        children_media_class=MediaClass.DIRECTORY,
        media_class=MediaClass.DIRECTORY,
        media_content_id="library",
        media_content_type=f"{MEDIA_PLAYER_PREFIX}library",
        title="Media Library",
    )

    browse_media.children = []
    for item_type, item_name in LIBRARY_MAP.items():
        browse_media.children.append(
            item_payload(
                {
                    "name": item_name,
                    "type": item_type,
                    "uri": item_type,
                    "id": None,
                    "thumbnail": None,
                },
                can_play_artist=can_play_artist,
            )
        )
    return browse_media


# Correctif Domo DZ : recherche Spotify dans le navigateur de médias.
SEARCH_LIMIT = 10  # maximum accepté par l'API depuis février 2026

SEARCH_TYPE_BY_CLASS: dict[MediaClass, SearchType] = {
    MediaClass.TRACK: SearchType.TRACK,
    MediaClass.ALBUM: SearchType.ALBUM,
    MediaClass.ARTIST: SearchType.ARTIST,
    MediaClass.PLAYLIST: SearchType.PLAYLIST,
}

SEARCH_MEDIA_CLASSES = list(SEARCH_TYPE_BY_CLASS)

# Correctif Domo DZ : explications affichées quand Spotify refuse la recherche.
SEARCH_HTTP_HINTS = {
    401: "session Spotify expirée, reconnectez le compte si cela se répète",
    429: "trop de requêtes envoyées à Spotify, réessayez dans un instant",
}


def _get_simplified_artist_item_payload(artist: SimplifiedArtist) -> ItemPayload:
    return {
        "id": artist.artist_id,
        "name": artist.name,
        "type": MediaType.ARTIST,
        "uri": artist.uri,
        "thumbnail": fetch_image_url(getattr(artist, "images", None) or []),
    }


def _get_search_track_item_payload(track: SimplifiedTrack) -> ItemPayload:
    payload = _get_track_item_payload(track)
    if artists := ", ".join(artist.name for artist in track.artists):
        payload["name"] = f"{track.name} – {artists}"
    return payload


# Correctif Domo DZ : chaque résultat est lu séparément. Un résultat abîmé est
# ignoré sans perdre les autres, et les pistes (lues comme Track, avec leur
# album) et les artistes (lus comme Artist) gardent leur image, que
# SearchResult de spotifyaio 2.0.2 perdait (modèles « Simplified »).
def _search_track_payload(raw: dict[str, Any]) -> ItemPayload:
    try:
        track: SimplifiedTrack = Track.from_dict(raw)
    except Exception:  # noqa: BLE001
        track = SimplifiedTrack.from_dict(raw)
    return _get_search_track_item_payload(track)


def _search_album_payload(raw: dict[str, Any]) -> ItemPayload:
    return _get_album_item_payload(
        SimplifiedAlbum.from_dict({**raw, "images": raw.get("images") or []})
    )


def _search_artist_payload(raw: dict[str, Any]) -> ItemPayload:
    try:
        return _get_artist_item_payload(
            Artist.from_dict({**raw, "images": raw.get("images") or []})
        )
    except Exception:  # noqa: BLE001
        return _get_simplified_artist_item_payload(SimplifiedArtist.from_dict(raw))


def _search_playlist_payload(raw: dict[str, Any]) -> ItemPayload:
    return _get_playlist_item_payload(BasePlaylist.from_dict(dict(raw)))


# Ordre d'affichage : pistes, albums, artistes, playlists.
SEARCH_DECODERS = (
    (SearchType.TRACK, _search_track_payload),
    (SearchType.ALBUM, _search_album_payload),
    (SearchType.ARTIST, _search_artist_payload),
    (SearchType.PLAYLIST, _search_playlist_payload),
)


def _search_error(detail: str) -> SearchError:
    """Journaliser puis construire l'erreur montrée dans le navigateur."""
    _LOGGER.warning("Recherche Spotify impossible (%s)", detail)
    return SearchError(f"Recherche Spotify impossible ({detail})")


def _search_section(data: dict[str, Any], search_type: SearchType) -> list[Any]:
    section = data.get(f"{search_type}s")
    if not isinstance(section, dict) or not isinstance(
        items := section.get("items"), list
    ):
        return []
    return [item for item in items if item is not None]


async def _async_search_raw(
    spotify: SpotifyClient, search_query: str, types: list[SearchType]
) -> dict[str, Any]:
    """Interroger GET /v1/search et contrôler la réponse.

    Correctif Domo DZ : SpotifyClient.search() de spotifyaio 2.0.2 (version figée
    dans manifest.json) ne contrôle que les codes 403 et 404, et transforme un
    refus (429, 401, 400, 500…) en résultat vide : l'utilisateur voyait
    « Aucun résultat » au lieu d'une erreur. On lit donc la réponse brute.
    """
    params = {"q": search_query, "limit": SEARCH_LIMIT, "type": ",".join(types)}
    try:
        text = await spotify._get("v1/search", params=params)  # noqa: SLF001
    except (SpotifyConnectionError, TimeoutError, aiohttp.ClientError) as err:
        raise _search_error(f"Spotify ne répond pas, {type(err).__name__}") from err
    except SpotifyForbiddenError as err:
        raise _search_error("accès refusé par Spotify, HTTP 403") from err
    except SpotifyNotFoundError as err:
        raise _search_error("HTTP 404") from err
    except Exception as err:  # noqa: BLE001
        raise _search_error(type(err).__name__) from err
    try:
        data = orjson.loads(text) if text else {}
    except orjson.JSONDecodeError as err:
        raise _search_error("réponse de Spotify illisible") from err
    if not isinstance(data, dict):
        raise _search_error("réponse de Spotify inattendue")
    if (error := data.get("error")) is not None:
        status = error.get("status") if isinstance(error, dict) else None
        message = (
            error.get("message")
            if isinstance(error, dict)
            else data.get("error_description") or error
        )
        hint = SEARCH_HTTP_HINTS.get(status) if isinstance(status, int) else None
        detail = hint or message or "erreur inconnue"
        raise _search_error(f"HTTP {status} : {detail}" if status else str(detail))
    return data


async def async_search_media_internal(
    spotify: SpotifyClient,
    query: SearchMediaQuery,
    *,
    can_play_artist: bool = True,
) -> SearchMedia:
    """Search Spotify (titres, albums, artistes, playlists)."""
    search_query = query.search_query.strip()
    if not search_query:
        return SearchMedia(result=[])
    types = [
        search_type
        for media_class, search_type in SEARCH_TYPE_BY_CLASS.items()
        if not query.media_filter_classes or media_class in query.media_filter_classes
    ]
    if not types:
        return SearchMedia(result=[])

    data = await _async_search_raw(spotify, search_query, types)

    items: list[ItemPayload] = []
    skipped: list[str] = []
    for search_type, decode in SEARCH_DECODERS:
        if search_type not in types:
            continue
        for raw in _search_section(data, search_type):
            try:
                if not isinstance(raw, dict):
                    raise TypeError(type(raw).__name__)
                items.append(decode(raw))
            except Exception as err:  # noqa: BLE001
                skipped.append(f"{search_type} {_describe_error(err)}")
    if skipped:
        _LOGGER.warning(
            "Recherche Spotify : %d résultat(s) illisible(s) ignoré(s) (%s)",
            len(skipped),
            skipped[0],
        )
        if not items:
            raise _search_error("format de réponse inattendu")

    result: list[BrowseMedia] = []
    for item in items:
        try:
            result.append(item_payload(item, can_play_artist=can_play_artist))
        except MissingMediaInformation, UnknownMediaType:
            continue
    return SearchMedia(result=result)
