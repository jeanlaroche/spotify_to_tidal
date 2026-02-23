A command line tool for importing your Spotify playlists into Tidal. Due to various performance optimisations, it is particularly suited for periodic synchronisation of very large collections.

Installation
-----------
Clone this git repository and then run:

```bash
uv sync --python 3.10
source .venv/bin/activate
```

This creates a virtual environment with Python 3.10, installs all dependencies, and activates the environment.

Setup
-----
0. Rename the file example_config.yml to config.yml
0. Go [here](https://developer.spotify.com/documentation/general/guides/authorization/app-settings/) and register a new app on developer.spotify.com.
0. Copy and paste your client ID and client secret to the Spotify part of the config file
0. Copy and paste the value in 'redirect_uri' of the config file to Redirect URIs at developer.spotify.com and press ADD
0. Enter your Spotify username to the config file

Usage
----

### Listing playlists

Start by listing all your Spotify playlists with their IDs:

```bash
spotify_to_tidal --list
```

### Exporting playlists to JSON

Export all your Spotify playlists and liked songs to JSON files in an `export/` directory. This is the recommended approach: you only need to fetch from Spotify once, and subsequent syncs to Tidal use the local files, so you don't risk running into Spotify's rate limits:

```bash
spotify_to_tidal --export
```

To export only liked songs (without fetching all playlists):

```bash
python export_liked.py
```

### Syncing from exported JSON files

Sync to Tidal using previously exported JSON files instead of querying Spotify. This only requires a Tidal session:

```bash
# Sync all exported playlists
spotify_to_tidal --from-export

# Sync specific files
spotify_to_tidal --from-export export/MyPlaylist.json export/_Liked_Songs.json
```

### Match report and suspicious matches

After syncing, a match report shows how tracks were matched (by ISRC or fuzzy name/artist matching). To only display fuzzy matches where the artist or title actually differ (ignoring case, punctuation, etc.):

```bash
spotify_to_tidal --from-export --suspicious-only
```

### Direct sync (without exporting first)

To synchronize all of your Spotify playlists directly with your Tidal account:

```bash
spotify_to_tidal
```

You can also just synchronize a specific playlist by doing the following:

```bash
spotify_to_tidal --uri 1ABCDEqsABCD6EaABCDa0a # accepts playlist id or full playlist uri
```

or sync just your 'Liked Songs' with:

```bash
spotify_to_tidal --sync-favorites
```

See example_config.yml for more configuration options, and `spotify_to_tidal --help` for more options.

---

#### Join our amazing community as a code contributor
<br><br>
<a href="https://github.com/spotify2tidal/spotify_to_tidal/graphs/contributors">
  <img class="dark-light" src="https://contrib.rocks/image?repo=spotify2tidal/spotify_to_tidal&anon=0&columns=25&max=100&r=true" />
</a>
