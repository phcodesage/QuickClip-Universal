from flask import Flask, request, session, send_from_directory, url_for, render_template, jsonify, redirect, Response, send_file
from flask_cors import CORS
import os
import json
from werkzeug.utils import safe_join, secure_filename
from datetime import datetime
import traceback
import sys
import time
import subprocess
import shutil
import logging
from flask_socketio import SocketIO, emit
import re
from pytube import YouTube
from pydub import AudioSegment
import tempfile
import uuid
import requests
from urllib.parse import parse_qs, urlparse
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
from functools import wraps
from datetime import timedelta
from models import db, User, Download, AudioCut
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
from config import Config


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

# Add these configurations
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
app.config['JWT_EXPIRATION_DELTA'] = timedelta(days=7)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///quickclip.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

with app.app_context():
    db.create_all()

# Authentication decorator
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            token = request.headers['Authorization'].split(' ')[1]
        
        if not token:
            return jsonify({'error': 'Token is missing'}), 401
        
        try:
            data = jwt.decode(token, app.config['JWT_SECRET_KEY'], algorithms=["HS256"])
            current_user = User.query.get(data['user_id'])
            if not current_user:
                return jsonify({'error': 'User not found'}), 404
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token has expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Invalid token'}), 401
            
        return f(current_user, *args, **kwargs)
    
    return decorated

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/video-info', methods=['POST'])
def get_video_info():
    try:
        data = request.get_json()
        url = data['url']
        
        # Use yt-dlp to get video info
        cmd = [
            'yt-dlp',
            '-j',  # Output video info as JSON
            '--no-playlist',
            url
        ]
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        stdout, stderr = process.communicate()
        
        if process.returncode != 0:
            raise Exception(f"Failed to fetch video info: {stderr}")
            
        video_info = json.loads(stdout)
        
        return jsonify({
            'title': video_info.get('title', 'Unknown Title'),
            'thumbnail': video_info.get('thumbnail', ''),
            'duration': video_info.get('duration', 0),
            'author': video_info.get('uploader', 'Unknown Author')
        })
    except Exception as e:
        logger.error(f"Error fetching video info: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/download', methods=['POST'])
def download_video():
    try:
        data = request.get_json()
        url = data['url']
        format = data['format']
        
        # Get video info for filename
        cmd = ['yt-dlp', '-j', '--no-playlist', url]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        stdout, stderr = process.communicate()
        
        if process.returncode != 0:
            raise Exception(f"Failed to get video info: {stderr}")
            
        video_info = json.loads(stdout)
        safe_title = "".join([c for c in video_info['title'] if c.isalpha() or c.isdigit() or c==' ']).rstrip()
        
        logger.info(f"Starting download request for URL: {url} in format: {format}")

        if format not in ['mp4', 'wav']:
            logger.error(f"Unsupported format requested: {format}")
            return jsonify({'error': 'Unsupported format'}), 400

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        output_filename = f"{safe_title}_{timestamp}.{format}"
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
                temp_output = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}_{timestamp}_temp.%(ext)s")
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
                temp_files = [f for f in os.listdir(DOWNLOAD_FOLDER) if f.startswith(f"{safe_title}_{timestamp}_temp")]
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

@app.route('/audio-cutter')
def audio_cutter():
    return render_template('audio_cutter.html')

@app.route('/upload-audio', methods=['POST'])
def upload_audio():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
            
        file = request.files['file']
        if not file.filename or not file.filename.lower().endswith(('.mp3', '.wav', '.m4a', '.ogg')):
            return jsonify({'error': 'Invalid audio file format'}), 400

        temp_dir = tempfile.mkdtemp()
        temp_path = os.path.join(temp_dir, secure_filename(file.filename))
        file.save(temp_path)

        # Convert to mp3 if needed and get duration
        try:
            audio = AudioSegment.from_file(temp_path)
            duration = len(audio) / 1000  # Convert to seconds
        except Exception as e:
            raise Exception(f"Error processing audio: {str(e)}")

        # Save the temp_path for later use
        if not hasattr(app, 'temp_files'):
            app.temp_files = {}
        file_id = str(uuid.uuid4())
        app.temp_files[file_id] = temp_path
        
        return jsonify({
            'temp_path': file_id,
            'duration': duration,
            'filename': os.path.basename(temp_path)
        })

    except Exception as e:
        logger.error(f"Error in upload_audio: {str(e)}")
        if 'temp_dir' in locals() and temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        return jsonify({'error': str(e)}), 500

# Add this new route to serve temp files
@app.route('/temp/<file_id>')
def serve_temp_file(file_id):
    if hasattr(app, 'temp_files') and file_id in app.temp_files:
        try:
            return send_file(
                app.temp_files[file_id],
                mimetype='audio/mpeg',
                as_attachment=False,
                download_name='temp_audio.mp3'
            )
        except Exception as e:
            print(f"Error serving temp file: {str(e)}")
            return str(e), 500
    return 'File not found', 404

@app.route('/cut-audio', methods=['POST'])
def cut_audio():
    try:
        data = request.get_json()
        file_id = data['temp_path']
        start_time = float(data['start_time'])
        end_time = float(data['end_time'])
        
        if not hasattr(app, 'temp_files') or file_id not in app.temp_files:
            return jsonify({'error': 'Audio file not found'}), 404
            
        temp_path = app.temp_files[file_id]
        
        # Load audio and cut
        audio = AudioSegment.from_file(temp_path)
        cut_audio = audio[start_time*1000:end_time*1000]
        
        # Save to new temp file
        output_path = os.path.join(DOWNLOAD_FOLDER, 'cut_audio.mp3')
        cut_audio.export(output_path, format='mp3')
        
        response = send_from_directory(DOWNLOAD_FOLDER, 'cut_audio.mp3', as_attachment=True)
        
        @response.call_on_close
        def cleanup():
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception as e:
                print(f"Cleanup error: {e}")
        
        return response
        
    except Exception as e:
        print(f"Error in cut_audio: {str(e)}")
        return jsonify({'error': str(e)}), 500



# Add after your existing imports
users_db = {}  # In-memory storage for demo. Replace with real database.

@app.route('/auth')
def auth():
    return render_template('auth.html')

@app.route('/auth/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('auth.html')
        
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    
    try:
        user = User.query.filter_by(email=email).first()
        
        if not user or not check_password_hash(user.password_hash, password):
            return jsonify({'error': 'Invalid email or password'}), 401
        
        # Generate tokens
        access_token, refresh_token = create_tokens(user.id)
        
        return jsonify({
            'access_token': access_token,
            'refresh_token': refresh_token,
            'message': 'Login successful'
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auth/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'GET':
        return render_template('auth.html')
        
    try:
        data = request.get_json()
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')
        
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'Email already registered'}), 400
            
        # Create new user
        new_user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password)
        )
        
        db.session.add(new_user)
        db.session.commit()
        
        # Generate tokens
        access_token, refresh_token = create_tokens(new_user.id)
        
        return jsonify({
            'access_token': access_token,
            'refresh_token': refresh_token,
            'message': 'Signup successful'
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/auth/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('index'))

@app.route('/auth/<provider>')
def social_auth(provider):
    # Placeholder for social auth
    return jsonify({
        'message': f'Social auth with {provider} not implemented'
    }), 501

@app.route('/auth/google')
def google_auth():
    # Implement Google OAuth here
    pass

@app.route('/auth/github')
def github_auth():
    # Implement GitHub OAuth here
    pass

# Profile/Dashboard route
@app.route('/profile')
@token_required
def profile(current_user):
    return render_template('profile.html', user=current_user)

# Initialize Flask-Mail
mail = Mail(app)
app.config.from_object(Config)

# Create serializer for tokens
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

def send_reset_email(user_email):
    token = serializer.dumps(user_email, salt='password-reset-salt')
    reset_url = url_for('reset_password', token=token, _external=True)
    
    msg = Message('Password Reset Request',
                 recipients=[user_email])
    msg.body = f'''To reset your password, visit the following link:
{reset_url}

If you did not make this request, simply ignore this email.
'''
    mail.send(msg)

@app.route('/auth/forgot-password', methods=['POST'])
def forgot_password():
    try:
        data = request.get_json()
        email = data.get('email')
        
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({'error': 'Email not found'}), 404
            
        send_reset_email(email)
        return jsonify({'message': 'Password reset email sent'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auth/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if request.method == 'GET':
        try:
            email = serializer.loads(token, salt='password-reset-salt', max_age=3600)
            return render_template('reset_password.html', token=token)
        except:
            return 'The password reset link is invalid or has expired.', 400
    
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=3600)
        data = request.get_json()
        new_password = data.get('password')
        
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({'error': 'User not found'}), 404
            
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        
        return jsonify({'message': 'Password has been reset successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def create_tokens(user_id):
    access_token = jwt.encode({
        'user_id': user_id,
        'exp': datetime.utcnow() + app.config['JWT_ACCESS_TOKEN_EXPIRES']
    }, app.config['JWT_SECRET_KEY'])
    
    refresh_token = jwt.encode({
        'user_id': user_id,
        'exp': datetime.utcnow() + app.config['JWT_REFRESH_TOKEN_EXPIRES']
    }, app.config['JWT_SECRET_KEY'])
    
    return access_token, refresh_token

@app.route('/auth/refresh', methods=['POST'])
def refresh_token():
    refresh_token = request.headers.get('Authorization')
    if not refresh_token:
        return jsonify({'error': 'Refresh token is missing'}), 401
    
    try:
        data = jwt.decode(refresh_token.split(' ')[1], 
                         app.config['JWT_SECRET_KEY'], 
                         algorithms=["HS256"])
        
        user = User.query.get(data['user_id'])
        if not user:
            return jsonify({'error': 'User not found'}), 404
            
        access_token, new_refresh_token = create_tokens(user.id)
        
        return jsonify({
            'access_token': access_token,
            'refresh_token': new_refresh_token
        }), 200
    except jwt.ExpiredSignatureError:
        return jsonify({'error': 'Refresh token has expired'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'error': 'Invalid refresh token'}), 401

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)
