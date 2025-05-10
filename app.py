from flask import Flask, request, Response, jsonify, redirect
import requests
from flask_cors import CORS
import jwt
import json
import urllib.parse
import os
import time
import logging
from xml.etree import ElementTree
import re
import threading
import uuid

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# JWT Secret Key - Change this to a secure secret in production
JWT_SECRET = "your-secret-key-change-this"
# How long the JWT token is valid (in seconds)
JWT_EXPIRY = 3600 * 24  # 24 hours

# Store active streams with their details
active_streams = {}

class StreamPlayerWithAuth:
    def __init__(self, stream_url, stream_id):
        self.stream_url = stream_url
        self.stream_id = stream_id
        self.base_url = self.extract_base_url(stream_url)
        self.query_params = self.extract_query_params(stream_url)
        self.download_folder = f"temp_videos/{stream_id}"
        self.is_mpd = stream_url.lower().endswith('.mpd')
        self.is_m3u8 = stream_url.lower().endswith('.m3u8')
        self.parsed_manifest = None
        self.cache = {}  # Cache for segment responses
        
        # MPD namespace for parsing (only used for MPD files)
        self.ns = {'mpd': 'urn:mpeg:dash:schema:mpd:2011'}
        
        # Create directory if it doesn't exist
        if not os.path.exists(self.download_folder):
            os.makedirs(self.download_folder)

    def extract_base_url(self, url):
        # Extract base part from URL (without query parameters)
        parsed_url = urllib.parse.urlparse(url)
        return f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    
    def extract_query_params(self, url):
        # Extract query parameters from URL
        if '?' not in url:
            return {}
        query_string = url.split('?', 1)[1]
        return dict(param.split('=') for param in query_string.split('&') if '=' in param)
    
    def add_auth_params_to_url(self, url):
        # Add authentication parameters to URL
        if '?' in url:
            base_url = url.split('?')[0]
        else:
            base_url = url
            
        # URL encode parameters as they might contain special characters
        query_params = urllib.parse.urlencode(self.query_params)
        return f"{base_url}?{query_params}"
    
    def fetch_manifest(self):
        """Download and parse manifest file (MPD or M3U8)"""
        logging.info(f"Downloading manifest from URL: {self.stream_url}")
        
        try:
            response = requests.get(self.stream_url)
            response.raise_for_status()
            
            if self.is_mpd:
                # Save MPD file
                manifest_path = os.path.join(self.download_folder, "manifest.mpd")
                with open(manifest_path, 'wb') as f:
                    f.write(response.content)
                
                # Parse XML for MPD
                self.parsed_manifest = ElementTree.fromstring(response.content)
                
                # Modify MPD for proxy playback
                self.modify_mpd_for_proxy()
                
                return True
            
            elif self.is_m3u8:
                # Save M3U8 file
                manifest_path = os.path.join(self.download_folder, "playlist.m3u8")
                with open(manifest_path, 'wb') as f:
                    f.write(response.content)
                
                # For M3U8, we store the content as string
                self.parsed_manifest = response.text
                
                # Modify M3U8 for proxy playback
                self.modify_m3u8_for_proxy(manifest_path)
                
                return True
                
            else:
                logging.error(f"Unsupported manifest format: {self.stream_url}")
                return False
                
        except Exception as e:
            logging.error(f"Error downloading manifest file: {e}")
            return False
    
    def modify_mpd_for_proxy(self):
        """Modify MPD file for proxy playback"""
        if not self.parsed_manifest:
            logging.error("Please fetch MPD file first")
            return
        
        mpd_path = os.path.join(self.download_folder, "manifest.mpd")
        
        try:
            # Read MPD from file
            with open(mpd_path, 'r', encoding='utf-8') as f:
                mpd_content = f.read()
            
            # Replace absolute URLs with relative paths that point to our API
            mpd_content = re.sub(r'(initialization|media)="https?://[^/]+/[^"]*?/([^"]+)"', 
                                r'\1="\2"', mpd_content)
            
            # Save modified MPD
            with open(mpd_path, 'w', encoding='utf-8') as f:
                f.write(mpd_content)
            
            logging.info(f"MPD file successfully modified for proxy playback: {mpd_path}")
        except Exception as e:
            logging.error(f"Error modifying MPD file: {e}")
    
    def modify_m3u8_for_proxy(self, m3u8_path):
        """Modify M3U8 file for proxy playback"""
        try:
            # Read M3U8 from file
            with open(m3u8_path, 'r', encoding='utf-8') as f:
                m3u8_content = f.read()
            
            # Get the base URL of the M3U8 file (everything before the last slash)
            base_url = os.path.dirname(self.stream_url)
            
            # Process lines in the M3U8 file
            modified_lines = []
            for line in m3u8_content.splitlines():
                # Skip comments and directives
                if line.startswith('#'):
                    # Check for EXT-X-STREAM-INF (for master playlists) or EXT-X-KEY (for encryption)
                    if '#EXT-X-STREAM-INF:' in line or '#EXT-X-KEY:' in line:
                        # Replace URIs in EXT-X-KEY directives
                        if '#EXT-X-KEY:' in line and 'URI="' in line:
                            parts = line.split('URI="')
                            if len(parts) > 1:
                                uri_part = parts[1].split('"')[0]
                                # Keep local paths as is, convert absolute URLs to paths
                                if uri_part.startswith('http'):
                                    # Extract segment name
                                    segment_name = uri_part.split('/')[-1]
                                    line = f"{parts[0]}URI=\"{segment_name}\""
                    modified_lines.append(line)
                # Process URLs
                elif line and not line.startswith('#'):
                    # Handle absolute URLs
                    if line.startswith('http'):
                        # Extract segment name
                        segment_name = line.split('/')[-1]
                        modified_lines.append(segment_name)
                    # Handle relative URLs
                    elif not line.startswith('/'):
                        modified_lines.append(line)
                    # Handle absolute paths
                    else:
                        # Extract segment name
                        segment_name = line.split('/')[-1]
                        modified_lines.append(segment_name)
            
            # Join lines and save modified M3U8
            modified_content = '\n'.join(modified_lines)
            with open(m3u8_path, 'w', encoding='utf-8') as f:
                f.write(modified_content)
            
            logging.info(f"M3U8 file successfully modified for proxy playback: {m3u8_path}")
        except Exception as e:
            logging.error(f"Error modifying M3U8 file: {e}")
    
    def get_segment(self, path):
        """Fetch a segment with authentication parameters"""
        # Check if segment is in cache
        if path in self.cache:
            logging.info(f"Serving segment from cache: {path}")
            return self.cache[path]
        
        try:
            # Extract domain and path from the stream URL
            url_parts = urllib.parse.urlparse(self.stream_url)
            domain = f"{url_parts.scheme}://{url_parts.netloc}"
            
            # Extract base path (up to the directory containing the manifest file)
            base_path = os.path.dirname(url_parts.path)
            
            # Construct full URL
            full_url = f"{domain}{base_path}/{path}"
            authenticated_url = self.add_auth_params_to_url(full_url)
            
            logging.info(f"Proxy: Request for {path}")
            logging.info(f"Redirecting to URL: {authenticated_url}")
            
            response = requests.get(authenticated_url)
            response.raise_for_status()
            
            # Cache the response (only for small segments to avoid memory issues)
            if len(response.content) < 10 * 1024 * 1024:  # Less than 10MB
                self.cache[path] = {
                    'content': response.content,
                    'content_type': response.headers.get('Content-Type', 'application/octet-stream')
                }
            
            return {
                'content': response.content,
                'content_type': response.headers.get('Content-Type', 'application/octet-stream')
            }
        except Exception as e:
            logging.error(f"Error fetching segment: {e}")
            return None

    def refresh_manifest(self):
        """Refresh the manifest for live streams"""
        if not self.is_m3u8:
            # Only M3U8 live streams need refreshing
            return
            
        try:
            response = requests.get(self.stream_url)
            response.raise_for_status()
            
            # Save M3U8 file
            manifest_path = os.path.join(self.download_folder, "playlist.m3u8")
            with open(manifest_path, 'wb') as f:
                f.write(response.content)
            
            # Store the content
            self.parsed_manifest = response.text
            
            # Modify M3U8 for proxy playback
            self.modify_m3u8_for_proxy(manifest_path)
            
            logging.info(f"Successfully refreshed M3U8 manifest: {self.stream_id}")
            return True
        except Exception as e:
            logging.error(f"Error refreshing M3U8 manifest: {e}")
            return False


def create_jwt_token(stream_url):
    """Create a JWT token for a video stream"""
    stream_id = str(uuid.uuid4())
    payload = {
        'stream_id': stream_id,
        'stream_url': stream_url,
        'exp': int(time.time()) + JWT_EXPIRY
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm='HS256')
    return token, stream_id


def validate_jwt_token(token):
    """Validate a JWT token and return the payload"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


@app.route('/api/create_stream', methods=['POST'])
def create_stream():
    """Create a new stream from a manifest URL (MPD or M3U8)"""
    data = request.get_json()
    
    if not data or 'stream_url' not in data:
        return jsonify({'error': 'Stream URL is required'}), 400
    
    stream_url = data['stream_url']
    is_live = data.get('is_live', False)
    
    # Validate URL format (must end with .mpd or .m3u8)
    if not (stream_url.lower().endswith('.mpd') or stream_url.lower().endswith('.m3u8')):
        return jsonify({'error': 'URL must be an MPD or M3U8 manifest'}), 400
    
    # Create JWT token
    token, stream_id = create_jwt_token(stream_url)
    
    # Initialize Stream player
    player = StreamPlayerWithAuth(stream_url, stream_id)
    
    # Fetch and parse manifest
    if not player.fetch_manifest():
        return jsonify({'error': 'Failed to fetch manifest file'}), 500
    
    # Store player in active streams
    active_streams[stream_id] = {
        'player': player,
        'is_live': is_live,
        'last_access': int(time.time())
    }
    
    # Return stream information
    manifest_path = "manifest.mpd" if stream_url.lower().endswith('.mpd') else "playlist.m3u8"
    
    return jsonify({
        'token': token,
        'stream_id': stream_id,
        'manifest_url': f"/api/stream/{token}/{manifest_path}",
        'expires_at': int(time.time()) + JWT_EXPIRY,
        'is_live': is_live,
        'format': 'DASH' if stream_url.lower().endswith('.mpd') else 'HLS'
    })


@app.route('/api/stream/<token>/manifest.mpd', methods=['GET'])
def get_mpd_manifest(token):
    """Serve the MPD manifest file for a stream"""
    payload = validate_jwt_token(token)
    
    if not payload:
        return jsonify({'error': 'Invalid or expired token'}), 401
    
    stream_id = payload['stream_id']
    
    if stream_id not in active_streams:
        # Re-initialize the player if it's not in active streams
        player = StreamPlayerWithAuth(payload['stream_url'], stream_id)
        if not player.fetch_manifest():
            return jsonify({'error': 'Failed to fetch MPD file'}), 500
        active_streams[stream_id] = {
            'player': player,
            'is_live': False,
            'last_access': int(time.time())
        }
    
    # Update last access time
    active_streams[stream_id]['last_access'] = int(time.time())
    player = active_streams[stream_id]['player']
    
    try:
        with open(os.path.join(player.download_folder, "manifest.mpd"), 'rb') as f:
            content = f.read()
        return Response(content, mimetype='application/dash+xml')
    except Exception as e:
        logging.error(f"Error serving MPD file: {e}")
        return jsonify({'error': 'Failed to serve MPD file'}), 500


@app.route('/api/stream/<token>/playlist.m3u8', methods=['GET'])
def get_m3u8_manifest(token):
    """Serve the M3U8 manifest file for a stream"""
    payload = validate_jwt_token(token)
    
    if not payload:
        return jsonify({'error': 'Invalid or expired token'}), 401
    
    stream_id = payload['stream_id']
    
    if stream_id not in active_streams:
        # Re-initialize the player if it's not in active streams
        player = StreamPlayerWithAuth(payload['stream_url'], stream_id)
        if not player.fetch_manifest():
            return jsonify({'error': 'Failed to fetch M3U8 file'}), 500
        active_streams[stream_id] = {
            'player': player,
            'is_live': False,
            'last_access': int(time.time())
        }
    
    # Update last access time
    active_streams[stream_id]['last_access'] = int(time.time())
    stream_data = active_streams[stream_id]
    player = stream_data['player']
    
    # For live streams, refresh the manifest
    if stream_data['is_live']:
        player.refresh_manifest()
    
    try:
        with open(os.path.join(player.download_folder, "playlist.m3u8"), 'rb') as f:
            content = f.read()
        return Response(content, mimetype='application/vnd.apple.mpegurl')
    except Exception as e:
        logging.error(f"Error serving M3U8 file: {e}")
        return jsonify({'error': 'Failed to serve M3U8 file'}), 500


@app.route('/api/stream/<token>/<path:segment_path>', methods=['GET'])
def get_segment(token, segment_path):
    """Serve a segment file for a stream"""
    payload = validate_jwt_token(token)
    
    if not payload:
        return jsonify({'error': 'Invalid or expired token'}), 401
    
    stream_id = payload['stream_id']
    
    if stream_id not in active_streams:
        # Re-initialize the player if it's not in active streams
        player = StreamPlayerWithAuth(payload['stream_url'], stream_id)
        if not player.fetch_manifest():
            return jsonify({'error': 'Failed to fetch manifest file'}), 500
        active_streams[stream_id] = {
            'player': player,
            'is_live': False,
            'last_access': int(time.time())
        }
    
    # Update last access time
    active_streams[stream_id]['last_access'] = int(time.time())
    player = active_streams[stream_id]['player']
    
    # Get segment from the player
    segment = player.get_segment(segment_path)
    
    if segment:
        return Response(segment['content'], mimetype=segment['content_type'])
    else:
        return jsonify({'error': 'Failed to fetch segment'}), 500


@app.route('/api/info/<token>', methods=['GET'])
def get_stream_info(token):
    """Get information about a stream"""
    payload = validate_jwt_token(token)
    
    if not payload:
        return jsonify({'error': 'Invalid or expired token'}), 401
    
    stream_id = payload['stream_id']
    
    if stream_id not in active_streams:
        return jsonify({'error': 'Stream not found'}), 404
    
    stream_data = active_streams[stream_id]
    player = stream_data['player']
    
    # Determine manifest path based on format
    manifest_path = "manifest.mpd" if player.is_mpd else "playlist.m3u8"
    
    return jsonify({
        'stream_id': stream_id,
        'stream_url': payload['stream_url'],
        'manifest_url': f"/api/stream/{token}/{manifest_path}",
        'expires_at': payload['exp'],
        'is_live': stream_data['is_live'],
        'format': 'DASH' if player.is_mpd else 'HLS'
    })


# Refresh live M3U8 playlists periodically
def refresh_live_streams():
    """Refresh M3U8 playlists for live streams"""
    while True:
        for stream_id, stream_data in active_streams.items():
            # Only refresh M3U8 live streams that have been accessed recently
            current_time = int(time.time())
            if (stream_data['is_live'] and 
                stream_data['player'].is_m3u8 and 
                current_time - stream_data['last_access'] < 300):  # 5 minutes
                stream_data['player'].refresh_manifest()
        
        # Sleep for 10 seconds before next refresh
        time.sleep(10)


# Clean up inactive streams periodically
def cleanup_inactive_streams():
    """Remove expired streams from memory"""
    while True:
        current_time = int(time.time())
        streams_to_remove = []
        
        for stream_id, stream_data in active_streams.items():
            # Check if stream has been inactive for more than 1 hour
            if current_time - stream_data['last_access'] > 3600:  # 1 hour
                streams_to_remove.append(stream_id)
        
        # Remove inactive streams
        for stream_id in streams_to_remove:
            try:
                import shutil
                shutil.rmtree(active_streams[stream_id]['player'].download_folder)
                del active_streams[stream_id]
                logging.info(f"Removed inactive stream: {stream_id}")
            except Exception as e:
                logging.error(f"Error removing stream {stream_id}: {e}")
        
        # Sleep for 1 hour
        time.sleep(3600)


# Start background threads
cleanup_thread = threading.Thread(target=cleanup_inactive_streams)
cleanup_thread.daemon = True
cleanup_thread.start()

refresh_thread = threading.Thread(target=refresh_live_streams)
refresh_thread.daemon = True
refresh_thread.start()


# Simple frontend for testing
@app.route('/')
def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Streaming API</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            h1 { color: #333; }
            form { margin-bottom: 20px; }
            label { display: block; margin-bottom: 5px; }
            input[type="text"] { width: 100%; padding: 8px; margin-bottom: 10px; }
            button { padding: 10px 15px; background: #4CAF50; color: white; border: none; cursor: pointer; }
            .checkbox-container { margin-bottom: 10px; }
            pre { background: #f4f4f4; padding: 10px; overflow: auto; }
            #result { margin-top: 20px; }
            .tab { margin-right: 20px; padding: 10px; display: inline-block; background: #ddd; cursor: pointer; }
            .tab.active { background: #4CAF50; color: white; }
            .player-container { margin-top: 20px; }
        </style>
    </head>
    <body>
        <h1>Streaming API</h1>
        <form id="streamForm">
            <label for="stream_url">Stream URL (MPD or M3U8):</label>
            <input type="text" id="stream_url" name="stream_url" placeholder="Enter Stream URL (.mpd or .m3u8)" required>
            
            <div class="checkbox-container">
                <input type="checkbox" id="is_live" name="is_live">
                <label for="is_live">This is a live stream (for M3U8 only)</label>
            </div>
            
            <button type="submit">Create Stream</button>
        </form>
        <div id="result"></div>

        <script>
            document.getElementById('streamForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const stream_url = document.getElementById('stream_url').value;
                const is_live = document.getElementById('is_live').checked;
                
                try {
                    const response = await fetch('/api/create_stream', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ stream_url, is_live })
                    });
                    
                    const data = await response.json();
                    const resultDiv = document.getElementById('result');
                    
                    if (response.ok) {
                        let playerHtml = '';
                        
                        if (data.format === 'DASH') {
                            playerHtml = `
                                <div class="player-container">
                                    <video id="videoPlayer" controls style="width: 100%;">
                                        <source src="${data.manifest_url}" type="application/dash+xml">
                                        Your browser does not support the video tag.
                                    </video>
                                    <script src="https://cdn.dashjs.org/latest/dash.all.min.js"><\/script>
                                    <script>
                                        const player = dashjs.MediaPlayer().create();
                                        player.initialize(document.querySelector("#videoPlayer"), "${data.manifest_url}", true);
                                    <\/script>
                                </div>
                            `;
                        } else {
                            playerHtml = `
                                <div class="player-container">
                                    <video id="videoPlayer" controls style="width: 100%;"></video>
                                    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"><\/script>
                                    <script>
                                        const video = document.getElementById('videoPlayer');
                                        if(Hls.isSupported()) {
                                            const hls = new Hls();
                                            hls.loadSource('${data.manifest_url}');
                                            hls.attachMedia(video);
                                            hls.on(Hls.Events.MANIFEST_PARSED, function() {
                                                video.play();
                                            });
                                        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                                            video.src = '${data.manifest_url}';
                                            video.addEventListener('loadedmetadata', function() {
                                                video.play();
                                            });
                                        }
                                    <\/script>
                                </div>
                            `;
                        }
                        
                        resultDiv.innerHTML = `
                            <h2>Stream Created</h2>
                            <p>Token: ${data.token}</p>
                            <p>Stream ID: ${data.stream_id}</p>
                            <p>Format: ${data.format}</p>
                            <p>Live Stream: ${data.is_live ? 'Yes' : 'No'}</p>
                            <p>Manifest URL: <a href="${data.manifest_url}" target="_blank">${data.manifest_url}</a></p>
                            <p>Use this URL in your ${data.format} player.</p>
                            <h3>Player Test</h3>
                            ${playerHtml}
                        `;
                    } else {
                        resultDiv.innerHTML = `<h2>Error</h2><pre>${JSON.stringify(data, null, 2)}</pre>`;
                    }
                } catch (error) {
                    document.getElementById('result').innerHTML = `<h2>Error</h2><pre>${error}</pre>`;
                }
            });
        </script>
    </body>
    </html>
    """


if __name__ == "__main__":
    # Create temp directory if it doesn't exist
    if not os.path.exists("temp_videos"):
        os.makedirs("temp_videos")
    
    # For production, use gunicorn or another WSGI server
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
