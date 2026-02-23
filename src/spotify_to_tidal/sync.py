#!/usr/bin/env python3

import asyncio
from .cache import failure_cache, track_match_cache
import datetime
from difflib import SequenceMatcher
from functools import partial
from typing import Callable, List, Sequence, Set, Mapping
import math
import requests
import sys
import spotipy
import tidalapi
from .tidalapi_patch import add_multiple_tracks_to_playlist, clear_tidal_playlist, get_all_favorites, get_all_playlists, get_all_playlist_tracks
import time
from tqdm.asyncio import tqdm as atqdm
from tqdm import tqdm
import traceback
import unicodedata
import math

from .type import spotify as t_spotify

def normalize(s) -> str:
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('ascii')

def simple(input_string: str) -> str:
    # only take the first part of a string before any hyphens or brackets to account for different versions
    return input_string.split('-')[0].strip().split('(')[0].strip().split('[')[0].strip()

def isrc_match(tidal_track: tidalapi.Track, spotify_track) -> bool:
    if "external_ids" in spotify_track and "isrc" in spotify_track["external_ids"]:
        return tidal_track.isrc == spotify_track["external_ids"]["isrc"]
    return False

def duration_match(tidal_track: tidalapi.Track, spotify_track, tolerance=2) -> bool:
    # the duration of the two tracks must be the same to within 2 seconds
    return abs(tidal_track.duration - spotify_track['duration_ms']/1000) < tolerance

def name_match(tidal_track, spotify_track) -> bool:
    def exclusion_rule(pattern: str, tidal_track: tidalapi.Track, spotify_track: t_spotify.SpotifyTrack):
        spotify_has_pattern = pattern in spotify_track['name'].lower()
        tidal_has_pattern = pattern in tidal_track.name.lower() or (not tidal_track.version is None and (pattern in tidal_track.version.lower()))
        return spotify_has_pattern != tidal_has_pattern

    # handle some edge cases
    if exclusion_rule("instrumental", tidal_track, spotify_track): return False
    if exclusion_rule("acapella", tidal_track, spotify_track): return False
    if exclusion_rule("remix", tidal_track, spotify_track): return False

    # the simplified version of the Spotify track name must be a substring of the Tidal track name
    # Try with both un-normalized and then normalized
    simple_spotify_track = simple(spotify_track['name'].lower()).split('feat.')[0].strip()
    return simple_spotify_track in tidal_track.name.lower() or normalize(simple_spotify_track) in normalize(tidal_track.name.lower())

def artist_match(tidal: tidalapi.Track | tidalapi.Album, spotify) -> bool:
    def split_artist_name(artist: str) -> Sequence[str]:
       if '&' in artist:
           return artist.split('&')
       elif ',' in artist:
           return artist.split(',')
       else:
           return [artist]

    def get_tidal_artists(tidal: tidalapi.Track | tidalapi.Album, do_normalize=False) -> Set[str]:
        result: list[str] = []
        for artist in tidal.artists:
            if do_normalize:
                artist_name = normalize(artist.name)
            else:
                artist_name = artist.name
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])

    def get_spotify_artists(spotify, do_normalize=False) -> Set[str]:
        result: list[str] = []
        for artist in spotify['artists']:
            if do_normalize:
                artist_name = normalize(artist['name'])
            else:
                artist_name = artist['name']
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])
    # There must be at least one overlapping artist between the Tidal and Spotify track
    # Try with both un-normalized and then normalized
    if get_tidal_artists(tidal).intersection(get_spotify_artists(spotify)) != set():
        return True
    return get_tidal_artists(tidal, True).intersection(get_spotify_artists(spotify, True)) != set()

def match(tidal_track, spotify_track) -> bool:
    if not spotify_track['id']: return False
    return isrc_match(tidal_track, spotify_track) or (
        duration_match(tidal_track, spotify_track)
        and name_match(tidal_track, spotify_track)
        and artist_match(tidal_track, spotify_track)
    )

def test_album_similarity(spotify_album, tidal_album, threshold=0.6):
    return SequenceMatcher(None, simple(spotify_album['name']), simple(tidal_album.name)).ratio() >= threshold and artist_match(tidal_album, spotify_album)

async def tidal_search(spotify_track, rate_limiter, tidal_session: tidalapi.Session) -> tuple | None:
    """Returns (tidal_track, is_isrc_match) or None if not found."""
    def _search_for_track_in_album():
        # search for album name and first album artist
        if 'album' in spotify_track and 'artists' in spotify_track['album'] and len(spotify_track['album']['artists']):
            query = simple(spotify_track['album']['name']) + " " + simple(spotify_track['album']['artists'][0]['name'])
            album_result = tidal_session.search(query, models=[tidalapi.album.Album])
            for album in album_result['albums']:
                if album.num_tracks >= spotify_track['track_number'] and test_album_similarity(spotify_track['album'], album):
                    album_tracks = album.tracks()
                    if len(album_tracks) < spotify_track['track_number']:
                        assert( not len(album_tracks) == album.num_tracks ) # incorrect metadata :(
                        continue
                    track = album_tracks[spotify_track['track_number'] - 1]
                    if match(track, spotify_track):
                        failure_cache.remove_match_failure(spotify_track['id'])
                        return (track, isrc_match(track, spotify_track))

    def _search_for_standalone_track():
        # if album search fails then search for track name and first artist
        query = simple(spotify_track['name']) + ' ' + simple(spotify_track['artists'][0]['name'])
        for track in tidal_session.search(query, models=[tidalapi.media.Track])['tracks']:
            if match(track, spotify_track):
                failure_cache.remove_match_failure(spotify_track['id'])
                return (track, isrc_match(track, spotify_track))

    artist_name = spotify_track['artists'][0]['name'] if spotify_track.get('artists') else '?'
    print(f"  Searching: {artist_name} - {spotify_track.get('name', '?')}")
    await rate_limiter.acquire()
    album_search = await asyncio.to_thread( _search_for_track_in_album )
    if album_search:
        return album_search
    await rate_limiter.acquire()
    track_search = await asyncio.to_thread( _search_for_standalone_track )
    if track_search:
        return track_search

    # if none of the search modes succeeded then store the track id to the failure cache
    failure_cache.cache_match_failure(spotify_track['id'])

async def repeat_on_request_error(function, *args, remaining=5, **kwargs):
    # utility to repeat calling the function up to 5 times if an exception is thrown
    try:
        return await function(*args, **kwargs)
    except (tidalapi.exceptions.TooManyRequests, requests.exceptions.RequestException, spotipy.exceptions.SpotifyException) as e:
        if remaining:
            print(f"{str(e)} occurred, retrying {remaining} times")
        else:
            print(f"{str(e)} could not be recovered")

        if isinstance(e, requests.exceptions.RequestException) and not e.response is None:
            print(f"Response message: {e.response.text}")
            print(f"Response headers: {e.response.headers}")

        if not remaining:
            print("Aborting sync")
            print(f"The following arguments were provided:\n\n {str(args)}")
            print(traceback.format_exc())
            sys.exit(1)
        sleep_schedule = {5: 1, 4:10, 3:60, 2:5*60, 1:10*60} # sleep variable length of time depending on retry number
        time.sleep(sleep_schedule.get(remaining, 1))
        return await repeat_on_request_error(function, *args, remaining=remaining-1, **kwargs)


def _extract_tracks_from_items(items: list) -> List[dict]:
    """Extract tracks from API response items, handling both old ({'track': ...}) and new ({'item': ...}) formats."""
    output = []
    for item in items:
        if item is None:
            continue
        if 'track' in item and isinstance(item['track'], dict) and item['track'] is not None:
            output.append(item['track'])
        elif 'item' in item and isinstance(item['item'], dict) and item['item'] is not None:
            output.append(item['item'])
    return output

async def _fetch_all_from_spotify_in_chunks(fetch_function: Callable) -> List[dict]:
    output = []
    results = fetch_function(0)
    print(f"  API response keys: {list(results.keys())}")
    print(f"  Total: {results.get('total', '?')}, Limit: {results.get('limit', '?')}, Items count: {len(results.get('items', []))}")
    if results.get('items'):
        first = results['items'][0]
        print(f"  First item keys: {list(first.keys()) if isinstance(first, dict) else type(first)}")
        import json
        print(f"  First item preview: {json.dumps(first, default=str)[:500]}")
    output.extend(_extract_tracks_from_items(results['items']))
    print(f"  Extracted {len(output)} tracks from first batch")

    # Get all the remaining tracks in parallel
    if results['next']:
        offsets = [results['limit'] * n for n in range(1, math.ceil(results['total'] / results['limit']))]
        extra_results = await atqdm.gather(
            *[asyncio.to_thread(fetch_function, offset) for offset in offsets],
            desc="Fetching additional data chunks"
        )
        for extra_result in extra_results:
            output.extend(_extract_tracks_from_items(extra_result['items']))

    return output


async def get_tracks_from_spotify_playlist(spotify_session: spotipy.Spotify, spotify_playlist):
    def _get_tracks_from_spotify_playlist(offset: int, playlist_id: str):
        fields = "next,total,limit,items(track(name,album(name,artists),artists,track_number,duration_ms,id)),type"
        return spotify_session.playlist_items(playlist_id=playlist_id, offset=offset)

    print(f"Loading tracks from Spotify playlist '{spotify_playlist['name']}'")
    items = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, lambda offset: _get_tracks_from_spotify_playlist(offset=offset, playlist_id=spotify_playlist["id"]))
    track_filter = lambda item: item.get('type', 'track') == 'track' # type may be 'episode' also
    sanity_filter = lambda item: ('album' in item
                                  and 'name' in item['album']
                                  and 'artists' in item['album']
                                  and len(item['album']['artists']) > 0
                                  and item['album']['artists'][0]['name'] is not None)
    return list(filter(sanity_filter, filter(track_filter, items)))

def populate_track_match_cache(spotify_tracks_: Sequence[t_spotify.SpotifyTrack], tidal_tracks_: Sequence[tidalapi.Track]):
    """ Populate the track match cache with all the existing tracks in Tidal playlist corresponding to Spotify playlist.
        Returns (isrc_count, fuzzy_matches) for reporting. """
    isrc_count = 0
    fuzzy_matches = []

    def _record_match(spotify_track, tidal_track):
        nonlocal isrc_count
        track_match_cache.insert((spotify_track['id'], tidal_track.id))
        if isrc_match(tidal_track, spotify_track):
            isrc_count += 1
        else:
            sp_artist = ', '.join([a['name'] for a in spotify_track['artists']])
            sp_name = spotify_track['name']
            td_artist = ', '.join([a.name for a in tidal_track.artists])
            td_name = tidal_track.name
            fuzzy_matches.append((sp_artist, sp_name, td_artist, td_name))

    def _populate_one_track_from_spotify(spotify_track: t_spotify.SpotifyTrack):
        for idx, tidal_track in list(enumerate(tidal_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                _record_match(spotify_track, tidal_track)
                tidal_tracks.pop(idx)
                return

    def _populate_one_track_from_tidal(tidal_track: tidalapi.Track):
        for idx, spotify_track in list(enumerate(spotify_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                _record_match(spotify_track, tidal_track)
                spotify_tracks.pop(idx)
                return

    # make a copy of the tracks to avoid modifying original arrays
    spotify_tracks = [t for t in spotify_tracks_]
    tidal_tracks = [t for t in tidal_tracks_]

    # first populate from the tidal tracks
    for track in tidal_tracks:
        _populate_one_track_from_tidal(track)
    # then populate from the subset of Spotify tracks that didn't match (to account for many-to-one style mappings)
    for track in spotify_tracks:
        _populate_one_track_from_spotify(track)

    return isrc_count, fuzzy_matches

def _strip_for_compare(s: str) -> str:
    """Strip string down to lowercase alphanumeric for loose comparison."""
    import re
    return re.sub(r'[^a-z0-9]', '', s.lower())

def _is_suspicious_fuzzy(sp_artist, sp_name, td_artist, td_name) -> bool:
    """Return True if the fuzzy match looks different enough to flag."""
    return (_strip_for_compare(sp_artist) != _strip_for_compare(td_artist) or
            _strip_for_compare(sp_name) != _strip_for_compare(td_name))

def _print_match_report(playlist_name: str, cache_isrc: int, cache_fuzzy: list, search_isrc: int, search_fuzzy: list, not_found: int, suspicious_only: bool = False):
    """Print a combined match report for a playlist."""
    total_isrc = cache_isrc + search_isrc
    all_fuzzy = cache_fuzzy + search_fuzzy
    total = total_isrc + len(all_fuzzy) + not_found
    print(f"\n  === Match Report for '{playlist_name}' ===")
    print(f"  ISRC matches:      {total_isrc} ({cache_isrc} cached + {search_isrc} new)")
    print(f"  Fuzzy matches:     {len(all_fuzzy)} ({len(cache_fuzzy)} cached + {len(search_fuzzy)} new)")
    print(f"  Not found:         {not_found}")
    print(f"  Total:             {total}")
    if all_fuzzy:
        if suspicious_only:
            flagged = [m for m in all_fuzzy if _is_suspicious_fuzzy(*m)]
        else:
            flagged = all_fuzzy
        if flagged:
            label = "Suspicious fuzzy matches" if suspicious_only else "Fuzzy matches (please verify)"
            print(f"\n  --- {label} ({len(flagged)}/{len(all_fuzzy)}) ---")
            for sp_artist, sp_name, td_artist, td_name in flagged:
                print(f"    Spotify: {sp_artist} - {sp_name}")
                print(f"    Tidal:   {td_artist} - {td_name}")
                print()
        elif suspicious_only:
            print(f"  All {len(all_fuzzy)} fuzzy matches look OK (artist/title match after normalization)")

def get_new_spotify_tracks(spotify_tracks: Sequence[t_spotify.SpotifyTrack]) -> List[t_spotify.SpotifyTrack]:
    ''' Extracts only the tracks that have not already been seen in our Tidal caches '''
    results = []
    for spotify_track in spotify_tracks:
        if not spotify_track['id']: continue
        if not track_match_cache.get(spotify_track['id']) and not failure_cache.has_match_failure(spotify_track['id']):
            results.append(spotify_track)
    return results

def get_tracks_for_new_tidal_playlist(spotify_tracks: Sequence[t_spotify.SpotifyTrack]) -> Sequence[int]:
    ''' gets list of corresponding tidal track ids for each spotify track, ignoring duplicates '''
    output = []
    seen_tracks = set()

    for spotify_track in spotify_tracks:
        if not spotify_track['id']: continue
        tidal_id = track_match_cache.get(spotify_track['id'])
        if tidal_id:
            if tidal_id in seen_tracks:
                track_name = spotify_track['name']
                artist_names = ', '.join([artist['name'] for artist in spotify_track['artists']])
                print(f'Duplicate found: Track "{track_name}" by {artist_names} will be ignored') 
            else:
                output.append(tidal_id)
                seen_tracks.add(tidal_id)
    return output

async def search_new_tracks_on_tidal(tidal_session: tidalapi.Session, spotify_tracks: Sequence[t_spotify.SpotifyTrack], playlist_name: str, config: dict):
    """ Generic function for searching for each item in a list of Spotify tracks which have not already been seen and adding them to the cache """
    async def _run_rate_limiter(semaphore):
        ''' Leaky bucket algorithm for rate limiting. Periodically releases items from semaphore at rate_limit'''
        _sleep_time = config.get('max_concurrency', 10)/config.get('rate_limit', 10)/4 # aim to sleep approx time to drain 1/4 of 'bucket'
        t0 = datetime.datetime.now()
        while True:
            await asyncio.sleep(_sleep_time)
            t = datetime.datetime.now()
            dt = (t - t0).total_seconds()
            new_items = round(config.get('rate_limit', 10)*dt)
            t0 = t
            [semaphore.release() for i in range(new_items)] # leak new_items from the 'bucket'

    # Extract the new tracks that do not already exist in the old tidal tracklist
    tracks_to_search = get_new_spotify_tracks(spotify_tracks)
    if not tracks_to_search:
        return 0, [], 0

    # Search for each of the tracks on Tidal concurrently
    task_description = "Searching Tidal for {}/{} tracks in Spotify playlist '{}'".format(len(tracks_to_search), len(spotify_tracks), playlist_name)
    semaphore = asyncio.Semaphore(config.get('max_concurrency', 10))
    rate_limiter_task = asyncio.create_task(_run_rate_limiter(semaphore))
    search_results = await atqdm.gather( *[ repeat_on_request_error(tidal_search, t, semaphore, tidal_session) for t in tracks_to_search ], desc=task_description )
    rate_limiter_task.cancel()

    # Add the search results to the cache and collect match report
    song404 = []
    search_fuzzy = []
    search_isrc = 0
    for idx, spotify_track in enumerate(tracks_to_search):
        result = search_results[idx]
        if result:
            tidal_track, was_isrc = result
            track_match_cache.insert( (spotify_track['id'], tidal_track.id) )
            if was_isrc:
                search_isrc += 1
            else:
                sp_artist = ', '.join([a['name'] for a in spotify_track['artists']])
                sp_name = spotify_track['name']
                td_artist = ', '.join([a.name for a in tidal_track.artists])
                td_name = tidal_track.name
                search_fuzzy.append((sp_artist, sp_name, td_artist, td_name))
        else:
            song404.append(f"{spotify_track['id']}: {','.join([a['name'] for a in spotify_track['artists']])} - {spotify_track['name']}")
            color = ('\033[91m', '\033[0m')
            print(color[0] + "Could not find the track " + song404[-1] + color[1])

    file_name = "songs not found.txt"
    header = f"==========================\nPlaylist: {playlist_name}\n==========================\n"
    with open(file_name, "a", encoding="utf-8") as file:
        file.write(header)
        for song in song404:
            file.write(f"{song}\n")

    return search_isrc, search_fuzzy, len(song404)

async def sync_playlist(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, spotify_playlist, tidal_playlist: tidalapi.Playlist | None, config: dict):
    """ sync given playlist to tidal """
    # Get the tracks from both Spotify and Tidal, creating a new Tidal playlist if necessary
    spotify_tracks = await get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)
    if len(spotify_tracks) == 0:
        return # nothing to do
    if tidal_playlist:
        old_tidal_tracks = await get_all_playlist_tracks(tidal_playlist)
    else:
        print(f"No playlist found on Tidal corresponding to Spotify playlist: '{spotify_playlist['name']}', creating new playlist")
        tidal_playlist =  tidal_session.user.create_playlist(spotify_playlist['name'], spotify_playlist['description'])
        old_tidal_tracks = []

    # Extract the new tracks from the playlist that we haven't already seen before
    cache_isrc, cache_fuzzy = populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    search_isrc, search_fuzzy, not_found = await search_new_tracks_on_tidal(tidal_session, spotify_tracks, spotify_playlist['name'], config)
    _print_match_report(spotify_playlist['name'], cache_isrc, cache_fuzzy, search_isrc, search_fuzzy, not_found, config.get('suspicious_only', False))
    new_tidal_track_ids = get_tracks_for_new_tidal_playlist(spotify_tracks)

    # Update the Tidal playlist if there are changes
    old_tidal_track_ids = [t.id for t in old_tidal_tracks]
    if new_tidal_track_ids == old_tidal_track_ids:
        print("No changes to write to Tidal playlist")
    elif new_tidal_track_ids[:len(old_tidal_track_ids)] == old_tidal_track_ids:
        # Append new tracks to the existing playlist if possible
        add_multiple_tracks_to_playlist(tidal_playlist, new_tidal_track_ids[len(old_tidal_track_ids):])
    else:
        # Erase old playlist and add new tracks from scratch if any reordering occured
        clear_tidal_playlist(tidal_playlist)
        add_multiple_tracks_to_playlist(tidal_playlist, new_tidal_track_ids)

async def sync_playlist_from_tracks(tidal_session: tidalapi.Session, spotify_playlist: dict, tidal_playlist, spotify_tracks: list, config: dict):
    """ sync a playlist to tidal using pre-loaded spotify tracks (from JSON export) """
    if len(spotify_tracks) == 0:
        print(f"  Skipping '{spotify_playlist['name']}' (no tracks)")
        return
    if tidal_playlist:
        old_tidal_tracks = await get_all_playlist_tracks(tidal_playlist)
    else:
        print(f"  No playlist found on Tidal for '{spotify_playlist['name']}', creating new playlist")
        tidal_playlist = tidal_session.user.create_playlist(spotify_playlist['name'], spotify_playlist.get('description', ''))
        old_tidal_tracks = []

    cache_isrc, cache_fuzzy = populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    search_isrc, search_fuzzy, not_found = await search_new_tracks_on_tidal(tidal_session, spotify_tracks, spotify_playlist['name'], config)
    _print_match_report(spotify_playlist['name'], cache_isrc, cache_fuzzy, search_isrc, search_fuzzy, not_found, config.get('suspicious_only', False))
    new_tidal_track_ids = get_tracks_for_new_tidal_playlist(spotify_tracks)

    old_tidal_track_ids = [t.id for t in old_tidal_tracks]
    if new_tidal_track_ids == old_tidal_track_ids:
        print("  No changes to write to Tidal playlist")
    elif new_tidal_track_ids[:len(old_tidal_track_ids)] == old_tidal_track_ids:
        add_multiple_tracks_to_playlist(tidal_playlist, new_tidal_track_ids[len(old_tidal_track_ids):])
    else:
        clear_tidal_playlist(tidal_playlist)
        add_multiple_tracks_to_playlist(tidal_playlist, new_tidal_track_ids)

async def sync_favorites(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict):
    """ sync user favorites to tidal """
    async def get_tracks_from_spotify_favorites() -> List[dict]:
        _get_favorite_tracks = lambda offset: spotify_session.current_user_saved_tracks(offset=offset)    
        tracks = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, _get_favorite_tracks)
        tracks.reverse()
        return tracks

    def get_new_tidal_favorites() -> List[int]:
        existing_favorite_ids = set([track.id for track in old_tidal_tracks])
        new_ids = []
        for spotify_track in spotify_tracks:
            match_id = track_match_cache.get(spotify_track['id'])
            if match_id and not match_id in existing_favorite_ids:
                new_ids.append(match_id)
        return new_ids

    print("Loading favorite tracks from Spotify")
    spotify_tracks = await get_tracks_from_spotify_favorites()
    print("Loading existing favorite tracks from Tidal")
    old_tidal_tracks = await get_all_favorites(tidal_session.user.favorites, order='DATE')
    cache_isrc, cache_fuzzy = populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    search_isrc, search_fuzzy, not_found = await search_new_tracks_on_tidal(tidal_session, spotify_tracks, "Favorites", config)
    _print_match_report("Favorites", cache_isrc, cache_fuzzy, search_isrc, search_fuzzy, not_found, config.get('suspicious_only', False))
    new_tidal_favorite_ids = get_new_tidal_favorites()
    if new_tidal_favorite_ids:
        for tidal_id in tqdm(new_tidal_favorite_ids, desc="Adding new tracks to Tidal favorites"):
            tidal_session.user.favorites.add_track(tidal_id)
    else:
        print("No new tracks to add to Tidal favorites")

def sync_playlists_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, playlists, config: dict):
  for spotify_playlist, tidal_playlist in playlists:
    # sync the spotify playlist to tidal
    asyncio.run(sync_playlist(spotify_session, tidal_session, spotify_playlist, tidal_playlist, config) )

def sync_favorites_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    asyncio.run(main=sync_favorites(spotify_session=spotify_session, tidal_session=tidal_session, config=config))

def get_tidal_playlists_wrapper(tidal_session: tidalapi.Session) -> Mapping[str, tidalapi.Playlist]:
    tidal_playlists = asyncio.run(get_all_playlists(tidal_session.user))
    return {playlist.name: playlist for playlist in tidal_playlists}

def pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists: Mapping[str, tidalapi.Playlist]):
    if spotify_playlist['name'] in tidal_playlists:
      # if there's an existing tidal playlist with the name of the current playlist then use that
      tidal_playlist = tidal_playlists[spotify_playlist['name']]
      return (spotify_playlist, tidal_playlist)
    else:
      return (spotify_playlist, None)

def get_user_playlist_mappings(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    results = []
    spotify_playlists = asyncio.run(get_playlists_from_spotify(spotify_session, config))
    tidal_playlists = get_tidal_playlists_wrapper(tidal_session)
    for spotify_playlist in spotify_playlists:
        results.append( pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists) )
    return results

async def get_playlists_from_spotify(spotify_session: spotipy.Spotify, config):
    # get all the playlists from the Spotify account
    playlists = []
    print("Loading Spotify playlists")
    first_results = spotify_session.current_user_playlists()
    exclude_list = set([x.split(':')[-1] for x in config.get('excluded_playlists', [])])
    playlists.extend([p for p in first_results['items']])
    user_id = spotify_session.current_user()['id']

    # get all the remaining playlists in parallel
    if first_results['next']:
        offsets = [ first_results['limit'] * n for n in range(1, math.ceil(first_results['total']/first_results['limit'])) ]
        extra_results = await atqdm.gather( *[asyncio.to_thread(spotify_session.current_user_playlists, offset=offset) for offset in offsets ] )
        for extra_result in extra_results:
            playlists.extend([p for p in extra_result['items']])

    # filter out playlists that don't belong to us or are on the exclude list
    my_playlist_filter = lambda p: p and p['owner']['id'] == user_id
    exclude_filter = lambda p: not p['id'] in exclude_list
    return list(filter( exclude_filter, filter( my_playlist_filter, playlists )))

def get_playlists_from_config(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    # get the list of playlist sync mappings from the configuration file
    def get_playlist_ids(config):
        return [(item['spotify_id'], item['tidal_id']) for item in config['sync_playlists']]
    output = []
    for spotify_id, tidal_id in get_playlist_ids(config=config):
        try:
            spotify_playlist = spotify_session.playlist(playlist_id=spotify_id)
        except spotipy.SpotifyException as e:
            print(f"Error getting Spotify playlist {spotify_id}")
            raise e
        try:
            tidal_playlist = tidal_session.playlist(playlist_id=tidal_id)
        except Exception as e:
            print(f"Error getting Tidal playlist {tidal_id}")
            raise e
        output.append((spotify_playlist, tidal_playlist))
    return output

