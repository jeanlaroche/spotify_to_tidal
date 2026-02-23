import yaml
import argparse
import sys

from . import sync as _sync
from . import auth as _auth

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yml', help='location of the config file')
    parser.add_argument('--uri', help='synchronize a specific URI instead of the one in the config')
    parser.add_argument('--sync-favorites', action=argparse.BooleanOptionalAction, help='synchronize the favorites')
    parser.add_argument('--list', action='store_true', help='list all Spotify playlists with their URIs')
    parser.add_argument('--export', action='store_true', help='export all Spotify playlists to JSON files in an export/ directory')
    parser.add_argument('--from-export', nargs='*', metavar='FILE', help='sync to Tidal from exported JSON files instead of Spotify. If no files specified, uses all JSON files in export/')
    parser.add_argument('--suspicious-only', action='store_true', help='in match report, only show fuzzy matches where artist/title actually differ')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)
    config['suspicious_only'] = args.suspicious_only

    if args.from_export is not None:
        import asyncio
        import json
        import glob
        import os

        # Collect JSON files
        if args.from_export:
            json_files = args.from_export
        else:
            json_files = sorted(glob.glob('export/*.json'))
        if not json_files:
            sys.exit("No JSON files found. Run --export first.")

        print("Opening Tidal session")
        tidal_session = _auth.open_tidal_session()
        if not tidal_session.check_login():
            sys.exit("Could not connect to Tidal")
        tidal_playlists = _sync.get_tidal_playlists_wrapper(tidal_session)

        print(f"\nSyncing {len(json_files)} playlists from export files\n")
        for filepath in json_files:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            playlist_name = data['playlist_name']
            description = data.get('description', '')
            tracks = data['tracks']
            print(f"  {playlist_name} ({len(tracks)} tracks) from {os.path.basename(filepath)}")

            # Build a fake spotify_playlist dict with what sync_playlist needs
            spotify_playlist = {'name': playlist_name, 'description': description, 'id': data.get('playlist_id', '')}
            tidal_playlist = tidal_playlists.get(playlist_name, None)

            asyncio.run(_sync.sync_playlist_from_tracks(tidal_session, spotify_playlist, tidal_playlist, tracks, config))

        print("\nDone!")
        sys.exit(0)

    print("Opening Spotify session")
    spotify_session = _auth.open_spotify_session(config['spotify'])
    print("Opening Tidal session")
    tidal_session = _auth.open_tidal_session()
    if not tidal_session.check_login():
        sys.exit("Could not connect to Tidal")
    if args.list or args.export:
        import asyncio
        playlists = asyncio.run(_sync.get_playlists_from_spotify(spotify_session, config))
        if args.list:
            print(f"\nFound {len(playlists)} playlists:\n")
            for i, p in enumerate(playlists, 1):
                print(f"  {i:3d}. {p['name']:<50s}  {p['id']}")
            sys.exit(0)
        if args.export:
            import asyncio
            import json
            import os
            import re
            export_dir = 'export'
            os.makedirs(export_dir, exist_ok=True)
            print(f"\nExporting {len(playlists)} playlists to {export_dir}/\n")
            for i, p in enumerate(playlists, 1):
                tracks = asyncio.run(_sync.get_tracks_from_spotify_playlist(spotify_session, p))
                safe_name = re.sub(r'[^\w\s-]', '_', p['name']).strip()
                filename = os.path.join(export_dir, f"{safe_name}.json")
                export_data = {
                    'playlist_name': p['name'],
                    'playlist_id': p['id'],
                    'playlist_uri': p.get('uri', ''),
                    'description': p.get('description', ''),
                    'total_tracks': len(tracks),
                    'tracks': tracks,
                }
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)
                print(f"  {i:3d}. {p['name']} ({len(tracks)} tracks) -> {filename}")

            # Export liked songs
            print(f"\nExporting liked songs...")
            _get_favorites = lambda offset: spotify_session.current_user_saved_tracks(offset=offset)
            favorites = asyncio.run(_sync._fetch_all_from_spotify_in_chunks(_get_favorites))
            favorites.reverse()
            filename = os.path.join(export_dir, "_Liked_Songs.json")
            export_data = {
                'playlist_name': 'Liked Songs',
                'playlist_id': '_liked_songs',
                'playlist_uri': '',
                'description': 'Liked/saved tracks',
                'total_tracks': len(favorites),
                'tracks': favorites,
            }
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)
            print(f"  Liked Songs ({len(favorites)} tracks) -> {filename}")

            print(f"\nDone! Exported {len(playlists)} playlists + liked songs.")
            sys.exit(0)
    if args.uri:
        # if a playlist ID is explicitly provided as a command line argument then use that
        spotify_playlist = spotify_session.playlist(args.uri)
        tidal_playlists = _sync.get_tidal_playlists_wrapper(tidal_session)
        tidal_playlist = _sync.pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists)
        _sync.sync_playlists_wrapper(spotify_session, tidal_session, [tidal_playlist], config)
        sync_favorites = args.sync_favorites # only sync favorites if command line argument explicitly passed
    elif args.sync_favorites:
        sync_favorites = True # sync only the favorites
    elif config.get('sync_playlists', None):
        # if the config contains a sync_playlists list of mappings then use that
        _sync.sync_playlists_wrapper(spotify_session, tidal_session, _sync.get_playlists_from_config(spotify_session, tidal_session, config), config)
        sync_favorites = args.sync_favorites is None and config.get('sync_favorites_default', True)
    else:
        # otherwise sync all the user playlists in the Spotify account and favorites unless explicitly disabled
        _sync.sync_playlists_wrapper(spotify_session, tidal_session, _sync.get_user_playlist_mappings(spotify_session, tidal_session, config), config)
        sync_favorites = args.sync_favorites is None and config.get('sync_favorites_default', True)

    if sync_favorites:
        _sync.sync_favorites_wrapper(spotify_session, tidal_session, config)

if __name__ == '__main__':
    main()
    sys.exit(0)
