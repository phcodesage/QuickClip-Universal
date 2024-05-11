from flask import Flask, request, send_from_directory, render_template, jsonify, abort, Response
from flask_cors import CORS
import os
import subprocess
import json
from werkzeug.utils import safe_join
import shlex  # For safely escaping shell commands
from datetime import datetime

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
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

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")  # Unique timestamp
        video_info_command = f'yt-dlp --skip-download --print-json {shlex.quote(url)}'
        video_info_result = subprocess.run(video_info_command, shell=True, text=True, capture_output=True, check=True)
        video_info = json.loads(video_info_result.stdout)
        video_id = video_info.get('id', 'download')

        output_filename = f"{video_id}_{timestamp}.{format}"
        output_path = safe_join(DOWNLOAD_FOLDER, output_filename)

        print(f"Downloading to {output_path}")  # Log where we're downloading
        if format == 'mp4':
            command = f'yt-dlp -f best -o {shlex.quote(output_path)} -- {shlex.quote(url)}'
        elif format == 'wav':
            command = f'yt-dlp --extract-audio --audio-format wav -o {shlex.quote(output_path)} -- {shlex.quote(url)}'

        subprocess.run(command, shell=True, check=True)
        print(f"File ready at {output_path}")  # Log the result path

        response = send_from_directory(directory=DOWNLOAD_FOLDER, path=output_filename, as_attachment=True)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"  # Prevent caching
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except subprocess.CalledProcessError as e:
        print(f"Subprocess error: {e}")
        return jsonify({'error': 'Failed to download video'}), 500
    except Exception as e:
        print(f"Unexpected error: {e}")
        abort(500, description=str(e))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
