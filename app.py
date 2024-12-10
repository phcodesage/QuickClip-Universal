from flask import Flask, request, send_from_directory, render_template, jsonify, abort, Response
from flask_cors import CORS
import os
import subprocess
import json
from werkzeug.utils import safe_join
import shlex
from datetime import datetime
from pytube import YouTube
import traceback
import sys
import time

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# Ensure absolute path for downloads
DOWNLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'downloads'))
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Check for required system dependencies
def check_dependencies():
    try:
        # First check for yt-dlp as it's the primary tool
        possible_paths = [
            os.path.join(os.path.dirname(sys.executable), 'bin'),
            '/usr/bin',
            '/usr/local/bin',
            '/bin',
            os.path.expanduser('~/.local/bin')
        ]
        
        # Update PATH environment variable
        os.environ["PATH"] = os.pathsep.join(possible_paths + os.environ.get("PATH", "").split(os.pathsep))
        
        # Check yt-dlp with full path
        yt_dlp_path = None
        
        # First check virtualenv path for yt-dlp
        venv_yt_dlp = os.path.join(os.path.dirname(sys.executable), 'yt-dlp')
        if os.path.exists(venv_yt_dlp):
            yt_dlp_path = venv_yt_dlp
        else:
            # Check other paths
            for path in possible_paths:
                if os.path.exists(os.path.join(path, 'yt-dlp')):
                    yt_dlp_path = os.path.join(path, 'yt-dlp')
                    break
        
        if not yt_dlp_path:
            print("yt-dlp not found")
            return False
            
        # Test yt-dlp
        try:
            yt_dlp_result = subprocess.run([yt_dlp_path, '--version'], 
                                         capture_output=True, 
                                         text=True,
                                         check=True)
            print(f"Found yt-dlp version: {yt_dlp_result.stdout.strip()}")
            
            # Store the path for later use
            app.config['YT_DLP_PATH'] = yt_dlp_path
            
            # Try to find ffmpeg but don't fail if not found
            for path in possible_paths:
                if os.path.exists(os.path.join(path, 'ffmpeg')):
                    ffmpeg_path = os.path.join(path, 'ffmpeg')
                    try:
                        subprocess.run([ffmpeg_path, '-version'], 
                                     capture_output=True, 
                                     text=True,
                                     check=True)
                        app.config['FFMPEG_PATH'] = ffmpeg_path
                        print(f"Found ffmpeg at: {ffmpeg_path}")
                        break
                    except:
                        print("ffmpeg found but not working, will use pytube fallback")
            
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Error testing yt-dlp: {str(e)}")
            print(f"Output: {e.output}")
            return False
        
    except Exception as e:
        print(f"Error checking dependencies: {str(e)}")
        print("Error: Required dependencies not installed or not accessible.")
        return False

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/download', methods=['POST'])
def download_video():
    if not check_dependencies():
        return jsonify({'error': 'Missing system dependencies'}), 500

    try:
        data = request.get_json()
        url = data['url']
        format = data['format']

        if format not in ['mp4', 'wav']:
            return jsonify({'error': 'Unsupported format'}), 400

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        
        try:
            # Try yt-dlp first (preferred method)
            env = os.environ.copy()
            
            # Get video info using full path
            video_info_command = [app.config['YT_DLP_PATH'], '--skip-download', '--print-json', url]
            video_info_result = subprocess.run(
                video_info_command,
                env=env,
                text=True,
                capture_output=True,
                check=True
            )
            video_info = json.loads(video_info_result.stdout)
            video_id = video_info.get('id', 'download')
            
            output_filename = f"{video_id}_{timestamp}.{format}"
            output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)
            
            if format == 'mp4':
                download_command = [
                    app.config['YT_DLP_PATH'],
                    '-f', 'best',
                    '-o', output_path,
                    '--no-playlist',  # Avoid downloading playlists
                    url
                ]
            else:  # wav
                download_command = [
                    app.config['YT_DLP_PATH'],
                    '--extract-audio',
                    '--audio-format', 'wav',
                    '-o', output_path,
                    '--no-playlist',
                    url
                ]
            
            subprocess.run(download_command, env=env, check=True)
            
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"yt-dlp failed, trying pytube: {str(e)}")
            # Fallback to pytube
            yt = YouTube(url)
            if format == 'mp4':
                stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
                output_filename = f"{yt.video_id}_{timestamp}.mp4"
            else:  # wav
                stream = yt.streams.filter(only_audio=True).first()
                output_filename = f"{yt.video_id}_{timestamp}.wav"
            
            output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)
            stream.download(output_path=DOWNLOAD_FOLDER, filename=output_filename)

        print(f"File ready at {output_path}")
        
        if not os.path.exists(output_path):
            raise FileNotFoundError(f"Download failed: File not found at {output_path}")

        response = send_from_directory(directory=DOWNLOAD_FOLDER, path=output_filename, as_attachment=True)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        
        # Clean up the file after sending
        @response.call_on_close
        def cleanup():
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception as e:
                print(f"Cleanup error: {e}")
        
        return response

    except Exception as e:
        error_msg = f"Error: {str(e)}\nTraceback: {traceback.format_exc()}"
        print(error_msg)
        return jsonify({'error': str(e)}), 500

def extract_video_id(url):
    """Extract video ID from YouTube URL."""
    if 'youtu.be' in url:
        return url.split('/')[-1]
    elif 'youtube.com' in url:
        from urllib.parse import parse_qs, urlparse
        parsed_url = urlparse(url)
        if 'shorts' in parsed_url.path:
            return parsed_url.path.split('/')[-1]
        return parse_qs(parsed_url.query)['v'][0]
    return url

if __name__ == '__main__':
    if not check_dependencies():
        sys.exit(1)
    app.run(debug=True, port=5000)
