import audio_patch  # Monkey-patch audioop for Python 3.13 before pydub or audio libs
from flask import Flask, request, session, send_from_directory, url_for, render_template, jsonify, redirect, Response, send_file
from flask_cors import CORS
import os
import json
from werkzeug.utils import safe_join, secure_filename
from datetime import datetime, timedelta, timezone
import traceback
import sys
import time
import subprocess
import shutil
import logging
from flask_socketio import SocketIO, emit
import re
try:
    import soundfile as sf
except ImportError:
    sf = None

try:
    import numpy as np
except ImportError:
    np = None
import tempfile
import uuid
import requests
from urllib.parse import parse_qs, urlparse
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
from functools import wraps
from models import db, User, Download, AudioCut
try:
    from flask_mail import Mail, Message
except ImportError:
    Mail = None
    Message = None
from itsdangerous import URLSafeTimedSerializer
from config import Config

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config.from_object(Config)

CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Ensure absolute path for downloads
DOWNLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'downloads'))
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Temporary audio uploads tracking dictionary
app.temp_files = {}

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('download.log')
    ]
)
logger = logging.getLogger(__name__)

db.init_app(app)

with app.app_context():
    db.create_all()

mail = Mail(app) if Mail is not None else None
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

def get_yt_dlp_cmd():
    """Helper to locate or fallback to python -m yt_dlp command."""
    yt_bin = shutil.which('yt-dlp')
    if yt_bin:
        return [yt_bin]
    return [sys.executable, '-m', 'yt_dlp']

def get_user_from_request():
    """Extract current user from Bearer token if present."""
    token = None
    if 'Authorization' in request.headers:
        parts = request.headers['Authorization'].split(' ')
        if len(parts) == 2:
            token = parts[1]
    if token:
        try:
            data = jwt.decode(token, app.config['JWT_SECRET_KEY'], algorithms=["HS256"])
            return db.session.get(User, data.get('user_id'))
        except Exception:
            pass
    return None

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_user_from_request()
        if not user:
            return jsonify({'error': 'Valid authorization token required'}), 401
        return f(user, *args, **kwargs)
    return decorated

def create_tokens(user_id):
    now = datetime.now(timezone.utc)
    access_token = jwt.encode({
        'user_id': user_id,
        'exp': now + app.config.get('JWT_ACCESS_TOKEN_EXPIRES', timedelta(hours=1))
    }, app.config['JWT_SECRET_KEY'], algorithm="HS256")
    
    refresh_token = jwt.encode({
        'user_id': user_id,
        'exp': now + app.config.get('JWT_REFRESH_TOKEN_EXPIRES', timedelta(days=30))
    }, app.config['JWT_SECRET_KEY'], algorithm="HS256")
    
    return access_token, refresh_token

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/downloader', methods=['GET'])
def downloader_page():
    return render_template('downloader.html')

@app.route('/video-info', methods=['POST'])
def get_video_info():
    try:
        data = request.get_json() or {}
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': 'Video/Media URL is required'}), 400
        
        cmd = get_yt_dlp_cmd() + [
            '-j',
            '--no-playlist',
            '--no-check-certificates',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            url
        ]
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate()
        
        if process.returncode != 0:
            logger.error(f"yt-dlp video-info error: {stderr}")
            err_msg = stderr.splitlines()[-1] if stderr else 'Failed to extract video information'
            raise Exception(err_msg)
            
        video_info = json.loads(stdout)
        
        # Detect platform from extractor or domain
        extractor = (video_info.get('extractor_key') or '').lower()
        domain = urlparse(url).netloc.lower()

        platform = 'media'
        if 'youtube' in extractor or 'youtube' in domain or 'youtu.be' in domain:
            platform = 'youtube'
        elif 'instagram' in extractor or 'instagram' in domain:
            platform = 'instagram'
        elif 'facebook' in extractor or 'facebook' in domain or 'fb' in domain:
            platform = 'facebook'
        elif 'tiktok' in extractor or 'tiktok' in domain:
            platform = 'tiktok'
        elif 'twitter' in extractor or 'x' in domain or 'twitter' in domain:
            platform = 'twitter'
        elif 'vimeo' in extractor or 'vimeo' in domain:
            platform = 'vimeo'
        elif 'reddit' in extractor or 'reddit' in domain:
            platform = 'reddit'

        raw_title = video_info.get('title') or video_info.get('description') or 'Social Media Video'
        # Truncate long descriptions if title falls back to description
        if len(raw_title) > 100:
            raw_title = raw_title[:97] + "..."

        return jsonify({
            'title': raw_title,
            'thumbnail': video_info.get('thumbnail') or '',
            'duration': video_info.get('duration') or 0,
            'author': video_info.get('uploader') or video_info.get('channel') or video_info.get('uploader_id') or 'Media Creator',
            'platform': platform
        })
    except Exception as e:
        logger.error(f"Error fetching video info: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/download', methods=['POST'])
def download_video():
    try:
        data = request.get_json() or {}
        url = data.get('url', '').strip()
        fmt = data.get('format', 'mp4').lower()
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400

        if fmt not in ['mp4', 'wav', 'mp3']:
            return jsonify({'error': 'Unsupported format. Choose mp4, wav, or mp3.'}), 400

        # Fetch video metadata
        info_cmd = get_yt_dlp_cmd() + [
            '-j', '--no-playlist', '--no-check-certificates',
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            url
        ]
        process = subprocess.Popen(info_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()
        
        raw_title = "QuickClip_Media"
        if process.returncode == 0 and stdout:
            try:
                info = json.loads(stdout)
                raw_title = info.get('title') or info.get('description') or 'QuickClip_Media'
            except Exception:
                pass

        # Create safe filename
        clean_title = re.sub(r'[^\w\s-]', '', raw_title[:50]).strip()
        clean_title = re.sub(r'[-\s]+', '_', clean_title)
        if not clean_title:
            clean_title = f"media_{uuid.uuid4().hex[:8]}"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"{clean_title}_{timestamp}.{fmt}"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_filename)

        logger.info(f"Starting download request: URL={url}, format={fmt}, output={output_filename}")

        yt_cmd = get_yt_dlp_cmd()
        user_agent_flags = ['--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36']

        if fmt == 'mp4':
            cmd = yt_cmd + [
                '-f', 'bestvideo+bestaudio/best',
                '--merge-output-format', 'mp4',
                '-o', output_path,
                '--no-playlist',
                '--no-check-certificates',
                '--progress',
                '--newline'
            ] + user_agent_flags + [url]
        elif fmt == 'mp3':
            temp_out = os.path.join(DOWNLOAD_FOLDER, f"{clean_title}_{timestamp}_temp.%(ext)s")
            cmd = yt_cmd + [
                '-f', 'bestaudio/best',
                '-x',
                '--audio-format', 'mp3',
                '--audio-quality', '0',
                '-o', temp_out,
                '--no-playlist',
                '--no-check-certificates',
                '--progress',
                '--newline'
            ] + user_agent_flags + [url]
        else: # wav
            temp_out = os.path.join(DOWNLOAD_FOLDER, f"{clean_title}_{timestamp}_temp.%(ext)s")
            cmd = yt_cmd + [
                '-f', 'bestaudio/best',
                '-x',
                '--audio-format', 'wav',
                '-o', temp_out,
                '--no-playlist',
                '--no-check-certificates',
                '--progress',
                '--newline'
            ] + user_agent_flags + [url]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                line_str = line.strip()
                logger.info(line_str)
                
                if '[download]' in line_str:
                    match = re.search(r'(\d+\.?\d*)%', line_str)
                    if match:
                        socketio.emit('download_progress', {
                            'progress': float(match.group(1)),
                            'status': line_str
                        })
                elif 'Downloading' in line_str:
                    socketio.emit('download_status', {'status': 'Downloading media content...'})
                elif 'Merging' in line_str or 'Extracting' in line_str:
                    socketio.emit('download_status', {'status': 'Processing output audio/video...'})

        stdout, stderr = process.communicate()

        if process.returncode != 0 and not os.path.exists(output_path):
            # Fallback attempt
            fallback_cmd = yt_cmd + [
                '-f', 'best',
                '-o', output_path,
                '--no-playlist',
                '--no-check-certificates',
                url
            ]
            if fmt in ['mp3', 'wav']:
                fallback_cmd.extend(['-x', '--audio-format', fmt])
            
            fb_proc = subprocess.Popen(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            fb_proc.communicate()

        if fmt in ['wav', 'mp3'] and not os.path.exists(output_path):
            temp_files = [f for f in os.listdir(DOWNLOAD_FOLDER) if f.startswith(f"{clean_title}_{timestamp}_temp")]
            if temp_files:
                shutil.move(os.path.join(DOWNLOAD_FOLDER, temp_files[0]), output_path)

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise FileNotFoundError(f"Download processing failed or returned empty file.")

        # Record download in database if user is logged in
        user = get_user_from_request()
        if user:
            try:
                db.session.add(Download(url=url, format=fmt, user_id=user.id))
                db.session.commit()
            except Exception as dbe:
                logger.error(f"Failed to record download in DB: {dbe}")

        response = send_from_directory(directory=DOWNLOAD_FOLDER, path=output_filename, as_attachment=True)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        
        @response.call_on_close
        def cleanup():
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                
        return response

    except Exception as e:
        logger.error(f"Download exception: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/audio-cutter')
def audio_cutter():
    return render_template('audio_cutter.html')

@app.route('/upload-audio', methods=['POST'])
def upload_audio():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No audio file provided'}), 400
            
        file = request.files['file']
        if not file or not file.filename:
            return jsonify({'error': 'No selected file'}), 400

        allowed_exts = ('.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac', '.webm')
        if not file.filename.lower().endswith(allowed_exts):
            return jsonify({'error': f'Unsupported file format. Please upload one of: {", ".join(allowed_exts)}'}), 400

        temp_dir = tempfile.mkdtemp()
        orig_name = secure_filename(file.filename) or "uploaded_audio"
        raw_path = os.path.join(temp_dir, f"raw_{orig_name}")
        file.save(raw_path)

        # Convert to standardized 16-bit PCM WAV for WaveSurfer & cutting
        wav_filename = f"converted_{uuid.uuid4().hex[:8]}.wav"
        wav_path = os.path.join(temp_dir, wav_filename)

        conv_cmd = [
            'ffmpeg', '-y',
            '-i', raw_path,
            '-acodec', 'pcm_s16le',
            '-ar', '44100',
            '-ac', '2',
            wav_path
        ]
        res = subprocess.run(conv_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({'error': f"Failed to convert audio file: {res.stderr}"}), 400

        # Calculate exact duration using ffprobe
        probe_cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            wav_path
        ]
        probe_res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        duration = 0.0
        try:
            duration = float(probe_res.stdout.strip())
        except Exception:
            try:
                audio_data, sr = sf.read(wav_path)
                duration = len(audio_data) / float(sr)
            except Exception:
                duration = 0.0

        file_id = str(uuid.uuid4())
        app.temp_files[file_id] = {
            'wav_path': wav_path,
            'temp_dir': temp_dir,
            'filename': orig_name
        }
        
        return jsonify({
            'temp_path': file_id,
            'duration': round(duration, 3),
            'filename': orig_name
        })

    except Exception as e:
        logger.error(f"Error in upload_audio: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/temp/<file_id>')
def serve_temp_file(file_id):
    if file_id in app.temp_files:
        info = app.temp_files[file_id]
        wav_path = info['wav_path'] if isinstance(info, dict) else info
        if os.path.exists(wav_path):
            return send_file(
                wav_path,
                mimetype='audio/wav',
                as_attachment=False,
                download_name='temp_audio.wav'
            )
    return 'File not found', 404

@app.route('/cut-audio', methods=['POST'])
def cut_audio():
    try:
        data = request.get_json() or {}
        file_id = data.get('temp_path')
        start_time = float(data.get('start_time', 0))
        end_time = float(data.get('end_time', 0))
        out_fmt = data.get('output_format', 'mp3').lower()
        if out_fmt not in ['mp3', 'wav']:
            out_fmt = 'mp3'

        if not file_id or file_id not in app.temp_files:
            return jsonify({'error': 'Audio session expired or not found. Please upload file again.'}), 404
            
        file_info = app.temp_files[file_id]
        wav_path = file_info['wav_path'] if isinstance(file_info, dict) else file_info

        if not os.path.exists(wav_path):
            return jsonify({'error': 'Source audio file missing.'}), 404

        out_name = f"cut_{uuid.uuid4().hex[:8]}.{out_fmt}"
        output_path = os.path.join(DOWNLOAD_FOLDER, out_name)

        cut_cmd = [
            'ffmpeg', '-y',
            '-ss', str(start_time),
            '-to', str(end_time),
            '-i', wav_path
        ]
        if out_fmt == 'mp3':
            cut_cmd.extend(['-c:a', 'libmp3lame', '-b:a', '192k'])
        else:
            cut_cmd.extend(['-c:a', 'pcm_s16le'])
        cut_cmd.append(output_path)

        res = subprocess.run(cut_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0 or not os.path.exists(output_path):
            return jsonify({'error': f"Audio cutting failed: {res.stderr}"}), 500

        # Log AudioCut for logged-in user
        user = get_user_from_request()
        if user:
            try:
                db.session.add(AudioCut(
                    filename=file_info.get('filename', 'cut_audio') if isinstance(file_info, dict) else 'cut_audio',
                    start_time=start_time,
                    end_time=end_time,
                    user_id=user.id
                ))
                db.session.commit()
            except Exception as dbe:
                logger.error(f"Failed to record AudioCut in DB: {dbe}")

        response = send_from_directory(DOWNLOAD_FOLDER, out_name, as_attachment=True)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        
        @response.call_on_close
        def cleanup():
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception as e:
                logger.error(f"Cleanup cut file error: {e}")

        return response
        
    except Exception as e:
        logger.error(f"Error in cut_audio: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

# Authentication Routes
@app.route('/auth')
def auth():
    return render_template('auth.html')

@app.route('/auth/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('auth.html')
        
    data = request.get_json() or {}
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    try:
        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            return jsonify({'error': 'Invalid email or password'}), 401
        
        access_token, refresh_token = create_tokens(user.id)
        
        return jsonify({
            'access_token': access_token,
            'refresh_token': refresh_token,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email
            },
            'message': 'Login successful'
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auth/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'GET':
        return render_template('auth.html')
        
    try:
        data = request.get_json() or {}
        username = data.get('username', '').strip()
        email = data.get('email', '').strip()
        password = data.get('password', '').strip()
        
        if not username or not email or not password:
            return jsonify({'error': 'Username, email, and password are required'}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'Email already registered'}), 400

        if User.query.filter_by(username=username).first():
            return jsonify({'error': 'Username already taken'}), 400
            
        new_user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password)
        )
        
        db.session.add(new_user)
        db.session.commit()
        
        access_token, refresh_token = create_tokens(new_user.id)
        
        return jsonify({
            'access_token': access_token,
            'refresh_token': refresh_token,
            'user': {
                'id': new_user.id,
                'username': new_user.username,
                'email': new_user.email
            },
            'message': 'Signup successful'
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/auth/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('index'))

@app.route('/api/me')
def get_me():
    user = get_user_from_request()
    if not user:
        return jsonify({'authenticated': False}), 200
    downloads_count = Download.query.filter_by(user_id=user.id).count()
    cuts_count = AudioCut.query.filter_by(user_id=user.id).count()
    return jsonify({
        'authenticated': True,
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'downloads_count': downloads_count,
            'cuts_count': cuts_count
        }
    })

@app.route('/profile')
def profile_page():
    user = get_user_from_request()
    if not user:
        return redirect(url_for('auth'))
    
    downloads_count = Download.query.filter_by(user_id=user.id).count()
    cuts_count = AudioCut.query.filter_by(user_id=user.id).count()
    recent_downloads = Download.query.filter_by(user_id=user.id).order_by(Download.created_at.desc()).limit(5).all()
    recent_cuts = AudioCut.query.filter_by(user_id=user.id).order_by(AudioCut.created_at.desc()).limit(5).all()
    
    return render_template(
        'profile.html', 
        user=user, 
        downloads_count=downloads_count, 
        cuts_count=cuts_count,
        recent_downloads=recent_downloads,
        recent_cuts=recent_cuts
    )

@app.route('/auth/forgot-password', methods=['POST'])
def forgot_password():
    try:
        data = request.get_json() or {}
        email = data.get('email', '').strip()
        
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({'error': 'Email address not found'}), 404
            
        token = serializer.dumps(email, salt='password-reset-salt')
        reset_url = url_for('reset_password', token=token, _external=True)
        
        try:
            msg = Message('Password Reset Request - QuickClip Universal', recipients=[email])
            msg.body = f"To reset your password, please visit:\n{reset_url}\n\nIf you did not request this, please ignore this email."
            mail.send(msg)
        except Exception as mail_err:
            logger.warning(f"Mail send simulation / error: {mail_err}")
            
        return jsonify({'message': 'Password reset link generated.', 'reset_url': reset_url}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auth/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if request.method == 'GET':
        try:
            email = serializer.loads(token, salt='password-reset-salt', max_age=3600)
            return render_template('reset_password.html', token=token)
        except Exception:
            return 'The password reset link is invalid or has expired.', 400
    
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=3600)
        data = request.get_json() or {}
        new_password = data.get('password', '').strip()
        
        if not new_password:
            return jsonify({'error': 'New password is required'}), 400

        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({'error': 'User not found'}), 404
            
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        
        return jsonify({'message': 'Password has been reset successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auth/refresh', methods=['POST'])
def refresh_token():
    auth_header = request.headers.get('Authorization', '')
    if not auth_header:
        return jsonify({'error': 'Refresh token is missing'}), 401
    
    try:
        parts = auth_header.split(' ')
        token = parts[1] if len(parts) == 2 else parts[0]
        data = jwt.decode(token, app.config['JWT_SECRET_KEY'], algorithms=["HS256"])
        
        user = db.session.get(User, data['user_id'])
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
    socketio.run(app, debug=True, host='0.0.0.0', port=5050, allow_unsafe_werkzeug=True)
