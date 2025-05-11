from flask import Flask, request, Response, jsonify, redirect
import requests
from flask_cors import CORS
import jwt
import json
import urllib.parse
import os
import time
import logging
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

class HLSPlayerWithAuth:
    def __init__(self, m3u8_url, stream_id):
        self.m3u8_url = m3u8_url
        self.stream_id = stream_id
        self.base_url = self.extract_base_url(m3u8_url)
        self.query_params = self.extract_query_params(m3u8_url)
        self.download_folder = f"temp_hls/{stream_id}"
        self.cache = {}  # Cache for segment responses
        
        # Create directory if it doesn't exist
        if not os.path.exists(self.download_folder):
            os.makedirs(self.download_folder)

    def extract_base_url(self, url):
        # Extract base part from URL (without query parameters)
        parsed_url = urllib.parse.urlparse(url)
        path_parts = parsed_url.path.split('/')
        if path_parts and path_parts[-1]:
            # Remove the filename from the path
            path = '/'.join(path_parts[:-1]) + '/'
        else:
            path = parsed_url.path
        return f"{parsed_url.scheme}://{parsed_url.netloc}{path}"
    
    def extract_query_params(self, url):
        # Extract query parameters from URL
        if '?' not in url:
            return {}
        query_string = url.split('?', 1)[1]
        params = {}
        for param in query_string.split('&'):
            if '=' in param:
                key, value = param.split('=', 1)
                params[key] = value
        return params
    
    def add_auth_params_to_url(self, url):
        # Add authentication parameters to URL
        if '?' in url:
            base_url = url.split('?')[0]
        else:
            base_url = url
            
        # URL encode parameters as they might contain special characters
        query_params = urllib.parse.urlencode(self.query_params)
        return f"{base_url}?{query_params}"
    
    def fetch_m3u8(self):
        """Download and process M3U8 playlist file"""
        logging.info(f"Downloading manifest from M3U8 URL: {self.m3u8_url}")
        
        try:
            response = requests.get(self.m3u8_url)
            response.raise_for_status()
            
            # Save original M3U8 file
            original_m3u8_path = os.path.join(self.download_folder, "original.m3u8")
            with open(original_m3u8_path, 'wb') as f:
                f.write(response.content)
            
            # Create modified M3U8 for proxy playback
            self.modify_m3u8_for_proxy(response.content.decode('utf-8'))
            
            return True
        except Exception as e:
            logging.error(f"Error downloading M3U8 file: {e}")
            return False
    
    def modify_m3u8_for_proxy(self, content):
        """Modify M3U8 file for proxy playback"""
        modified_content = content
        
        # Get the filename from the original URL
        m3u8_filename = self.m3u8_url.split('/')[-1].split('?')[0]
        
        try:
            # Process line by line
            lines = content.splitlines()
            modified_lines = []
            
            for line in lines:
                if line.startswith('#'):
                    # Handle directives with URLs
                    if '#EXT-X-STREAM-INF' in line:
                        modified_lines.append(line)
                    elif '#EXT-X-KEY' in line and 'URI=' in line:
                        # Replace encryption key URLs
                        pattern = r'URI="([^"]+)"'
                        match = re.search(pattern, line)
                        if match:
                            original_url = match.group(1)
                            # Handle relative or absolute URLs
                            if not original_url.startswith('http'):
                                if original_url.startswith('/'):
                                    # Absolute path from domain root
                                    domain = '/'.join(self.base_url.split('/')[:3])  # http(s)://domain.com
                                    full_url = domain + original_url
                                else:
                                    # Relative path
                                    full_url = self.base_url + original_url
                            else:
                                full_url = original_url
                                
                            proxy_url = f"/api/stream/{self.stream_id}/{original_url.split('/')[-1]}"
                            modified_line = line.replace(f'URI="{original_url}"', f'URI="{proxy_url}"')
                            modified_lines.append(modified_line)
                        else:
                            modified_lines.append(line)
                    else:
                        modified_lines.append(line)
                elif line.strip() and not line.startswith('#'):
                    # Handle media playlist or segment URLs
                    if line.endswith('.m3u8') or '.m3u8?' in line:
                        # This is a variant playlist
                        if line.startswith('http'):
                            # Absolute URL
                            playlist_name = line.split('/')[-1].split('?')[0]
                        else:
                            # Relative URL
                            playlist_name = line.split('?')[0]
                            
                        proxy_url = f"/api/stream/{self.stream_id}/{playlist_name}"
                        modified_lines.append(proxy_url)
                    elif line.endswith('.ts') or '.ts?' in line or any(ext in line for ext in ['.aac', '.mp4', '.vtt', '.webvtt']):
                        # This is a media segment
                        if line.startswith('http'):
                            # Absolute URL
                            segment_name = line.split('/')[-1].split('?')[0]
                        else:
                            # Relative URL
                            segment_name = line
                            
                        proxy_url = f"/api/stream/{self.stream_id}/{segment_name}"
                        modified_lines.append(proxy_url)
                    else:
                        # Keep other lines unchanged
                        modified_lines.append(line)
                else:
                    # Empty lines or comments
                    modified_lines.append(line)
            
            # Save modified M3U8
            modified_m3u8_path = os.path.join(self.download_folder, "manifest.m3u8")
            with open(modified_m3u8_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(modified_lines))
            
            logging.info(f"M3U8 file successfully modified for proxy playback: {modified_m3u8_path}")
        except Exception as e:
            logging.error(f"Error modifying M3U8 file: {e}")
    
    def get_m3u8_content(self, path=None):
        """Get the modified M3U8 content"""
        try:
            if path:
                # This is a sub-playlist
                # Download the sub-playlist and modify it
                if path.startswith('http'):
                    sub_url = path
                else:
                    sub_url = f"{self.base_url}{path}"
                
                if '?' not in sub_url and self.query_params:
                    sub_url = self.add_auth_params_to_url(sub_url)
                
                logging.info(f"Downloading sub-playlist: {sub_url}")
                response = requests.get(sub_url)
                response.raise_for_status()
                
                # Save original sub-playlist
                sub_filename = path.split('/')[-1].split('?')[0]
                sub_path = os.path.join(self.download_folder, sub_filename)
                with open(sub_path, 'wb') as f:
                    f.write(response.content)
                
                # Modify the sub-playlist
                self.modify_m3u8_for_proxy(response.content.decode('utf-8'))
                
                # Return the modified sub-playlist
                modified_path = os.path.join(self.download_folder, "manifest.m3u8")
                with open(modified_path, 'rb') as f:
                    return f.read()
            else:
                # Return the main playlist
                manifest_path = os.path.join(self.download_folder, "manifest.m3u8")
                with open(manifest_path, 'rb') as f:
                    return f.read()
        except Exception as e:
            logging.error(f"Error getting M3U8 content: {e}")
            return None
    
    def get_hls_key(self, key_url, authorization):
        """Fetch and process HLS encryption key"""
        try:
            headers = {}
            if authorization:
                headers['Authorization'] = authorization
            
            response = requests.get(key_url, headers=headers)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logging.error(f"Error fetching HLS key: {e}")
            return None

    def get_segment(self, path):
        """Fetch a segment with authentication parameters"""
        # Check if segment is in cache
        if path in self.cache:
            logging.info(f"Serving segment from cache: {path}")
            return self.cache[path]
        
        try:
            # Handle different types of paths
            if path.startswith('http'):
                # Absolute URL
                full_url = path
            elif path.startswith('/'):
                # Absolute path from domain root
                domain = '/'.join(self.base_url.split('/')[:3])  # http(s)://domain.com
                full_url = domain + path
            else:
                # Relative path
                full_url = self.base_url + path
            
            # Add auth parameters if needed
            if '?' not in full_url and self.query_params:
                authenticated_url = self.add_auth_params_to_url(full_url)
            else:
                authenticated_url = full_url
            
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


def create_jwt_token(m3u8_url):
    """Create a JWT token for a video stream"""
    stream_id = str(uuid.uuid4())
    payload = {
        'stream_id': stream_id,
        'm3u8_url': m3u8_url,
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
    """Create a new stream from an M3U8 URL"""
    data = request.get_json()
    
    if not data or 'm3u8_url' not in data:
        return jsonify({'error': 'M3U8 URL is required'}), 400
    
    m3u8_url = data['m3u8_url']
    
    # Create JWT token
    token, stream_id = create_jwt_token(m3u8_url)
    
    # Initialize HLS player
    player = HLSPlayerWithAuth(m3u8_url, stream_id)
    
    # Fetch and parse M3U8
    if not player.fetch_m3u8():
        return jsonify({'error': 'Failed to fetch M3U8 file'}), 500
    
    # Store player in active streams
    active_streams[stream_id] = player
    
    # Return stream information
    return jsonify({
        'token': token,
        'stream_id': stream_id,
        'manifest_url': f"/api/stream/{token}/manifest.m3u8",
        'expires_at': int(time.time()) + JWT_EXPIRY
    })


@app.route('/api/stream/<token>/manifest.m3u8', methods=['GET'])
def get_manifest(token):
    """Serve the M3U8 manifest file for a stream"""
    payload = validate_jwt_token(token)
    
    if not payload:
        return jsonify({'error': 'Invalid or expired token'}), 401
    
    stream_id = payload['stream_id']
    
    if stream_id not in active_streams:
        # Re-initialize the player if it's not in active streams
        player = HLSPlayerWithAuth(payload['m3u8_url'], stream_id)
        if not player.fetch_m3u8():
            return jsonify({'error': 'Failed to fetch M3U8 file'}), 500
        active_streams[stream_id] = player
    
    player = active_streams[stream_id]
    
    # Serve the M3U8 content
    content = player.get_m3u8_content()
    if content:
        return Response(content, mimetype='application/vnd.apple.mpegurl')
    else:
        return jsonify({'error': 'Failed to serve M3U8 file'}), 500


@app.route('/api/stream/<stream_id>/<path:segment_path>', methods=['GET'])
def get_segment_or_playlist(stream_id, segment_path):
    """Serve a segment or sub-playlist file for a stream"""
    if stream_id not in active_streams:
        return jsonify({'error': 'Stream not found'}), 404
    
    player = active_streams[stream_id]
    
    # Handle HLS key requests
    if 'get-hls-key' in segment_path:
        # Extract videoKey from the request URL
        video_key = request.args.get('videoKey')
        if not video_key:
            return jsonify({'error': 'VideoKey is required'}), 400
            
        # Use the fixed authorization token for penpencil API
        auth_token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3NDc0ODY3OTMuNTY4LCJkYXRhIjp7Il9pZCI6IjY2NjQwNDYyYzg0NzgzZDM4NjMxMjMzNyIsInVzZXJuYW1lIjoiOTY2ODQyNTI1NyIsImZpcnN0TmFtZSI6IlB1bmFtIFByYWhhcmFqIiwibGFzdE5hbWUiOiJUcmlwYXRoeSAiLCJvcmdhbml6YXRpb24iOnsiX2lkIjoiNWViMzkzZWU5NWZhYjc0NjhhNzlkMTg5Iiwid2Vic2l0ZSI6InBoeXNpY3N3YWxsYWguY29tIiwibmFtZSI6IlBoeXNpY3N3YWxsYWgifSwiZW1haWwiOiJwdW5hbXByYWhhcmFqQGdtYWlsLmNvbSIsInJvbGVzIjpbIjViMjdiZDk2NTg0MmY5NTBhNzc4YzZlZiJdLCJjb3VudHJ5R3JvdXAiOiJJTiIsInR5cGUiOiJVU0VSIn0sImlhdCI6MTc0Njg4MTk5M30.nxRAMtZWVLp_9IWgygfh4ID7N0ipx6PX7sLniC9SL3A"
        
        # Construct the penpencil API URL with authorization parameter
        key_url = f"https://api.penpencil.co/v1/videos/get-hls-key?videoKey={video_key}&key=enc.key&authorization={auth_token}"
        
        try:
            # Make request to penpencil API
            headers = {
                'Authorization': f'Bearer {auth_token}',
                'Content-Type': 'application/json'
            }
            response = requests.get(key_url, headers=headers)
            response.raise_for_status()
            
            # सीधे बाइनरी डेटा के रूप में रिस्पांस को प्रोसेस करें
            if response.content:
                return Response(response.content, mimetype='application/octet-stream')
            else:
                return jsonify({'error': 'Empty response from key server'}), 500
                
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching encryption key: {str(e)}")
            return jsonify({'error': 'Failed to fetch encryption key'}), 500
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding key response: {str(e)}")
            return jsonify({'error': 'Invalid response from key server'}), 500
    
    # Check if this is an M3U8 file (sub-playlist)
    if segment_path.endswith('.m3u8'):
        content = player.get_m3u8_content(segment_path)
        if content:
            return Response(content, mimetype='application/vnd.apple.mpegurl')
        else:
            return jsonify({'error': 'Failed to serve sub-playlist'}), 500
    else:
        # This is a media segment
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
    
    return jsonify({
        'stream_id': stream_id,
        'm3u8_url': payload['m3u8_url'],
        'manifest_url': f"/api/stream/{token}/manifest.m3u8",
        'expires_at': payload['exp']
    })


# Clean up inactive streams periodically
def cleanup_inactive_streams():
    """Remove expired streams from memory"""
    while True:
        current_time = int(time.time())
        streams_to_remove = []
        
        for stream_id, player in active_streams.items():
            # Check if stream has been inactive for more than 1 hour
            if os.path.exists(player.download_folder):
                manifest_path = os.path.join(player.download_folder, "manifest.m3u8")
                if os.path.exists(manifest_path):
                    mod_time = os.path.getmtime(manifest_path)
                    if current_time - mod_time > 3600:  # 1 hour
                        streams_to_remove.append(stream_id)
        
        # Remove inactive streams
        for stream_id in streams_to_remove:
            try:
                import shutil
                shutil.rmtree(active_streams[stream_id].download_folder)
                del active_streams[stream_id]
                logging.info(f"Removed inactive stream: {stream_id}")
            except Exception as e:
                logging.error(f"Error removing stream {stream_id}: {e}")
        
        # Sleep for 1 hour
        time.sleep(3600)


# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_inactive_streams)
cleanup_thread.daemon = True
cleanup_thread.start()


# Direct access URL processor - for simpler use cases
@app.route('/process_m3u8', methods=['GET'])
def process_m3u8():
    # Get the M3U8 URL from the request parameters
    m3u8_url = request.args.get('url')
    if not m3u8_url:
        return "Please provide an M3U8 URL as a 'url' query parameter", 400
    
    try:
        # Parse the URL and extract query parameters
        parsed_url = urllib.parse.urlparse(m3u8_url)
        base_url = urlunparse(parsed_url._replace(query=''))
        base_path = '/'.join(base_url.split('/')[:-1]) + '/'
        query_params = dict(parse_qsl(parsed_url.query))
        
        # Get the M3U8 content
        response = requests.get(m3u8_url)
        if response.status_code != 200:
            return f"Failed to fetch M3U8 file: {response.status_code}", 500
        
        content = response.text
        
        # Process the M3U8 content to add parameters to segment URLs
        def replace_url(match):
            segment_url = match.group(1)
            # Skip URLs that already have parameters or are absolute URLs
            if '?' in segment_url or segment_url.startswith('http'):
                return match.group(0)
            
            # Create the full URL for the segment
            if segment_url.startswith('/'):
                # Handle absolute path
                domain = '/'.join(base_url.split('/')[:3])  # http(s)://domain.com
                full_url = domain + segment_url
            else:
                # Handle relative path
                full_url = base_path + segment_url
                
            # Add the parameters
            if '?' in full_url:
                full_url += '&' + urllib.parse.urlencode(query_params)
            else:
                full_url += '?' + urllib.parse.urlencode(query_params)
                
            return match.group(0).replace(segment_url, full_url)
        
        # Find lines that are not comments and likely contain segment URLs
        # Match both TS files and other media segments
        processed_content = re.sub(r'([^\s#][^\s]*\.ts)', replace_url, content)
        
        # Handle sub-playlists in master playlist
        processed_content = re.sub(r'([^\s#][^\s]*\.m3u8)', replace_url, content)
        
        # Return the processed M3U8 content as a response
        return Response(processed_content, mimetype='application/vnd.apple.mpegurl')
        
    except Exception as e:
        return f"Error processing M3U8 file: {str(e)}", 500


# Simple frontend for testing
@app.route('/')
def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>HLS Stream API</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            h1 { color: #333; }
            .tab { overflow: hidden; border: 1px solid #ccc; background-color: #f1f1f1; }
            .tab button { background-color: inherit; float: left; border: none; outline: none; cursor: pointer; padding: 14px 16px; transition: 0.3s; }
            .tab button:hover { background-color: #ddd; }
            .tab button.active { background-color: #ccc; }
            .tabcontent { display: none; padding: 20px; border: 1px solid #ccc; border-top: none; }
            form { margin-bottom: 20px; }
            label { display: block; margin-bottom: 5px; }
            input[type="text"] { width: 100%; padding: 8px; margin-bottom: 10px; }
            button { padding: 10px 15px; background: #4CAF50; color: white; border: none; cursor: pointer; }
            pre { background: #f4f4f4; padding: 10px; overflow: auto; }
            .result { margin-top: 20px; }
        </style>
    </head>
    <body>
        <h1>HLS Stream API</h1>
        
        <div class="tab">
            <button class="tablinks active" onclick="openTab(event, 'AdvancedMode')">Advanced Mode</button>
            <button class="tablinks" onclick="openTab(event, 'SimpleMode')">Simple Mode</button>
        </div>
        
        <div id="AdvancedMode" class="tabcontent" style="display: block;">
            <h2>Create a Proxy Stream</h2>
            <p>This mode creates a protected stream that requires a token.</p>
            <form id="streamForm">
                <label for="m3u8_url">M3U8 URL:</label>
                <input type="text" id="m3u8_url" name="m3u8_url" placeholder="Enter M3U8 URL" required>
                <button type="submit">Create Stream</button>
            </form>
            <div id="advancedResult" class="result"></div>
        </div>
        
        <div id="SimpleMode" class="tabcontent">
            <h2>Process M3U8 URL</h2>
            <p>This mode generates a direct URL that adds parameters to all segments.</p>
            <form id="simpleForm">
                <label for="simple_m3u8_url">M3U8 URL:</label>
                <input type="text" id="simple_m3u8_url" name="simple_m3u8_url" placeholder="Enter M3U8 URL" required>
                <button type="submit">Process URL</button>
            </form>
            <div id="simpleResult" class="result"></div>
        </div>

        <script>
            function openTab(evt, tabName) {
                var i, tabcontent, tablinks;
                tabcontent = document.getElementsByClassName("tabcontent");
                for (i = 0; i < tabcontent.length; i++) {
                    tabcontent[i].style.display = "none";
                }
                tablinks = document.getElementsByClassName("tablinks");
                for (i = 0; i < tablinks.length; i++) {
                    tablinks[i].className = tablinks[i].className.replace(" active", "");
                }
                document.getElementById(tabName).style.display = "block";
                evt.currentTarget.className += " active";
            }
            
            document.getElementById('streamForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const m3u8_url = document.getElementById('m3u8_url').value;
                
                try {
                    const response = await fetch('/api/create_stream', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ m3u8_url })
                    });
                    
                    const data = await response.json();
                    const resultDiv = document.getElementById('advancedResult');
                    
                    if (response.ok) {
                        resultDiv.innerHTML = `
                            <h3>Stream Created</h3>
                            <p>Token: ${data.token}</p>
                            <p>Stream ID: ${data.stream_id}</p>
                            <p>Manifest URL: <a href="${data.manifest_url}" target="_blank">${data.manifest_url}</a></p>
                            <p>Use this URL in your HLS player.</p>
                            <h3>Player Test</h3>
                            <div id="player">
                                <video id="videoPlayer" controls style="width: 100%;">
                                    <source src="${data.manifest_url}" type="application/x-mpegURL">
                                    Your browser does not support the video tag.
                                </video>
                            </div>
                            <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"><\/script>
                            <script>
                                document.addEventListener('DOMContentLoaded', function() {
                                    const video = document.getElementById('videoPlayer');
                                    const manifestUrl = "${data.manifest_url}";
                                    
                                    if (Hls.isSupported()) {
                                        const hls = new Hls();
                                        hls.loadSource(manifestUrl);
                                        hls.attachMedia(video);
                                        hls.on(Hls.Events.MANIFEST_PARSED, function() {
                                            video.play();
                                        });
                                    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                                        video.src = manifestUrl;
                                        video.addEventListener('loadedmetadata', function() {
                                            video.play();
                                        });
                                    }
                                });
                            <\/script>
                        `;
                    } else {
                        resultDiv.innerHTML = `<h3>Error</h3><pre>${JSON.stringify(data, null, 2)}</pre>`;
                    }
                } catch (error) {
                    document.getElementById('advancedResult').innerHTML = `<h3>Error</h3><pre>${error}</pre>`;
                }
            });
            
            document.getElementById('simpleForm').addEventListener('submit', (e) => {
                e.preventDefault();
                const m3u8_url = document.getElementById('simple_m3u8_url').value;
                
                if (m3u8_url) {
                    const encodedUrl = encodeURIComponent(m3u8_url);
                    const processingUrl = `/process_m3u8?url=${encodedUrl}`;
                    
                    document.getElementById('simpleResult').innerHTML = `
                        <h3>Processed URL</h3>
                        <p>Use this URL in your HLS player:</p>
                        <pre>${window.location.origin}${processingUrl}</pre>
                        <p><a href="${processingUrl}" target="_blank">Test in new tab</a></p>
                        <h3>Player Test</h3>
                        <div id="simplePlayer">
                            <video id="simpleVideoPlayer" controls style="width: 100%;">
                                <source src="${processingUrl}" type="application/x-mpegURL">
                                Your browser does not support the video tag.
                            </video>
                        </div>
                        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"><\/script>
                        <script>
                            document.addEventListener('DOMContentLoaded', function() {
                                const video = document.getElementById('simpleVideoPlayer');
                                const manifestUrl = "${processingUrl}";
                                
                                if (Hls.isSupported()) {
                                    const hls = new Hls();
                                    hls.loadSource(manifestUrl);
                                    hls.attachMedia(video);
                                    hls.on(Hls.Events.MANIFEST_PARSED, function() {
                                        video.play();
                                    });
                                } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                                    video.src = manifestUrl;
                                    video.addEventListener('loadedmetadata', function() {
                                        video.play();
                                    });
                                }
                            });
                        <\/script>
                    `;
                } else {
                    alert('Please enter an M3U8 URL');
                }
            });
        </script>
    </body>
    </html>
    """


if __name__ == "__main__":
    # Create temp directory if it doesn't exist
    if not os.path.exists("temp_hls"):
        os.makedirs("temp_hls")
    
    # For production, use gunicorn or another WSGI server
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
