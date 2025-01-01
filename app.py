from flask import Flask, request, send_from_directory, render_template, jsonify, abort, Response
from flask_cors import CORS
import os
import json
from werkzeug.utils import safe_join
from datetime import datetime
import traceback
import sys
import time
import subprocess
import shutil
import logging
from flask_socketio import SocketIO, emit
import re

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Ensure absolute path for downloads
DOWNLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'downloads'))
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Set up logging configuration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('download.log')
    ]
)
logger = logging.getLogger(__name__)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/download', methods=['POST'])
def download_video():
    try:
        data = request.get_json()
        url = data['url']
        format = data['format']
        
        logger.info(f"Starting download request for URL: {url} in format: {format}")

        if format not in ['mp4', 'wav']:
            logger.error(f"Unsupported format requested: {format}")
            return jsonify({'error': 'Unsupported format'}), 400

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        video_id = extract_video_id(url)
        output_filename = f"{video_id}_{timestamp}.{format}"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)
        
        logger.info(f"Generated output path: {output_path}")

        try:
            if format == 'mp4':
                logger.info("Configuring video download...")
                cmd = [
                    'yt-dlp',
                    '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    '--merge-output-format', 'mp4',
                    '-o', output_path,
                    '--no-playlist',
                    '--progress',
                    '--newline',
                    url
                ]
            else:  # wav
                logger.info("Configuring audio download...")
                temp_output = os.path.join(DOWNLOAD_FOLDER, f"{video_id}_{timestamp}_temp.%(ext)s")
                cmd = [
                    'yt-dlp',
                    '-f', 'bestaudio',
                    '-x',
                    '--audio-format', 'wav',
                    '-o', temp_output,
                    '--no-playlist',
                    '--progress',
                    '--newline',
                    url
                ]

            logger.debug(f"Executing command: {' '.join(cmd)}")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )

            # Modified progress monitoring
            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    output = output.strip()
                    logger.info(output)
                    
                    # Parse progress information
                    if '[download]' in output:
                        # Try to extract progress percentage
                        progress_match = re.search(r'(\d+\.?\d*)%', output)
                        if progress_match:
                            progress = float(progress_match.group(1))
                            socketio.emit('download_progress', {
                                'progress': progress,
                                'status': output
                            })
                    elif 'Downloading' in output:
                        socketio.emit('download_status', {
                            'status': 'Downloading video information...'
                        })
                    elif 'Merging' in output:
                        socketio.emit('download_status', {
                            'status': 'Merging audio and video...'
                        })

            stdout, stderr = process.communicate()

            if process.returncode != 0:
                logger.error(f"Download error: {stderr}")
                raise Exception(f"Download failed: {stderr}")

            # For audio downloads, we need to find the converted file
            if format == 'wav':
                temp_files = [f for f in os.listdir(DOWNLOAD_FOLDER) if f.startswith(f"{video_id}_{timestamp}_temp")]
                if temp_files:
                    temp_file = os.path.join(DOWNLOAD_FOLDER, temp_files[0])
                    shutil.move(temp_file, output_path)

            # Verify the download
            if not os.path.exists(output_path):
                raise FileNotFoundError(f"Download failed: File not found at {output_path}")
            
            if os.path.getsize(output_path) == 0:
                os.remove(output_path)
                raise ValueError("Downloaded file is empty")

            # Send the file
            response = send_from_directory(directory=DOWNLOAD_FOLDER, path=output_filename, as_attachment=True)
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            
            # Clean up after sending
            @response.call_on_close
            def cleanup():
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                except Exception as e:
                    print(f"Cleanup error: {e}")
            
            return response

        except Exception as e:
            # Try alternative format if first attempt fails
            try:
                cmd = [
                    'yt-dlp',
                    '-f', 'best' if format == 'mp4' else 'bestaudio',
                    '-o', output_path,
                    '--no-playlist',
                    '--no-check-certificates',
                    url
                ]
                
                if format == 'wav':
                    cmd.extend(['-x', '--audio-format', 'wav'])

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True
                )
                stdout, stderr = process.communicate()

                if process.returncode != 0 or not os.path.exists(output_path):
                    raise Exception(f"Download failed with both methods. Last error: {stderr}")

                response = send_from_directory(directory=DOWNLOAD_FOLDER, path=output_filename, as_attachment=True)
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
                
                @response.call_on_close
                def cleanup():
                    try:
                        if os.path.exists(output_path):
                            os.remove(output_path)
                    except Exception as e:
                        print(f"Cleanup error: {e}")
                
                return response

            except Exception as e2:
                raise Exception(f"All download attempts failed. Errors: {str(e)}, then {str(e2)}")

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
    socketio.run(app, debug=True, port=5000)
