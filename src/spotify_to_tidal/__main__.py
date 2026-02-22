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
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)
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
            print(f"\nDone! Exported {len(playlists)} playlists.")
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
