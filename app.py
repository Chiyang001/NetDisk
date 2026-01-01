import os
import shutil
import time
import shortuuid
import zipfile
import tempfile
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, send_from_directory, jsonify, abort, send_file, session, redirect, url_for, after_this_request
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image
import io
import threading

app = Flask(__name__)

# --- 配置 ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, 'storage')
FOLDERZIP_DIR = os.path.join(BASE_DIR, 'folderzip')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///disk.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024  # 16GB 最大上传大小
app.secret_key = 'your_secret_key_here' # 用于Session加密

# Session 配置 - 防止下载时 session 丢失
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

# 默认登录密码（首次运行时使用）
DEFAULT_PASSWORD = '123456'

# 版本信息
VERSION = 'Ver.2026-0101Beta'
AUTHOR = '炽阳001'
GITHUB_URL = 'https://github.com/Chiyang001?tab=repositories'
BILIBILI_URL = 'https://space.bilibili.com/404891612'
PROJECT_URL = 'https://github.com/Chiyang001/NetDisk'

if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR)

if not os.path.exists(FOLDERZIP_DIR):
    os.makedirs(FOLDERZIP_DIR)

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

db = SQLAlchemy(app)

# --- 数据库模型：分享链接 ---
class ShareLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(20), unique=True, nullable=False)
    file_path = db.Column(db.Text, nullable=False) # 相对路径，多个文件用 | 分隔
    expire_at = db.Column(db.DateTime, nullable=True) # None表示永久
    created_at = db.Column(db.DateTime, default=datetime.now)
    is_batch = db.Column(db.Boolean, default=False) # 是否为批量分享

# --- 数据库模型：系统设置 ---
class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

# --- 数据库模型：密码重置令牌 ---
class PasswordResetToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(32), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    expire_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)

# 初始化数据库
with app.app_context():
    db.create_all()
    
    # 检查并添加新字段（兼容旧数据库）
    try:
        # 尝试查询 is_batch 字段，如果不存在会抛出异常
        ShareLink.query.with_entities(ShareLink.is_batch).first()
    except Exception as e:
        # 字段不存在，需要添加
        print("检测到数据库需要更新，正在添加新字段...")
        try:
            with db.engine.connect() as conn:
                # 添加 is_batch 字段
                conn.execute(db.text("ALTER TABLE share_link ADD COLUMN is_batch BOOLEAN DEFAULT 0"))
                # 修改 file_path 字段类型为 TEXT
                conn.execute(db.text("ALTER TABLE share_link MODIFY COLUMN file_path TEXT"))
                conn.commit()
                print("数据库更新完成")
        except Exception as alter_error:
            print(f"数据库更新失败（可能已经更新过）: {alter_error}")
    
    # 初始化默认设置
    if not Settings.query.filter_by(key='password_hash').first():
        default_hash = generate_password_hash(DEFAULT_PASSWORD)
        db.session.add(Settings(key='password_hash', value=default_hash))
        print("\n" + "="*60)
        print("🔐 首次运行检测到！")
        print(f"📝 默认登录密码：{DEFAULT_PASSWORD}")
        print("⚠️  请立即登录并修改密码以确保系统安全！")
        print("="*60 + "\n")
    
    if not Settings.query.filter_by(key='theme').first():
        db.session.add(Settings(key='theme', value='light'))
    
    if not Settings.query.filter_by(key='background_type').first():
        db.session.add(Settings(key='background_type', value='image'))
    
    if not Settings.query.filter_by(key='background_image').first():
        db.session.add(Settings(key='background_image', value='bg.png'))
    
    if not Settings.query.filter_by(key='background_color').first():
        db.session.add(Settings(key='background_color', value='#667eea'))
    
    if not Settings.query.filter_by(key='security_question').first():
        db.session.add(Settings(key='security_question', value=''))
    
    if not Settings.query.filter_by(key='security_answer').first():
        db.session.add(Settings(key='security_answer', value=''))
    
    db.session.commit()

# --- 辅助函数：获取设置 ---
def get_setting(key, default=None):
    setting = Settings.query.filter_by(key=key).first()
    return setting.value if setting else default

def set_setting(key, value):
    setting = Settings.query.filter_by(key=key).first()
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        setting = Settings(key=key, value=value)
        db.session.add(setting)
    db.session.commit()

# --- 辅助函数：验证密码 ---
def verify_password(password):
    password_hash = get_setting('password_hash')
    if password_hash:
        return check_password_hash(password_hash, password)
    return False

# --- 辅助函数：检查是否为默认密码 ---
def is_default_password():
    """检查当前密码是否为默认密码"""
    password_hash = get_setting('password_hash')
    if password_hash:
        return check_password_hash(password_hash, DEFAULT_PASSWORD)
    return True

# --- 辅助函数：清理过期的 ZIP 文件 ---
def cleanup_old_zips():
    """删除超过24小时的 ZIP 文件"""
    try:
        now = time.time()
        for filename in os.listdir(FOLDERZIP_DIR):
            filepath = os.path.join(FOLDERZIP_DIR, filename)
            if os.path.isfile(filepath) and filename.endswith('.zip'):
                file_age = now - os.path.getmtime(filepath)
                if file_age > 24 * 3600:  # 24小时
                    os.remove(filepath)
                    print(f"已删除过期 ZIP 文件: {filename}")
    except Exception as e:
        print(f"清理 ZIP 文件失败: {e}")

# --- 后台定时清理任务 ---
def schedule_cleanup():
    """每小时执行一次清理任务"""
    cleanup_old_zips()
    # 设置下次执行
    threading.Timer(3600, schedule_cleanup).start()

# 启动清理任务
schedule_cleanup()

# --- 登录验证装饰器 ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- 辅助函数：安全路径检查 ---
def get_safe_path(req_path):
    # 防止 ../ 攻击，确保路径在 storage 目录下
    if not req_path or req_path.strip() == '/':
        return STORAGE_DIR
    
    # 移除开头的 /
    req_path = req_path.lstrip('/')
    safe_path = os.path.abspath(os.path.join(STORAGE_DIR, req_path))
    if not safe_path.startswith(STORAGE_DIR):
        raise ValueError("非法路径")
    return safe_path

def get_rel_path(full_path):
    return full_path.replace(STORAGE_DIR, '').replace('\\', '/').lstrip('/')

# --- 辅助函数：判断文件类型 ---
def is_image(filename):
    image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico'}
    return os.path.splitext(filename.lower())[1] in image_exts

def is_video(filename):
    video_exts = {'.mp4', '.webm', '.ogg', '.mov', '.avi', '.mkv', '.flv', '.wmv'}
    return os.path.splitext(filename.lower())[1] in video_exts

def is_audio(filename):
    audio_exts = {'.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac', '.wma', '.ape', '.opus'}
    return os.path.splitext(filename.lower())[1] in audio_exts

def is_archive(filename):
    archive_exts = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tar.gz', '.tar.bz2', '.tar.xz'}
    lower_name = filename.lower()
    return any(lower_name.endswith(ext) for ext in archive_exts)

def is_office_doc(filename):
    office_exts = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
    return os.path.splitext(filename.lower())[1] in office_exts

def is_pdf(filename):
    return os.path.splitext(filename.lower())[1] == '.pdf'

def get_file_type(filename):
    if is_image(filename):
        return 'image'
    elif is_video(filename):
        return 'video'
    elif is_audio(filename):
        return 'audio'
    elif is_archive(filename):
        return 'archive'
    elif is_office_doc(filename):
        return 'office'
    elif is_pdf(filename):
        return 'pdf'
    else:
        return 'file'

# --- 路由：登录页面 ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if verify_password(password):
            # 检查是否已设置安全问题
            security_question = get_setting('security_question', '')
            if not security_question:
                # 未设置安全问题，标记需要设置并跳转到设置页面
                session['temp_logged_in'] = True  # 临时登录状态
                session.permanent = True
                return redirect(url_for('setup_security'))
            
            session['logged_in'] = True
            session.permanent = True  # 使 session 持久化
            return redirect(url_for('index'))
        else:
            # 获取主题和背景设置（错误时也要传递）
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('login.html', 
                                 error='密码错误',
                                 theme=theme, 
                                 bg_type=bg_type, 
                                 bg_image=bg_image, 
                                 bg_color=bg_color,
                                 is_default_password=is_default_password(),
                                 reset_success=False)
    
    # 获取主题和背景设置
    theme = get_setting('theme', 'light')
    bg_type = get_setting('background_type', 'image')
    bg_image = get_setting('background_image', 'bg.png')
    bg_color = get_setting('background_color', '#667eea')
    
    # 检查是否有重置成功的标记
    reset_success = request.args.get('reset') == 'success'
    
    return render_template('login.html', 
                         theme=theme, 
                         bg_type=bg_type, 
                         bg_image=bg_image, 
                         bg_color=bg_color,
                         is_default_password=is_default_password(),
                         reset_success=reset_success)

# --- 路由：登出 ---
@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('temp_logged_in', None)
    return redirect(url_for('login'))

# --- 路由：设置安全问题（首次登录必须设置）---
@app.route('/setup-security', methods=['GET', 'POST'])
def setup_security():
    # 检查是否有临时登录状态
    if not session.get('temp_logged_in'):
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        question = request.form.get('question', '').strip()
        custom_question = request.form.get('custom_question', '').strip()
        answer = request.form.get('answer', '').strip()
        
        # 如果选择了自定义问题
        if question == 'custom':
            question = custom_question
        
        if not question or not answer:
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('setup_security.html',
                                 error='问题和答案不能为空',
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color)
        
        if len(answer) < 2:
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('setup_security.html',
                                 error='答案至少需要2个字符',
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color)
        
        # 保存安全问题
        set_setting('security_question', question)
        set_setting('security_answer', answer)
        
        # 清除临时登录状态，设置正式登录状态
        session.pop('temp_logged_in', None)
        session['logged_in'] = True
        session.permanent = True
        
        return redirect(url_for('index'))
    
    # GET 请求
    theme = get_setting('theme', 'light')
    bg_type = get_setting('background_type', 'image')
    bg_image = get_setting('background_image', 'bg.png')
    bg_color = get_setting('background_color', '#667eea')
    
    return render_template('setup_security.html',
                         theme=theme,
                         bg_type=bg_type,
                         bg_image=bg_image,
                         bg_color=bg_color)

# --- 路由：忘记密码页面 ---
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        security_answer = request.form.get('security_answer', '').strip()
        
        # 获取设置的安全问题答案
        saved_answer = get_setting('security_answer', '')
        
        if not saved_answer:
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('forgot_password.html',
                                 error='管理员未设置安全问题，请联系管理员重置密码',
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color,
                                 security_question=get_setting('security_question', ''))
        
        # 验证答案（不区分大小写）
        if security_answer.lower() == saved_answer.lower():
            # 生成重置令牌
            token = shortuuid.uuid()
            expire_at = datetime.now() + timedelta(minutes=30)  # 30分钟有效期
            
            reset_token = PasswordResetToken(token=token, expire_at=expire_at)
            db.session.add(reset_token)
            db.session.commit()
            
            # 重定向到重置密码页面
            return redirect(url_for('reset_password', token=token))
        else:
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('forgot_password.html',
                                 error='安全问题答案错误',
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color,
                                 security_question=get_setting('security_question', ''))
    
    # GET 请求
    theme = get_setting('theme', 'light')
    bg_type = get_setting('background_type', 'image')
    bg_image = get_setting('background_image', 'bg.png')
    bg_color = get_setting('background_color', '#667eea')
    security_question = get_setting('security_question', '')
    
    if not security_question:
        return render_template('forgot_password.html',
                             error='管理员未设置安全问题，请联系管理员重置密码',
                             theme=theme,
                             bg_type=bg_type,
                             bg_image=bg_image,
                             bg_color=bg_color,
                             security_question='')
    
    return render_template('forgot_password.html',
                         theme=theme,
                         bg_type=bg_type,
                         bg_image=bg_image,
                         bg_color=bg_color,
                         security_question=security_question)

# --- 路由：重置密码页面 ---
@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    # 验证令牌
    reset_token = PasswordResetToken.query.filter_by(token=token, used=False).first()
    
    if not reset_token:
        return "重置链接无效或已使用", 403
    
    if datetime.now() > reset_token.expire_at:
        return "重置链接已过期，请重新申请", 403
    
    if request.method == 'POST':
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        
        if not new_password or len(new_password) < 6:
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('reset_password.html',
                                 error='密码至少需要6位',
                                 token=token,
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color)
        
        if new_password != confirm_password:
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('reset_password.html',
                                 error='两次输入的密码不一致',
                                 token=token,
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color)
        
        # 更新密码
        new_hash = generate_password_hash(new_password)
        set_setting('password_hash', new_hash)
        
        # 标记令牌为已使用
        reset_token.used = True
        db.session.commit()
        
        # 重定向到登录页面
        return redirect(url_for('login') + '?reset=success')
    
    # GET 请求
    theme = get_setting('theme', 'light')
    bg_type = get_setting('background_type', 'image')
    bg_image = get_setting('background_image', 'bg.png')
    bg_color = get_setting('background_color', '#667eea')
    
    return render_template('reset_password.html',
                         token=token,
                         theme=theme,
                         bg_type=bg_type,
                         bg_image=bg_image,
                         bg_color=bg_color)

# --- 路由：设置页面 ---
@app.route('/settings')
@login_required
def settings_page():
    # 获取主题和背景设置
    theme = get_setting('theme', 'light')
    bg_type = get_setting('background_type', 'image')
    bg_image = get_setting('background_image', 'bg.png')
    bg_color = get_setting('background_color', '#667eea')
    
    return render_template('settings.html',
                         theme=theme,
                         bg_type=bg_type,
                         bg_image=bg_image,
                         bg_color=bg_color,
                         version=VERSION,
                         author=AUTHOR,
                         github_url=GITHUB_URL,
                         bilibili_url=BILIBILI_URL,
                         project_url=PROJECT_URL)

# --- 路由：首页 (文件列表) ---
@app.route('/')
@login_required
def index():
    # 获取当前请求的相对路径，默认为根目录
    req_path = request.args.get('path', '')
    sort_by = request.args.get('sort', 'name')  # name, time, size
    sort_order = request.args.get('order', 'asc')  # asc, desc
    
    try:
        abs_path = get_safe_path(req_path)
    except:
        return "非法路径", 403

    files_list = []
    if os.path.isdir(abs_path):
        for item in os.listdir(abs_path):
            if item.startswith('.'): continue # 隐藏文件
            full_item_path = os.path.join(abs_path, item)
            is_dir = os.path.isdir(full_item_path)
            size = os.path.getsize(full_item_path) if not is_dir else 0
            size_bytes = size  # 保存原始字节数用于排序
            mtime_timestamp = os.path.getmtime(full_item_path)  # 保存时间戳用于排序
            # 转换时间
            mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime_timestamp))
            
            rel_path_item = os.path.join(req_path, item).replace('\\', '/')
            
            files_list.append({
                'name': item,
                'is_dir': is_dir,
                'size': f"{size/1024/1024:.2f} MB" if not is_dir else "-",
                'size_bytes': size_bytes,
                'mtime': mtime,
                'mtime_timestamp': mtime_timestamp,
                'rel_path': rel_path_item,
                'file_type': 'folder' if is_dir else get_file_type(item)
            })
    
    # 排序逻辑
    if sort_by == 'name':
        files_list.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
    elif sort_by == 'time':
        files_list.sort(key=lambda x: (not x['is_dir'], x['mtime_timestamp']))
    elif sort_by == 'size':
        files_list.sort(key=lambda x: (not x['is_dir'], x['size_bytes']))
    
    # 倒序
    if sort_order == 'desc':
        # 分离文件夹和文件
        folders = [f for f in files_list if f['is_dir']]
        files = [f for f in files_list if not f['is_dir']]
        # 分别倒序
        folders.reverse()
        files.reverse()
        files_list = folders + files
    
    # 获取主题和背景设置
    theme = get_setting('theme', 'light')
    bg_type = get_setting('background_type', 'image')
    bg_image = get_setting('background_image', 'bg.png')
    bg_color = get_setting('background_color', '#667eea')
    
    return render_template('index.html', 
                         files=files_list, 
                         current_path=req_path,
                         sort_by=sort_by,
                         sort_order=sort_order,
                         theme=theme,
                         bg_type=bg_type,
                         bg_image=bg_image,
                         bg_color=bg_color,
                         version=VERSION,
                         author=AUTHOR,
                         github_url=GITHUB_URL,
                         bilibili_url=BILIBILI_URL)

# --- 接口：操作 (重命名, 删除, 新建文件夹) ---
@app.route('/api/operate', methods=['POST'])
@login_required
def operate():
    data = request.json
    action = data.get('action')
    path = data.get('path') # 相对路径
    
    try:
        abs_path = get_safe_path(path)
        
        if action == 'mkdir':
            new_folder = data.get('name')
            # 不使用 secure_filename，因为它会移除中文字符
            # 只移除危险字符
            new_folder = new_folder.replace('..', '').replace('/', '').replace('\\', '')
            if not new_folder or new_folder.strip() == '':
                return jsonify({'status': 'error', 'msg': '文件夹名称无效'})
            
            new_folder_path = os.path.join(abs_path, new_folder)
            
            # 检查文件夹是否已存在
            if os.path.exists(new_folder_path):
                return jsonify({'status': 'error', 'msg': f'文件夹 "{new_folder}" 已存在'})
            
            os.mkdir(new_folder_path)
            
        elif action == 'delete':
            if os.path.isdir(abs_path):
                shutil.rmtree(abs_path)
            else:
                os.remove(abs_path)
                
        elif action == 'rename':
            new_name = data.get('new_name')
            # 不使用 secure_filename，保留中文
            new_name = new_name.replace('..', '').replace('/', '').replace('\\', '')
            if not new_name or new_name.strip() == '':
                return jsonify({'status': 'error', 'msg': '名称无效'})
            
            parent = os.path.dirname(abs_path)
            new_path = os.path.join(parent, new_name)
            
            # 检查目标名称是否已存在
            if os.path.exists(new_path):
                return jsonify({'status': 'error', 'msg': f'名称 "{new_name}" 已存在'})
            
            os.rename(abs_path, new_path)
            
        return jsonify({'status': 'success'})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': str(e)})

# --- 接口：复制/移动/粘贴 ---
@app.route('/api/paste', methods=['POST'])
@login_required
def paste():
    data = request.json
    src_path = data.get('src')
    dest_path = data.get('dest') # 目标文件夹
    action = data.get('action') # copy 或 move

    try:
        abs_src = get_safe_path(src_path)
        abs_dest_folder = get_safe_path(dest_path)
        
        # 检查源文件是否存在
        if not os.path.exists(abs_src):
            return jsonify({'status': 'error', 'msg': '源文件不存在'})
        
        # 检查目标文件夹是否存在
        if not os.path.exists(abs_dest_folder):
            return jsonify({'status': 'error', 'msg': '目标文件夹不存在'})
        
        filename = os.path.basename(abs_src)
        abs_dest_final = os.path.join(abs_dest_folder, filename)
        
        # 如果目标已存在，自动重命名
        if os.path.exists(abs_dest_final):
            base_name, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(abs_dest_final):
                new_filename = f"{base_name}_副本{counter}{ext}"
                abs_dest_final = os.path.join(abs_dest_folder, new_filename)
                counter += 1

        if action == 'copy':
            if os.path.isdir(abs_src):
                shutil.copytree(abs_src, abs_dest_final)
            else:
                shutil.copy2(abs_src, abs_dest_final)
        elif action == 'move':
            shutil.move(abs_src, abs_dest_final)
            
        return jsonify({'status': 'success'})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': f'操作失败: {str(e)}'})

# --- 接口：上传文件 ---
@app.route('/upload', methods=['POST'])
@login_required
def upload():
    current_path = request.form.get('path', '')
    file = request.files.get('file')
    relative_path = request.form.get('relativePath', '')
    
    try:
        save_dir = get_safe_path(current_path)
        
        if file:
            # 安全的文件名处理函数（保留中文）
            def safe_filename(filename):
                # 只移除危险字符，保留中文和其他字符
                dangerous_chars = ['..', '/', '\\', '\0', '<', '>', ':', '"', '|', '?', '*']
                for char in dangerous_chars:
                    filename = filename.replace(char, '_')
                return filename.strip()
            
            # 如果有相对路径（文件夹上传），保持目录结构
            if relative_path and '/' in relative_path:
                # 提取目录部分
                path_parts = relative_path.split('/')
                if len(path_parts) > 1:
                    # 创建子目录（保留中文目录名）
                    safe_parts = [safe_filename(part) for part in path_parts[:-1]]
                    sub_dir = os.path.join(save_dir, *safe_parts)
                    os.makedirs(sub_dir, exist_ok=True)
                    filename = safe_filename(path_parts[-1])
                    file.save(os.path.join(sub_dir, filename))
                else:
                    filename = safe_filename(relative_path)
                    file.save(os.path.join(save_dir, filename))
            else:
                # 普通文件上传
                filename = safe_filename(file.filename)
                file.save(os.path.join(save_dir, filename))
                
        return jsonify({'status': 'success'})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': str(e)})

# --- 接口：创建分享 ---
@app.route('/api/share', methods=['POST'])
@login_required
def create_share():
    data = request.json
    path = data.get('path')
    paths = data.get('paths')  # 批量分享
    minutes = int(data.get('minutes', 0)) # 0代表永久
    
    token = shortuuid.uuid()[:8] # 生成8位短链接
    expire_at = datetime.now() + timedelta(minutes=minutes) if minutes > 0 else None
    
    if paths and len(paths) > 0:
        # 批量分享：多个路径用 | 分隔
        file_path = '|'.join(paths)
        new_share = ShareLink(token=token, file_path=file_path, expire_at=expire_at, is_batch=True)
    else:
        # 单个分享
        new_share = ShareLink(token=token, file_path=path, expire_at=expire_at, is_batch=False)
    
    db.session.add(new_share)
    db.session.commit()
    
    share_url = request.host_url + 's/' + token
    return jsonify({'status': 'success', 'url': share_url})

# --- 辅助函数：打包文件夹为 ZIP ---
def zip_folder(folder_path, zip_name):
    """将文件夹打包为 ZIP 文件并返回文件路径"""
    # 使用时间戳和随机字符串避免文件名冲突
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    random_str = shortuuid.uuid()[:6]
    safe_zip_name = f"{zip_name}_{timestamp}_{random_str}.zip"
    zip_path = os.path.join(FOLDERZIP_DIR, safe_zip_name)
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, folder_path)
                try:
                    zipf.write(file_path, arcname)
                except Exception as e:
                    print(f"打包文件失败 {file_path}: {e}")
    
    return zip_path

# --- 路由：访问分享链接（显示详情页）---
@app.route('/s/<token>')
def access_share(token):
    link = ShareLink.query.filter_by(token=token).first()
    
    if not link:
        return "链接不存在或已失效", 404
        
    if link.expire_at and datetime.now() > link.expire_at:
        return "链接已过期", 403
    
    try:
        # 检查是否为批量分享
        if link.is_batch:
            # 批量分享 - 显示文件列表页面
            file_paths = link.file_path.split('|')
            file_count = len(file_paths)
            
            # 获取所有文件信息
            files = []
            total_size = 0
            
            for path in file_paths:
                try:
                    abs_path = get_safe_path(path)
                    if not os.path.exists(abs_path):
                        continue
                    
                    file_name = os.path.basename(abs_path)
                    is_dir = os.path.isdir(abs_path)
                    
                    if is_dir:
                        # 计算文件夹大小
                        dir_size = 0
                        for dirpath, dirnames, filenames in os.walk(abs_path):
                            for f in filenames:
                                fp = os.path.join(dirpath, f)
                                if os.path.exists(fp):
                                    dir_size += os.path.getsize(fp)
                        size_bytes = dir_size
                        file_type = 'folder'
                        type_text = '文件夹'
                    else:
                        size_bytes = os.path.getsize(abs_path)
                        file_type = get_file_type(file_name)
                        type_text = {
                            'image': '图片',
                            'video': '视频',
                            'audio': '音频',
                            'archive': '压缩包',
                            'pdf': 'PDF文档',
                            'office': 'Office文档',
                            'file': '文件'
                        }.get(file_type, '文件')
                    
                    # 格式化大小
                    if size_bytes < 1024:
                        size_str = f"{size_bytes} B"
                    elif size_bytes < 1024 * 1024:
                        size_str = f"{size_bytes/1024:.2f} KB"
                    else:
                        size_str = f"{size_bytes/1024/1024:.2f} MB"
                    
                    total_size += size_bytes
                    
                    files.append({
                        'name': file_name,
                        'path': path,
                        'size': size_str,
                        'is_dir': is_dir,
                        'file_type': file_type,
                        'type_text': type_text
                    })
                except Exception as e:
                    print(f"处理文件失败 {path}: {e}")
                    continue
            
            # 格式化总大小
            if total_size < 1024:
                total_size_str = f"{total_size} B"
            elif total_size < 1024 * 1024:
                total_size_str = f"{total_size/1024:.2f} KB"
            else:
                total_size_str = f"{total_size/1024/1024:.2f} MB"
            
            # 格式化时间
            created_at = link.created_at.strftime('%Y-%m-%d %H:%M')
            expire_at = link.expire_at.strftime('%Y-%m-%d %H:%M') if link.expire_at else None
            
            # 计算剩余时间
            time_remaining = None
            if link.expire_at:
                remaining = link.expire_at - datetime.now()
                if remaining.days > 0:
                    time_remaining = f"{remaining.days} 天"
                elif remaining.seconds > 3600:
                    time_remaining = f"{remaining.seconds // 3600} 小时"
                elif remaining.seconds > 60:
                    time_remaining = f"{remaining.seconds // 60} 分钟"
                else:
                    time_remaining = f"{remaining.seconds} 秒"
            
            # 获取主题和背景设置
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('batch_share.html',
                                 token=token,
                                 files=files,
                                 file_count=file_count,
                                 total_size=total_size_str,
                                 created_at=created_at,
                                 expire_at=expire_at,
                                 time_remaining=time_remaining,
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color)
        
        else:
            # 单个分享 - 显示原来的详情页
            abs_path = get_safe_path(link.file_path)
            
            if not os.path.exists(abs_path):
                return "文件源已被删除", 404
            
            # 获取文件信息
            file_name = os.path.basename(abs_path)
            is_dir = os.path.isdir(abs_path)
            
            if is_dir:
                file_type = 'folder'
                # 计算文件夹大小
                total_size = 0
                for dirpath, dirnames, filenames in os.walk(abs_path):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        if os.path.exists(fp):
                            total_size += os.path.getsize(fp)
                file_size = f"{total_size/1024/1024:.2f} MB"
            else:
                file_type = get_file_type(file_name)
                size_bytes = os.path.getsize(abs_path)
                if size_bytes < 1024:
                    file_size = f"{size_bytes} B"
                elif size_bytes < 1024 * 1024:
                    file_size = f"{size_bytes/1024:.2f} KB"
                else:
                    file_size = f"{size_bytes/1024/1024:.2f} MB"
            
            # 格式化时间
            created_at = link.created_at.strftime('%Y-%m-%d %H:%M')
            expire_at = link.expire_at.strftime('%Y-%m-%d %H:%M') if link.expire_at else None
            
            # 计算剩余时间
            time_remaining = None
            if link.expire_at:
                remaining = link.expire_at - datetime.now()
                if remaining.days > 0:
                    time_remaining = f"{remaining.days} 天"
                elif remaining.seconds > 3600:
                    time_remaining = f"{remaining.seconds // 3600} 小时"
                elif remaining.seconds > 60:
                    time_remaining = f"{remaining.seconds // 60} 分钟"
                else:
                    time_remaining = f"{remaining.seconds} 秒"
            
            # 获取主题和背景设置
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('share.html',
                                 token=token,
                                 file_name=file_name,
                                 file_type=file_type,
                                 file_size=file_size,
                                 created_at=created_at,
                                 expire_at=expire_at,
                                 time_remaining=time_remaining,
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color,
                                 is_batch=link.is_batch)
    except Exception as e:
        print(f"分享页面加载失败: {e}")
        import traceback
        traceback.print_exc()
        return "加载失败", 500

# --- 路由：分享文件下载 ---
@app.route('/share-download/<token>')
def share_download(token):
    link = ShareLink.query.filter_by(token=token).first()
    
    if not link:
        return "链接不存在或已失效", 404
        
    if link.expire_at and datetime.now() > link.expire_at:
        return "链接已过期", 403
        
    # 下载文件
    try:
        if link.is_batch:
            # 批量下载：打包成 ZIP
            file_paths = link.file_path.split('|')
            
            # 创建临时 ZIP 文件
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            random_str = shortuuid.uuid()[:6]
            zip_name = f"batch_share_{timestamp}_{random_str}.zip"
            zip_path = os.path.join(FOLDERZIP_DIR, zip_name)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for path in file_paths:
                    try:
                        abs_path = get_safe_path(path)
                        if not os.path.exists(abs_path):
                            continue
                        
                        if os.path.isdir(abs_path):
                            # 添加文件夹
                            folder_name = os.path.basename(abs_path)
                            for root, dirs, files in os.walk(abs_path):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    arcname = os.path.join(folder_name, os.path.relpath(file_path, abs_path))
                                    zipf.write(file_path, arcname)
                        else:
                            # 添加文件
                            zipf.write(abs_path, os.path.basename(abs_path))
                    except Exception as e:
                        print(f"打包文件失败 {path}: {e}")
            
            return send_file(
                zip_path,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f"批量分享_{timestamp}.zip"
            )
        else:
            # 单个文件下载
            abs_path = get_safe_path(link.file_path)
            if os.path.isdir(abs_path):
                # 如果是文件夹，打包为 ZIP 下载
                folder_name = os.path.basename(abs_path)
                zip_path = zip_folder(abs_path, folder_name)
                
                return send_file(
                    zip_path,
                    mimetype='application/zip',
                    as_attachment=True,
                    download_name=f"{folder_name}.zip"
                )
            return send_file(abs_path, as_attachment=True)
    except Exception as e:
        print(f"分享下载失败: {e}")
        import traceback
        traceback.print_exc()
        return "文件源已被删除或下载失败", 404

# --- 路由：批量分享中的单个文件下载 ---
@app.route('/share-download-single/<token>/<int:index>')
def share_download_single(token, index):
    link = ShareLink.query.filter_by(token=token).first()
    
    if not link:
        return "链接不存在或已失效", 404
        
    if link.expire_at and datetime.now() > link.expire_at:
        return "链接已过期", 403
    
    if not link.is_batch:
        return "此链接不是批量分享", 400
    
    try:
        file_paths = link.file_path.split('|')
        
        if index < 0 or index >= len(file_paths):
            return "文件索引无效", 400
        
        path = file_paths[index]
        abs_path = get_safe_path(path)
        
        if not os.path.exists(abs_path):
            return "文件不存在或已被删除", 404
        
        if os.path.isdir(abs_path):
            # 如果是文件夹，打包为 ZIP 下载
            folder_name = os.path.basename(abs_path)
            zip_path = zip_folder(abs_path, folder_name)
            
            return send_file(
                zip_path,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f"{folder_name}.zip"
            )
        else:
            # 直接下载文件
            return send_file(abs_path, as_attachment=True)
    except Exception as e:
        print(f"单个文件下载失败: {e}")
        import traceback
        traceback.print_exc()
        return "下载失败", 500

# --- 路由：分享文件预览 ---
@app.route('/share-preview/<token>')
def share_preview(token):
    link = ShareLink.query.filter_by(token=token).first()
    
    if not link:
        return "链接不存在或已失效", 404
        
    if link.expire_at and datetime.now() > link.expire_at:
        return "链接已过期", 403
    
    try:
        abs_path = get_safe_path(link.file_path)
        
        if not os.path.isfile(abs_path):
            return "文件不存在", 404
        
        filename = os.path.basename(abs_path)
        file_type = get_file_type(filename)
        
        if file_type not in ['image', 'video', 'audio', 'office', 'pdf']:
            return "此文件类型不支持预览", 400
        
        # Office 文档和 PDF 使用新的预览页面
        if file_type in ['office', 'pdf']:
            # 生成可访问的文件 URL
            file_url = request.host_url + 'share-file/' + token
            from urllib.parse import quote
            file_url = quote(file_url, safe=':/?&=')
            
            return render_template('document_preview.html', 
                                 file_path=link.file_path, 
                                 file_name=filename,
                                 file_type=file_type,
                                 file_url=file_url)
        
        # 图片、视频和音频使用原来的预览页面
        return render_template('preview.html', 
                             file_path=link.file_path, 
                             file_name=filename,
                             file_type=file_type)
    except Exception as e:
        print(f"预览失败: {e}")
        return "预览失败", 500

# --- 路由：分享文件内容（用于预览）---
@app.route('/share-file/<token>')
def share_file(token):
    link = ShareLink.query.filter_by(token=token).first()
    
    if not link:
        abort(404)
        
    if link.expire_at and datetime.now() > link.expire_at:
        abort(403)
    
    try:
        abs_path = get_safe_path(link.file_path)
        
        if not os.path.isfile(abs_path):
            abort(404)
        
        filename = os.path.basename(abs_path)
        file_type = get_file_type(filename)
        
        if file_type == 'pdf':
            return send_file(abs_path, mimetype='application/pdf')
        elif file_type == 'office':
            ext = os.path.splitext(filename.lower())[1]
            mime_types = {
                '.doc': 'application/msword',
                '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                '.xls': 'application/vnd.ms-excel',
                '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                '.ppt': 'application/vnd.ms-powerpoint',
                '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
            }
            return send_file(abs_path, mimetype=mime_types.get(ext, 'application/octet-stream'))
        else:
            abort(404)
    except:
        abort(404)

# --- 路由：分享文件缩略图 ---
@app.route('/share-thumbnail/<token>')
def share_thumbnail(token):
    link = ShareLink.query.filter_by(token=token).first()
    
    if not link:
        abort(404)
        
    if link.expire_at and datetime.now() > link.expire_at:
        abort(403)
    
    try:
        abs_path = get_safe_path(link.file_path)
        
        if not os.path.isfile(abs_path):
            abort(404)
        
        # 只为图片生成缩略图
        if is_image(abs_path):
            try:
                img = Image.open(abs_path)
                # 转换 RGBA 到 RGB
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = background
                
                # 生成缩略图
                img.thumbnail((400, 400), Image.Resampling.LANCZOS)
                
                # 保存到内存
                img_io = io.BytesIO()
                img.save(img_io, 'JPEG', quality=85)
                img_io.seek(0)
                
                return send_file(img_io, mimetype='image/jpeg')
            except Exception as e:
                print(f"缩略图生成失败: {e}")
                abort(404)
        else:
            abort(404)
    except:
        abort(404)

# --- 路由：生成缩略图 ---
@app.route('/thumbnail')
@login_required
def thumbnail():
    path = request.args.get('path')
    try:
        abs_path = get_safe_path(path)
        
        if not os.path.isfile(abs_path):
            abort(404)
        
        # 只为图片生成缩略图
        if is_image(abs_path):
            try:
                img = Image.open(abs_path)
                # 转换 RGBA 到 RGB
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = background
                
                # 生成缩略图
                img.thumbnail((200, 200), Image.Resampling.LANCZOS)
                
                # 保存到内存
                img_io = io.BytesIO()
                img.save(img_io, 'JPEG', quality=85)
                img_io.seek(0)
                
                return send_file(img_io, mimetype='image/jpeg')
            except Exception as e:
                print(f"缩略图生成失败: {e}")
                abort(404)
        else:
            abort(404)
    except:
        abort(404)

# --- 路由：查看压缩包内容 ---
@app.route('/archive-view')
@login_required
def archive_view():
    path = request.args.get('path')
    try:
        abs_path = get_safe_path(path)
        if not os.path.isfile(abs_path):
            abort(404)
        
        if not is_archive(abs_path):
            abort(404)
        
        filename = os.path.basename(abs_path)
        
        # 获取主题和背景设置
        theme = get_setting('theme', 'light')
        bg_type = get_setting('background_type', 'image')
        bg_image = get_setting('background_image', 'bg.png')
        bg_color = get_setting('background_color', '#667eea')
        
        return render_template('archive_view.html',
                             archive_path=path,
                             archive_name=filename,
                             theme=theme,
                             bg_type=bg_type,
                             bg_image=bg_image,
                             bg_color=bg_color)
    except:
        abort(404)

# --- 路由：从压缩包下载单个文件 ---
@app.route('/download-from-archive', methods=['POST'])
@login_required
def download_from_archive():
    archive_path = request.form.get('archive_path')
    file_name = request.form.get('file_name')
    
    try:
        abs_archive_path = get_safe_path(archive_path)
        
        if not os.path.exists(abs_archive_path):
            return "压缩包不存在", 404
        
        file_ext = abs_archive_path.lower()
        
        # 创建临时目录
        import tempfile
        temp_dir = tempfile.mkdtemp()
        
        if file_ext.endswith('.zip'):
            import zipfile
            with zipfile.ZipFile(abs_archive_path, 'r') as zip_ref:
                # 查找并提取文件
                for file_info in zip_ref.filelist:
                    try:
                        filename = file_info.filename.encode('cp437').decode('utf-8')
                    except:
                        try:
                            filename = file_info.filename.encode('cp437').decode('gbk')
                        except:
                            filename = file_info.filename
                    
                    if filename == file_name:
                        extracted_path = zip_ref.extract(file_info, temp_dir)
                        
                        @after_this_request
                        def cleanup(response):
                            try:
                                shutil.rmtree(temp_dir)
                            except:
                                pass
                            return response
                        
                        return send_file(extracted_path, as_attachment=True, download_name=os.path.basename(file_name))
                        
        elif file_ext.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')):
            import tarfile
            with tarfile.open(abs_archive_path, 'r:*') as tar_ref:
                member = tar_ref.getmember(file_name)
                tar_ref.extract(member, temp_dir)
                extracted_path = os.path.join(temp_dir, file_name)
                
                @after_this_request
                def cleanup(response):
                    try:
                        shutil.rmtree(temp_dir)
                    except:
                        pass
                    return response
                
                return send_file(extracted_path, as_attachment=True, download_name=os.path.basename(file_name))
        
        elif file_ext.endswith('.rar'):
            import rarfile
            with rarfile.RarFile(abs_archive_path, 'r') as rar_ref:
                # 提取文件
                rar_ref.extract(file_name, temp_dir)
                extracted_path = os.path.join(temp_dir, file_name)
                
                @after_this_request
                def cleanup(response):
                    try:
                        shutil.rmtree(temp_dir)
                    except:
                        pass
                    return response
                
                return send_file(extracted_path, as_attachment=True, download_name=os.path.basename(file_name))
                
        elif file_ext.endswith('.7z'):
            import py7zr
            with py7zr.SevenZipFile(abs_archive_path, 'r') as sz_ref:
                # 提取特定文件
                sz_ref.extract(temp_dir, [file_name])
                extracted_path = os.path.join(temp_dir, file_name)
                
                @after_this_request
                def cleanup(response):
                    try:
                        shutil.rmtree(temp_dir)
                    except:
                        pass
                    return response
                
                return send_file(extracted_path, as_attachment=True, download_name=os.path.basename(file_name))
        
        return "文件未找到", 404
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"下载失败: {str(e)}", 500

# --- 辅助函数：将 Word 文档转换为 HTML ---
def convert_docx_to_html(docx_path):
    try:
        from docx import Document
        from docx.shared import Inches
        
        doc = Document(docx_path)
        html_content = ['<div class="document-content">']
        
        # 处理段落
        for para in doc.paragraphs:
            if para.text.strip():
                style = ''
                if para.style.name.startswith('Heading'):
                    level = para.style.name.replace('Heading ', '')
                    html_content.append(f'<h{level}>{para.text}</h{level}>')
                else:
                    # 处理段落格式
                    if para.alignment:
                        align_map = {0: 'left', 1: 'center', 2: 'right', 3: 'justify'}
                        style = f'text-align: {align_map.get(para.alignment, "left")};'
                    html_content.append(f'<p style="{style}">{para.text}</p>')
        
        # 处理表格
        for table in doc.tables:
            html_content.append('<table class="table table-bordered">')
            for row in table.rows:
                html_content.append('<tr>')
                for cell in row.cells:
                    html_content.append(f'<td>{cell.text}</td>')
                html_content.append('</tr>')
            html_content.append('</table>')
        
        html_content.append('</div>')
        return '\n'.join(html_content)
    except Exception as e:
        return f'<div class="alert alert-danger">Word 文档解析失败: {str(e)}</div>'

# --- 辅助函数：将 Excel 表格转换为 HTML ---
def convert_xlsx_to_html(xlsx_path):
    try:
        from openpyxl import load_workbook
        
        wb = load_workbook(xlsx_path, data_only=True)
        html_content = ['<div class="document-content">']
        
        # 处理所有工作表
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            html_content.append(f'<h3>工作表: {sheet_name}</h3>')
            html_content.append('<div class="table-responsive">')
            html_content.append('<table class="table table-bordered table-striped">')
            
            # 获取有数据的区域
            max_row = sheet.max_row
            max_col = sheet.max_column
            
            # 限制显示行数，避免过大
            display_rows = min(max_row, 1000)
            
            for row_idx, row in enumerate(sheet.iter_rows(max_row=display_rows, max_col=max_col), 1):
                html_content.append('<tr>')
                for cell in row:
                    value = cell.value if cell.value is not None else ''
                    # 第一行作为表头
                    tag = 'th' if row_idx == 1 else 'td'
                    html_content.append(f'<{tag}>{value}</{tag}>')
                html_content.append('</tr>')
            
            if max_row > display_rows:
                html_content.append(f'<tr><td colspan="{max_col}" class="text-center text-muted">... 还有 {max_row - display_rows} 行未显示 ...</td></tr>')
            
            html_content.append('</table>')
            html_content.append('</div>')
        
        html_content.append('</div>')
        return '\n'.join(html_content)
    except Exception as e:
        return f'<div class="alert alert-danger">Excel 表格解析失败: {str(e)}</div>'

# --- 辅助函数：将 PPT 转换为 HTML ---
def convert_pptx_to_html(pptx_path):
    try:
        from pptx import Presentation
        
        prs = Presentation(pptx_path)
        html_content = ['<div class="document-content presentation">']
        
        for slide_idx, slide in enumerate(prs.slides, 1):
            html_content.append(f'<div class="slide" id="slide-{slide_idx}">')
            html_content.append(f'<h3>幻灯片 {slide_idx}</h3>')
            html_content.append('<div class="slide-content">')
            
            # 提取文本内容
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    # 判断是否为标题
                    if hasattr(shape, "is_placeholder") and shape.is_placeholder:
                        html_content.append(f'<h4>{shape.text}</h4>')
                    else:
                        html_content.append(f'<p>{shape.text}</p>')
                
                # 处理表格
                if shape.has_table:
                    html_content.append('<table class="table table-bordered">')
                    for row in shape.table.rows:
                        html_content.append('<tr>')
                        for cell in row.cells:
                            html_content.append(f'<td>{cell.text}</td>')
                        html_content.append('</tr>')
                    html_content.append('</table>')
            
            html_content.append('</div>')
            html_content.append('</div>')
        
        html_content.append('</div>')
        return '\n'.join(html_content)
    except Exception as e:
        return f'<div class="alert alert-danger">PPT 文档解析失败: {str(e)}</div>'

# --- 路由：预览文件 ---
@app.route('/preview')
@login_required
def preview():
    path = request.args.get('path')
    try:
        abs_path = get_safe_path(path)
        if not os.path.isfile(abs_path):
            abort(404)
        
        filename = os.path.basename(abs_path)
        file_type = get_file_type(filename)
        
        # Office 文档转换为 HTML 预览
        if file_type == 'office':
            file_ext = os.path.splitext(filename.lower())[1]
            html_content = ''
            
            if file_ext in ['.docx', '.doc']:
                html_content = convert_docx_to_html(abs_path)
            elif file_ext in ['.xlsx', '.xls']:
                html_content = convert_xlsx_to_html(abs_path)
            elif file_ext in ['.pptx', '.ppt']:
                html_content = convert_pptx_to_html(abs_path)
            
            # 获取主题和背景设置
            theme = get_setting('theme', 'light')
            bg_type = get_setting('background_type', 'image')
            bg_image = get_setting('background_image', 'bg.png')
            bg_color = get_setting('background_color', '#667eea')
            
            return render_template('office_preview.html',
                                 file_path=path,
                                 file_name=filename,
                                 file_type=file_type,
                                 html_content=html_content,
                                 theme=theme,
                                 bg_type=bg_type,
                                 bg_image=bg_image,
                                 bg_color=bg_color)
        
        # PDF 使用文档预览页面
        elif file_type == 'pdf':
            return render_template('document_preview.html', 
                                 file_path=path, 
                                 file_name=filename,
                                 file_type=file_type,
                                 file_url='')
        
        # 图片、视频和音频使用原来的预览页面
        return render_template('preview.html', 
                             file_path=path, 
                             file_name=filename,
                             file_type=file_type)
    except:
        abort(404)

# --- 路由：获取文件内容（用于预览） ---
@app.route('/file')
@login_required
def get_file():
    path = request.args.get('path')
    try:
        abs_path = get_safe_path(path)
        if not os.path.isfile(abs_path):
            abort(404)
        
        # 获取文件的 MIME 类型
        filename = os.path.basename(abs_path)
        file_type = get_file_type(filename)
        
        if file_type == 'image':
            return send_file(abs_path)
        elif file_type == 'video':
            return send_file(abs_path)
        elif file_type == 'audio':
            return send_file(abs_path)
        elif file_type == 'pdf':
            return send_file(abs_path, mimetype='application/pdf')
        elif file_type == 'office':
            # Office 文档直接发送
            ext = os.path.splitext(filename.lower())[1]
            mime_types = {
                '.doc': 'application/msword',
                '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                '.xls': 'application/vnd.ms-excel',
                '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                '.ppt': 'application/vnd.ms-powerpoint',
                '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
            }
            return send_file(abs_path, mimetype=mime_types.get(ext, 'application/octet-stream'))
        else:
            abort(404)
    except:
        abort(404)

# --- 路由：下载普通文件或文件夹 ---
@app.route('/download')
@login_required
def download():
    path = request.args.get('path')
    try:
        abs_path = get_safe_path(path)
        if os.path.isdir(abs_path):
            # 如果是文件夹，打包为 ZIP 下载
            folder_name = os.path.basename(abs_path) or 'storage'
            zip_path = zip_folder(abs_path, folder_name)
            
            # 不再下载后删除，保留24小时
            return send_file(
                zip_path,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f"{folder_name}.zip"
            )
        return send_file(abs_path, as_attachment=True)
    except Exception as e:
        print(f"下载失败: {e}")
        import traceback
        traceback.print_exc()
        return "文件不存在或下载失败", 404

# --- 接口：修改密码 ---
@app.route('/api/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.json
    old_password = data.get('old_password')
    new_password = data.get('new_password')
    
    if not old_password or not new_password:
        return jsonify({'status': 'error', 'msg': '密码不能为空'})
    
    if not verify_password(old_password):
        return jsonify({'status': 'error', 'msg': '原密码错误'})
    
    if len(new_password) < 6:
        return jsonify({'status': 'error', 'msg': '新密码至少6位'})
    
    # 更新密码
    new_hash = generate_password_hash(new_password)
    set_setting('password_hash', new_hash)
    
    return jsonify({'status': 'success', 'msg': '密码修改成功'})

# --- 接口：切换主题 ---
@app.route('/api/toggle-theme', methods=['POST'])
@login_required
def toggle_theme():
    data = request.json
    theme = data.get('theme', 'light')
    
    if theme not in ['light', 'dark']:
        return jsonify({'status': 'error', 'msg': '无效的主题'})
    
    set_setting('theme', theme)
    return jsonify({'status': 'success', 'theme': theme})

# --- 接口：设置安全问题 ---
@app.route('/api/set-security-question', methods=['POST'])
@login_required
def set_security_question():
    data = request.json
    question = data.get('question', '').strip()
    answer = data.get('answer', '').strip()
    
    if not question or not answer:
        return jsonify({'status': 'error', 'msg': '问题和答案不能为空'})
    
    if len(answer) < 2:
        return jsonify({'status': 'error', 'msg': '答案至少需要2个字符'})
    
    set_setting('security_question', question)
    set_setting('security_answer', answer)
    
    return jsonify({'status': 'success', 'msg': '安全问题设置成功'})

# --- 接口：更新背景设置 ---
@app.route('/api/update-background', methods=['POST'])
@login_required
def update_background():
    data = request.json
    bg_type = data.get('type', 'image')  # image 或 color
    bg_value = data.get('value', '')
    
    if bg_type == 'color':
        set_setting('background_type', 'color')
        set_setting('background_color', bg_value)
    elif bg_type == 'image':
        set_setting('background_type', 'image')
        set_setting('background_image', bg_value)
    else:
        return jsonify({'status': 'error', 'msg': '无效的背景类型'})
    
    return jsonify({'status': 'success'})

# --- 接口：上传背景图片 ---
@app.route('/api/upload-background', methods=['POST'])
@login_required
def upload_background():
    file = request.files.get('file')
    
    if not file:
        return jsonify({'status': 'error', 'msg': '没有文件'})
    
    # 检查文件类型
    if not file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
        return jsonify({'status': 'error', 'msg': '只支持图片格式'})
    
    try:
        # 保存文件
        filename = f"bg_{int(time.time())}.{file.filename.rsplit('.', 1)[1]}"
        filepath = os.path.join(STATIC_DIR, filename)
        file.save(filepath)
        
        # 更新设置
        set_setting('background_type', 'image')
        set_setting('background_image', filename)
        
        return jsonify({'status': 'success', 'filename': filename})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

# --- 路由：获取当前设置 ---
@app.route('/api/get-settings', methods=['GET'])
@login_required
def get_settings():
    return jsonify({
        'theme': get_setting('theme', 'light'),
        'background_type': get_setting('background_type', 'image'),
        'background_image': get_setting('background_image', 'bg.png'),
        'background_color': get_setting('background_color', '#667eea')
    })

# --- 接口：查看压缩包内容 ---
@app.route('/api/archive-content', methods=['POST'])
@login_required
def get_archive_content():
    data = request.json
    file_path = data.get('path')
    
    try:
        abs_file_path = get_safe_path(file_path)
        
        if not os.path.exists(abs_file_path):
            return jsonify({'status': 'error', 'msg': '文件不存在'})
        
        if not os.path.isfile(abs_file_path):
            return jsonify({'status': 'error', 'msg': '不是有效的文件'})
        
        if not is_archive(abs_file_path):
            return jsonify({'status': 'error', 'msg': '不是压缩包文件'})
        
        file_list = []
        file_ext = abs_file_path.lower()
        
        if file_ext.endswith('.zip'):
            import zipfile
            with zipfile.ZipFile(abs_file_path, 'r') as zip_ref:
                for file_info in zip_ref.filelist:
                    try:
                        # 尝试解码文件名
                        try:
                            filename = file_info.filename.encode('cp437').decode('utf-8')
                        except:
                            try:
                                filename = file_info.filename.encode('cp437').decode('gbk')
                            except:
                                filename = file_info.filename
                        
                        # 跳过空文件夹（只有路径分隔符的条目）
                        if not filename or filename.strip('/') == '':
                            continue
                        
                        is_dir = filename.endswith('/')
                        size = file_info.file_size if not is_dir else 0
                        
                        # 跳过大小为0的目录条目（空文件夹）
                        if is_dir and size == 0:
                            continue
                        
                        # 格式化大小
                        if size < 1024:
                            size_str = f"{size} B"
                        elif size < 1024 * 1024:
                            size_str = f"{size/1024:.2f} KB"
                        else:
                            size_str = f"{size/1024/1024:.2f} MB"
                        
                        file_list.append({
                            'name': filename.rstrip('/'),  # 移除末尾的斜杠
                            'size': size_str,
                            'size_bytes': size,
                            'is_dir': is_dir,
                            'compressed_size': file_info.compress_size
                        })
                    except Exception as e:
                        print(f"处理文件信息失败: {e}")
                        continue
                        
        elif file_ext.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')):
            import tarfile
            with tarfile.open(abs_file_path, 'r:*') as tar_ref:
                for member in tar_ref.getmembers():
                    # 跳过空文件夹
                    if not member.name or member.name.strip('/') == '':
                        continue
                    
                    is_dir = member.isdir()
                    size = member.size if not is_dir else 0
                    
                    # 跳过大小为0的目录条目（空文件夹）
                    if is_dir and size == 0:
                        continue
                    
                    # 格式化大小
                    if size < 1024:
                        size_str = f"{size} B"
                    elif size < 1024 * 1024:
                        size_str = f"{size/1024:.2f} KB"
                    else:
                        size_str = f"{size/1024/1024:.2f} MB"
                    
                    file_list.append({
                        'name': member.name.rstrip('/'),
                        'size': size_str,
                        'size_bytes': size,
                        'is_dir': is_dir,
                        'compressed_size': 0
                    })
                    
        elif file_ext.endswith('.rar'):
            try:
                import rarfile
                with rarfile.RarFile(abs_file_path, 'r') as rar_ref:
                    for file_info in rar_ref.infolist():
                        # 跳过空文件夹
                        if not file_info.filename or file_info.filename.strip('/') == '':
                            continue
                        
                        is_dir = file_info.isdir()
                        size = file_info.file_size if not is_dir else 0
                        
                        # 跳过大小为0的目录条目（空文件夹）
                        if is_dir and size == 0:
                            continue
                        
                        # 格式化大小
                        if size < 1024:
                            size_str = f"{size} B"
                        elif size < 1024 * 1024:
                            size_str = f"{size/1024:.2f} KB"
                        else:
                            size_str = f"{size/1024/1024:.2f} MB"
                        
                        file_list.append({
                            'name': file_info.filename.rstrip('/'),
                            'size': size_str,
                            'size_bytes': size,
                            'is_dir': is_dir,
                            'compressed_size': file_info.compress_size
                        })
            except ImportError:
                return jsonify({'status': 'error', 'msg': 'RAR 格式需要安装 rarfile 库，请运行: pip install rarfile'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': f'RAR 文件读取失败: {str(e)}'})
                
        elif file_ext.endswith('.7z'):
            try:
                import py7zr
                with py7zr.SevenZipFile(abs_file_path, 'r') as sz_ref:
                    for name, file_info in sz_ref.list():
                        # 跳过空文件夹
                        if not name or name.strip('/') == '':
                            continue
                        
                        is_dir = file_info.is_directory
                        size = file_info.uncompressed if not is_dir else 0
                        
                        # 跳过大小为0的目录条目（空文件夹）
                        if is_dir and size == 0:
                            continue
                        
                        # 格式化大小
                        if size < 1024:
                            size_str = f"{size} B"
                        elif size < 1024 * 1024:
                            size_str = f"{size/1024:.2f} KB"
                        else:
                            size_str = f"{size/1024/1024:.2f} MB"
                        
                        file_list.append({
                            'name': name.rstrip('/'),
                            'size': size_str,
                            'size_bytes': size,
                            'is_dir': is_dir,
                            'compressed_size': file_info.compressed if hasattr(file_info, 'compressed') else 0
                        })
            except ImportError:
                return jsonify({'status': 'error', 'msg': '7Z 格式需要安装 py7zr 库，请运行: pip install py7zr'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': f'7Z 文件读取失败: {str(e)}'})
                
        else:
            return jsonify({'status': 'error', 'msg': '不支持查看此压缩格式的内容'})
        
        # 过滤掉重复的条目（有些压缩包会同时包含文件和其父目录）
        seen_names = set()
        filtered_list = []
        for item in file_list:
            if item['name'] not in seen_names:
                seen_names.add(item['name'])
                filtered_list.append(item)
        
        file_list = filtered_list
        
        # 计算总大小和文件数量
        total_size = sum(f['size_bytes'] for f in file_list if not f['is_dir'])
        file_count = len([f for f in file_list if not f['is_dir']])
        folder_count = len([f for f in file_list if f['is_dir']])
        
        if total_size < 1024:
            total_size_str = f"{total_size} B"
        elif total_size < 1024 * 1024:
            total_size_str = f"{total_size/1024:.2f} KB"
        else:
            total_size_str = f"{total_size/1024/1024:.2f} MB"
        
        return jsonify({
            'status': 'success',
            'files': file_list,
            'total_size': total_size_str,
            'file_count': file_count,
            'folder_count': folder_count
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': f'读取失败: {str(e)}'})

# --- 接口：从压缩包中提取单个文件 ---
@app.route('/api/extract-file', methods=['POST'])
@login_required
def extract_single_file():
    data = request.json
    archive_path = data.get('archive_path')
    file_name = data.get('file_name')
    
    try:
        abs_archive_path = get_safe_path(archive_path)
        
        if not os.path.exists(abs_archive_path):
            return jsonify({'status': 'error', 'msg': '压缩包不存在'})
        
        file_ext = abs_archive_path.lower()
        
        # 创建临时文件
        import tempfile
        temp_dir = tempfile.mkdtemp()
        
        if file_ext.endswith('.zip'):
            import zipfile
            with zipfile.ZipFile(abs_archive_path, 'r') as zip_ref:
                # 查找文件
                for file_info in zip_ref.filelist:
                    try:
                        filename = file_info.filename.encode('cp437').decode('utf-8')
                    except:
                        try:
                            filename = file_info.filename.encode('cp437').decode('gbk')
                        except:
                            filename = file_info.filename
                    
                    if filename == file_name:
                        # 提取文件到临时目录
                        extracted_path = zip_ref.extract(file_info, temp_dir)
                        
                        # 返回文件路径供下载
                        return jsonify({
                            'status': 'success',
                            'temp_path': extracted_path,
                            'file_name': os.path.basename(file_name)
                        })
                        
        elif file_ext.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')):
            import tarfile
            with tarfile.open(abs_archive_path, 'r:*') as tar_ref:
                member = tar_ref.getmember(file_name)
                tar_ref.extract(member, temp_dir)
                extracted_path = os.path.join(temp_dir, file_name)
                
                return jsonify({
                    'status': 'success',
                    'temp_path': extracted_path,
                    'file_name': os.path.basename(file_name)
                })
        
        return jsonify({'status': 'error', 'msg': '文件未找到'})
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': f'提取失败: {str(e)}'})

# --- 接口：解压压缩包 ---
@app.route('/api/extract', methods=['POST'])
@login_required
def extract_archive():
    data = request.json
    file_path = data.get('path')  # 压缩包路径
    extract_to = data.get('extract_to', '')  # 解压到的目标文件夹，默认为当前目录
    
    try:
        abs_file_path = get_safe_path(file_path)
        
        # 检查文件是否存在
        if not os.path.exists(abs_file_path):
            return jsonify({'status': 'error', 'msg': '文件不存在'})
        
        if not os.path.isfile(abs_file_path):
            return jsonify({'status': 'error', 'msg': '不是有效的文件'})
        
        # 检查是否为压缩包
        if not is_archive(abs_file_path):
            return jsonify({'status': 'error', 'msg': '不是支持的压缩包格式'})
        
        # 确定解压目标目录
        if extract_to:
            abs_extract_dir = get_safe_path(extract_to)
        else:
            # 默认解压到压缩包所在目录
            abs_extract_dir = os.path.dirname(abs_file_path)
        
        # 创建以压缩包名称命名的文件夹
        archive_name = os.path.splitext(os.path.basename(abs_file_path))[0]
        # 处理 .tar.gz 等双扩展名
        if archive_name.endswith('.tar'):
            archive_name = os.path.splitext(archive_name)[0]
        
        extract_folder = os.path.join(abs_extract_dir, archive_name)
        
        # 如果目标文件夹已存在，添加数字后缀
        counter = 1
        original_extract_folder = extract_folder
        while os.path.exists(extract_folder):
            extract_folder = f"{original_extract_folder}_{counter}"
            counter += 1
        
        os.makedirs(extract_folder, exist_ok=True)
        
        # 根据文件类型解压
        file_ext = abs_file_path.lower()
        
        if file_ext.endswith('.zip'):
            # 解压 ZIP 文件
            import zipfile
            with zipfile.ZipFile(abs_file_path, 'r') as zip_ref:
                # 处理中文文件名编码问题
                for file_info in zip_ref.filelist:
                    try:
                        # 尝试使用 UTF-8 解码
                        file_info.filename = file_info.filename.encode('cp437').decode('utf-8')
                    except:
                        try:
                            # 尝试使用 GBK 解码
                            file_info.filename = file_info.filename.encode('cp437').decode('gbk')
                        except:
                            pass
                zip_ref.extractall(extract_folder)
                
        elif file_ext.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')):
            # 解压 TAR 文件
            import tarfile
            with tarfile.open(abs_file_path, 'r:*') as tar_ref:
                tar_ref.extractall(extract_folder)
                
        elif file_ext.endswith('.gz') and not file_ext.endswith('.tar.gz'):
            # 解压单个 GZ 文件
            import gzip
            output_file = os.path.join(extract_folder, os.path.splitext(os.path.basename(abs_file_path))[0])
            with gzip.open(abs_file_path, 'rb') as f_in:
                with open(output_file, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
                    
        elif file_ext.endswith('.bz2') and not file_ext.endswith('.tar.bz2'):
            # 解压单个 BZ2 文件
            import bz2
            output_file = os.path.join(extract_folder, os.path.splitext(os.path.basename(abs_file_path))[0])
            with bz2.open(abs_file_path, 'rb') as f_in:
                with open(output_file, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
                    
        elif file_ext.endswith('.rar'):
            # 解压 RAR 文件
            try:
                import rarfile
                with rarfile.RarFile(abs_file_path, 'r') as rar_ref:
                    rar_ref.extractall(extract_folder)
            except ImportError:
                return jsonify({'status': 'error', 'msg': 'RAR 格式需要安装 rarfile 库，请运行: pip install rarfile'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': f'RAR 解压失败: {str(e)}'})
            
        elif file_ext.endswith('.7z'):
            # 解压 7Z 文件
            try:
                import py7zr
                with py7zr.SevenZipFile(abs_file_path, 'r') as sz_ref:
                    sz_ref.extractall(extract_folder)
            except ImportError:
                return jsonify({'status': 'error', 'msg': '7Z 格式需要安装 py7zr 库，请运行: pip install py7zr'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': f'7Z 解压失败: {str(e)}'})
            
        else:
            return jsonify({'status': 'error', 'msg': '不支持的压缩格式'})
        
        return jsonify({
            'status': 'success', 
            'msg': f'解压成功，文件已解压到: {os.path.basename(extract_folder)}'
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': f'解压失败: {str(e)}'})

# --- 接口：清空缓存 ---
@app.route('/api/clear-cache', methods=['POST'])
@login_required
def clear_cache():
    try:
        # 清空 folderzip 目录中的所有 ZIP 文件
        deleted_count = 0
        if os.path.exists(FOLDERZIP_DIR):
            for filename in os.listdir(FOLDERZIP_DIR):
                filepath = os.path.join(FOLDERZIP_DIR, filename)
                if os.path.isfile(filepath) and filename.endswith('.zip'):
                    os.remove(filepath)
                    deleted_count += 1
        
        # 清空旧的背景图片（保留当前使用的）
        current_bg = get_setting('background_image', 'bg.png')
        if os.path.exists(STATIC_DIR):
            for filename in os.listdir(STATIC_DIR):
                if filename.startswith('bg_') and filename != current_bg:
                    filepath = os.path.join(STATIC_DIR, filename)
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                        deleted_count += 1
        
        return jsonify({
            'status': 'success', 
            'msg': f'已清空缓存，删除了 {deleted_count} 个临时文件'
        })
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

# --- 接口：清空所有数据 ---
@app.route('/api/clear-all-data', methods=['POST'])
@login_required
def clear_all_data():
    data = request.json
    confirm_text = data.get('confirm', '')
    
    # 需要输入确认文本
    if confirm_text != 'DELETE ALL':
        return jsonify({'status': 'error', 'msg': '请输入正确的确认文本'})
    
    try:
        # 1. 删除所有云盘文件
        if os.path.exists(STORAGE_DIR):
            shutil.rmtree(STORAGE_DIR)
            os.makedirs(STORAGE_DIR)
        
        # 2. 删除所有 ZIP 文件
        if os.path.exists(FOLDERZIP_DIR):
            shutil.rmtree(FOLDERZIP_DIR)
            os.makedirs(FOLDERZIP_DIR)
        
        # 3. 删除所有上传的背景图片
        if os.path.exists(STATIC_DIR):
            for filename in os.listdir(STATIC_DIR):
                if filename.startswith('bg_'):
                    filepath = os.path.join(STATIC_DIR, filename)
                    if os.path.isfile(filepath):
                        os.remove(filepath)
        
        # 4. 清空数据库中的分享链接
        ShareLink.query.delete()
        
        # 5. 重置所有设置为默认值
        default_hash = generate_password_hash(DEFAULT_PASSWORD)
        set_setting('password_hash', default_hash)
        set_setting('theme', 'light')
        set_setting('background_type', 'image')
        set_setting('background_image', 'bg.png')
        set_setting('background_color', '#667eea')
        
        db.session.commit()
        
        # 6. 清除当前 session，强制重新登录
        session.clear()
        
        return jsonify({
            'status': 'success', 
            'msg': '所有数据已清空，密码已重置为默认密码'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'msg': str(e)})

# --- 主程序入口

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
