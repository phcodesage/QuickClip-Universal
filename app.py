from flask import Flask, request, send_from_directory, render_template, jsonify, abort, Response
from flask_cors import CORS
import os
import json
from werkzeug.utils import safe_join
from datetime import datetime
from pytube import YouTube
import traceback
import sys
import time
import yt_dlp  # Import yt-dlp as a module instead

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# Ensure absolute path for downloads
DOWNLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'downloads'))
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/download', methods=['POST'])
def download_video():
    try:
        data = request.get_json()
        url = data['url']
        format = data['format']

        if format not in ['mp4', 'wav']:
            return jsonify({'error': 'Unsupported format'}), 400

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        
        try:
            # Use yt-dlp as a Python module
            video_id = extract_video_id(url)
            output_filename = f"{video_id}_{timestamp}.{format}"
            output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

            ydl_opts = {
                'format': 'best' if format == 'mp4' else 'bestaudio',
                'outtmpl': output_path,
                'no_playlist': True,
            }

            if format == 'wav':
                ydl_opts.update({
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'wav',
                    }],
                })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
        except Exception as e:
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
    app.run(debug=True, port=5000)
