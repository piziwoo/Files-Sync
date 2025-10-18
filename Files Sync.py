import os
import subprocess
import datetime
import threading
import tempfile
import time
import sys
import xml.etree.ElementTree as ET
from urllib.parse import quote
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import pathlib
import stat
import shutil
import ctypes
from win32event import CreateMutex
from win32api import GetLastError
from winerror import ERROR_ALREADY_EXISTS
import hashlib
import base64

# 线程锁定义
log_lock = threading.Lock()
pending_delete_lock = threading.Lock()
pending_delete_dir_lock = threading.Lock()
info_lock = threading.Lock()
last_moved_lock = threading.Lock()
request_lock = threading.Lock()

# 全局配置
ignored_files = set()
recent_deletes = []
recent_deletes_dir = []
DELAY_FIRST_CHECK = 0.5
DELAY_FINAL_ACTION = 1.0
pending_batch = None
pending_batch_dir = None
last_moved_event = {"src_path": None, "dest_path": None, "time": 0}
MOVE_EVENT_TIMEOUT = 5.0
MOVE_DETECTION_WINDOW = 1.0
MIN_REQUEST_INTERVAL = 0.5
last_request_time = 0
TEMP_SYNC_DIR = r"C:\ProgramData\Files Sync Temp"
RESTRICTED_EXTENSIONS = set()
BYPASS_SUFFIX = ""
WEBDAV_URL = ""
USERNAME = ""
PASSWORD = ""
CONFIG_FILE = "sync.json"
INFO_FILE = "info.txt"
LAST_INFO_CACHE = os.path.join("C:\\", "ProgramData", "last_info_cache.txt")
PROMPT_TEXT = "格式：FS 同步码 文件夹路径 或 F 同步码 文件路径，下面输入每行一个"
EXAMPLE_TEXT = "示例：FS 123 D:\\文件夹"
SYNC_INTERVAL = 300
DEBOUNCE_INTERVAL = 3.0
RETRY_COUNT = 3
RETRY_DELAY = 5
TIME_TOLERANCE = 2
ENCRYPT_KEY = b"sync123"

def encrypt_webdav(url_line, user_line, pwd_line):
    data = f"{url_line}\n{user_line}\n{pwd_line}".encode('utf-8')
    key_repeated = ENCRYPT_KEY * (len(data) // len(ENCRYPT_KEY) + 1)
    xored = bytes(a ^ b for a, b in zip(data, key_repeated[:len(data)]))
    return base64.b64encode(xored).decode('utf-8')

def decrypt_webdav(encrypted_line):
    try:
        data = base64.b64decode(encrypted_line.encode('utf-8'))
        key_repeated = ENCRYPT_KEY * (len(data) // len(ENCRYPT_KEY) + 1)
        xored = bytes(a ^ b for a, b in zip(data, key_repeated[:len(data)]))
        lines = xored.decode('utf-8').split('\n')
        url = lines[0].split('地址：', 1)[1].strip() if len(lines) > 0 and '地址：' in lines[0] else ''
        user = lines[1].split('账号：', 1)[1].strip() if len(lines) > 1 and '账号：' in lines[1] else ''
        pwd = lines[2].split('密码：', 1)[1].strip() if len(lines) > 2 and '密码：' in lines[2] else ''
        return url, user, pwd
    except Exception as e:
        log_message(f"解密失败: {e}")
        return '', '', ''

class BatchDeleteDir:
    def __init__(self):
        self.start_time = time.time()
        self.missing_dirs = {}
        self.first_check_done = False
        self.final_timer = None

class PendingDeleteDir:
    def __init__(self, path, rel_path):
        self.path = path
        self.rel_path = rel_path
        self.filename = os.path.basename(path)
        self.timestamp = time.time()
        self.is_moved = False

class BatchDelete:
    def __init__(self):
        self.start_time = time.time()
        self.missing_files = {}
        self.first_check_done = False
        self.final_timer = None

class PendingDelete:
    def __init__(self, path, is_directory, filename=None, mtime=None, md5=None):
        self.path = path
        self.is_directory = is_directory
        self.filename = filename
        self.mtime = mtime
        self.md5 = md5
        self.timestamp = time.time()
        self.is_moved = False

def calculate_md5(file_path):
    try:
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest().upper()
    except Exception as e:
        log_message(f"hashlib计算MD5失败: {file_path}（{e}），尝试PowerShell")
        try:
            escaped_path = file_path.replace("\\", "\\\\")
            ps_cmd = (
                f"Get-FileHash -Path '{escaped_path}' -Algorithm MD5 "
                "| Select-Object -ExpandProperty Hash "
                "| ForEach-Object { $_.ToUpper() }"
            )
            process = subprocess.Popen(
                ["powershell", "-Command", ps_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            stdout, stderr = process.communicate()
            md5_result = stdout.strip()
            if len(md5_result) == 32 and all(c in "0123456789ABCDEF" for c in md5_result):
                log_message(f"PowerShell计算MD5成功: {file_path} -> {md5_result}")
                return md5_result
            log_message(f"PowerShell输出无效MD5: {file_path}（输出: {md5_result[:20]}...）")
            return None
        except Exception as e:
            log_message(f"PowerShell计算MD5失败: {file_path}（{e}）")
            return None

def log_message(message):
    if any(kw in message for kw in ["跳过系统文件", "% Total", "Dload", "Upload", "Speed"]):
        return
    thread_name = threading.current_thread().name
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with log_lock:
        print(f"[{timestamp}] [{thread_name}] {message}")

def ensure_request_interval():
    global last_request_time
    with request_lock:
        current_time = time.time()
        elapsed = current_time - last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            wait = MIN_REQUEST_INTERVAL - elapsed
            log_message(f"请求间隔控制，等待{wait:.2f}秒")
            time.sleep(wait)
        last_request_time = time.time()

def encode_url_component(path):
    return quote(path, safe='')

def normalize_webdav_path(path):
    return path

def check_single_instance():
    mutex_name = "FileSyncService_Mutex"
    try:
        mutex = CreateMutex(None, False, mutex_name)
        if GetLastError() == ERROR_ALREADY_EXISTS:
            log_message("程序已在运行，退出")
            sys.exit(1)
        return mutex
    except Exception as e:
        log_message(f"互斥锁创建失败: {e}")
        sys.exit(1)

def load_config():
    global RESTRICTED_EXTENSIONS, BYPASS_SUFFIX
    try:
        webdav_config = {"url": "", "username": "", "password": ""}
        configs = []
        if not os.path.exists(CONFIG_FILE):
            template_content = """地址：
账号：
密码：
被限制下载的后缀名“用、隔开”：
绕过后缀名“只能填一个，空白代表改为无后缀名再下载”：
格式：FS 同步码 文件夹路径 或 F 同步码 文件路径，下面输入每行一个
示例：FS 123 D:\\文件夹
"""
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                f.write(template_content)
            log_message(f"创建配置文件模板: {CONFIG_FILE}")
            sys.exit(1)
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines()]
        first_line = lines[0] if lines else ""
        is_encrypted = first_line.endswith('==')
        if is_encrypted:
            url, username, password = decrypt_webdav(first_line)
            webdav_config['url'] = url
            webdav_config['username'] = username
            webdav_config['password'] = password
            log_message("检测到加密配置，进行解密")
            i = 1
        else:
            webdav_lines = []
            i = 0
            while i < len(lines) and len(webdav_lines) < 3:
                line = lines[i]
                if line:
                    webdav_lines.append(line)
                i += 1
            for line in webdav_lines:
                if '地址：' in line:
                    webdav_config['url'] = line.split('地址：', 1)[1].strip()
                elif '账号：' in line:
                    webdav_config['username'] = line.split('账号：', 1)[1].strip()
                elif '密码：' in line:
                    webdav_config['password'] = line.split('密码：', 1)[1].strip()
            if webdav_config['url']:
                encrypted_line = encrypt_webdav(
                    f"地址：{webdav_config['url']}",
                    f"账号：{webdav_config['username']}",
                    f"密码：{webdav_config['password']}"
                )
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    f.write(f"{encrypted_line}\n")
                    for j in range(i, len(lines)):
                        f.write(lines[j] + '\n')
                log_message("检测到明文配置，已自动加密保存")
        restricted_extensions = ""
        bypass_suffix = ""
        while i < len(lines):
            line = lines[i]
            if '被限制下载的后缀名' in line:
                restricted_extensions = line.split('被限制下载的后缀名“用、隔开”：', 1)[1].strip()
                i += 1
                if i < len(lines) and '绕过后缀名' in lines[i]:
                    bypass_suffix = lines[i].split('绕过后缀名“只能填一个，空白代表改为无后缀名再下载”：', 1)[1].strip()
                break
            i += 1
        RESTRICTED_EXTENSIONS = {'.' + ext.lower() for ext in restricted_extensions.split('、') if ext.strip()} if restricted_extensions else set()
        BYPASS_SUFFIX = bypass_suffix if bypass_suffix else ""
        log_message(f"加载扩展名配置: 受限后缀={RESTRICTED_EXTENSIONS}, 绕过后缀={BYPASS_SUFFIX or '无后缀'}")
        if not webdav_config['url']:
            log_message("错误：WebDAV地址未提供")
            sys.exit(1)
        if webdav_config['username'] and not webdav_config['password'] or not webdav_config['username'] and webdav_config['password']:
            log_message("错误：WebDAV账号和密码必须同时提供或同时为空")
            sys.exit(1)
        if webdav_config['username'] and webdav_config['password']:
            log_message(f"加载WebDAV配置: URL={webdav_config['url'][:20]}..., User={webdav_config['username']}")
        else:
            log_message(f"加载WebDAV配置: URL={webdav_config['url'][:20]}...（匿名访问）")
        task_start = i
        prompt_found = False
        for j in range(task_start, len(lines)):
            line = lines[j]
            if not line:
                continue
            if not prompt_found and PROMPT_TEXT in line:
                prompt_found = True
                continue
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0] in ("FS", "F"):
                path = parts[2].encode('utf-8').decode('utf-8', errors='ignore').strip('\u202a\u202b\u202c\u200e\u200f')
                log_message(f"加载同步任务: {parts[0]} {parts[1]} {path}")
                configs.append({"type": parts[0], "sync_code": parts[1], "path": path})
        if not configs:
            log_message("无有效同步任务")
        return configs, webdav_config
    except Exception as e:
        log_message(f"配置加载失败: {e}")
        sys.exit(1)

def run_curl(cmd_args, encoding=None):
    try:
        if USERNAME and PASSWORD:
            auth_cmd = ['--user', f'{USERNAME}:{PASSWORD}']
            cmd_args = cmd_args[:1] + auth_cmd + cmd_args[1:]
        cmd_str = ' '.join(cmd_args[:5]) + ('...' if len(cmd_args) > 5 else '')
        log_message(f"执行curl: {cmd_str}")
        encoding = encoding or (sys.getfilesystemencoding() if os.name == 'nt' else 'utf-8')
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding=encoding, errors='replace', creationflags=creationflags)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            error_msg = stderr.strip() or stdout.strip() or f"返回码 {process.returncode}"
            return None, process.returncode, error_msg
        return stdout.strip(), process.returncode, ""
    except Exception as e:
        log_message(f"curl执行异常: {e}")
        return None, -1, str(e)

def get_webdav_path(sync_code, name, sub_path="", is_folder=False):
    root_folder = encode_url_component(f"{sync_code} {name}")
    base_path = f"{WEBDAV_URL.rstrip('/')}/{root_folder}"
    if sub_path:
        sub_path = encode_url_component(sub_path.strip("./").replace("\\", "/"))
        full_path = f"{base_path}/{sub_path}"
    else:
        full_path = base_path
    return f"{full_path}/" if is_folder else full_path

def download_info_file():
    try:
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as temp_file:
            info_path = temp_file.name
        cmd_args = ['curl', '-o', info_path, f"{WEBDAV_URL.rstrip('/')}/{INFO_FILE}"]
        download_success = False
        for i in range(RETRY_COUNT + 1):
            ensure_request_interval()
            result, returncode, error_msg = run_curl(cmd_args)
            if returncode == 0:
                download_success = True
                break
            if i < RETRY_COUNT:
                log_message(f"同步记录下载失败（重试{i+1}）: {error_msg[:50]}...")
                time.sleep(RETRY_DELAY)
        if download_success:
            shutil.copy2(info_path, LAST_INFO_CACHE)
            log_message(f"同步记录下载成功，更新缓存")
            return info_path
        elif os.path.exists(LAST_INFO_CACHE):
            shutil.copy2(LAST_INFO_CACHE, info_path)
            log_message(f"同步记录下载失败，使用缓存")
            return info_path
        else:
            log_message("同步记录下载失败且无缓存，创建空白文件")
            with open(info_path, 'w', encoding='utf-8') as f:
                f.write("")
            return info_path
    except Exception as e:
        log_message(f"同步记录处理异常: {e}")
        return None

def parse_info_file(info_path):
    try:
        if not info_path or not os.path.exists(info_path):
            return {}, set()
        with open(info_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content or "Not Found" in content:
            return {}, set()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        info = {}
        known_dirs = set()
        current_sync = None
        for line in lines:
            if line == ',':
                current_sync = None
                continue
            if line.startswith("FS ") or line.startswith("F "):
                parts = line.split(" ", 2)
                if len(parts) != 3:
                    log_message(f"无效同步记录行: {line}")
                    continue
                current_sync = {"type": parts[0], "sync_code": parts[1], "name": parts[2], "dirs": set(), "files": {}}
                key = f"{parts[1]} {parts[2]}"
                info[key] = current_sync
                log_message(f"加载同步记录: {key}")
                if parts[0] == "FS":
                    dir_path = get_webdav_path(parts[1], parts[2], is_folder=True)
                    known_dirs.add(dir_path)
            elif current_sync:
                if line.endswith("/"):
                    dir_path = line.rstrip("/")
                    current_sync["dirs"].add(dir_path)
                    log_message(f"解析同步目录: {dir_path}")
                    if current_sync["type"] == "FS":
                        remote_dir = get_webdav_path(current_sync["sync_code"], current_sync["name"], dir_path, is_folder=True)
                        known_dirs.add(remote_dir)
                else:
                    parts = line.split(" ", 1)
                    if len(parts) != 2:
                        log_message(f"无效同步记录行: {line}")
                        continue
                    try:
                        time_str = parts[0]
                        file_time = datetime.datetime.strptime(time_str, "%Y-%m-%d-%H-%M-%S")
                        md5_part, filename = parts[1].split(" ", 1)
                        if not md5_part.startswith("MD5:"):
                            log_message(f"无效校验值格式: {line}")
                            continue
                        file_md5 = md5_part[4:]
                        current_sync["files"][filename] = {"mtime": file_time, "md5": file_md5}
                        log_message(f"解析同步文件: {filename}（{file_time}，校验值: {file_md5}）")
                    except (ValueError, IndexError) as e:
                        log_message(f"同步记录解析错误: {line} ({e})")
                        continue
        return info, known_dirs
    except Exception as e:
        log_message(f"同步记录解析异常: {e}")
        return {}, set()

def is_webdav_dir(remote_dir):
    cmd_args = ['curl', '-X', 'PROPFIND', '--header', 'Depth: 0', remote_dir]
    ensure_request_interval()
    result, returncode, error_msg = run_curl(cmd_args)
    if returncode not in (200, 207):
        log_message(f"目录检查失败: {remote_dir} ({error_msg[:50]}...)")
        return False
    try:
        root = ET.fromstring(result)
        return root.find('.//d:collection', {'d': 'DAV:'}) is not None
    except ET.ParseError:
        log_message(f"目录响应解析失败: {remote_dir}")
        return False

def create_webdav_directory(remote_dir, known_dirs, skip_check=False):
    normalized_dir = remote_dir
    if normalized_dir in known_dirs:
        log_message(f"目录已缓存: {normalized_dir}")
        return True
    if not skip_check and is_webdav_dir(remote_dir):
        log_message(f"目录已存在: {remote_dir}")
        known_dirs.add(normalized_dir)
        return True
    for i in range(RETRY_COUNT + 1):
        cmd_args = ['curl', '-X', 'MKCOL', remote_dir]
        ensure_request_interval()
        result, returncode, error_msg = run_curl(cmd_args)
        if returncode == 0 or "Created" in str(result) or "Method Not Allowed" in error_msg:
            log_message(f"创建目录成功: {remote_dir}")
            known_dirs.add(normalized_dir)
            return True
        if i < RETRY_COUNT:
            log_message(f"创建目录失败（重试{i+1}）: {remote_dir} ({error_msg[:50]}...)")
            time.sleep(RETRY_DELAY)
    log_message(f"创建目录失败: {remote_dir}")
    return False

def get_local_files_info(local_dir):
    try:
        files_info = {}
        EXCLUDED_DIRS = {'$RECYCLE.BIN', 'System Volume Information'}
        for root, dirs, files in os.walk(local_dir, topdown=True):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not (pathlib.Path(os.path.join(root, d)).is_dir() and 
                                                                          pathlib.Path(os.path.join(root, d)).stat().st_file_attributes & stat.FILE_ATTRIBUTE_SYSTEM)]
            rel_root = os.path.relpath(root, local_dir).replace("\\", "/").strip("./")
            if rel_root:
                try:
                    files_info[rel_root] = {"path": root, "is_directory": True}
                    log_message(f"本地目录: {rel_root}")
                except OSError as e:
                    log_message(f"无法访问目录 {root}: {e}")
                    continue
            for file in files:
                local_file = os.path.join(root, file)
                try:
                    file_stat = pathlib.Path(local_file).stat()
                    if file_stat.st_file_attributes & stat.FILE_ATTRIBUTE_SYSTEM:
                        continue
                    mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(local_file)).replace(microsecond=0)
                    file_md5 = calculate_md5(local_file)
                    if file_md5 is None:
                        log_message(f"跳过无法计算MD5的文件: {local_file}")
                        continue
                    rel_path = os.path.join(rel_root, file).replace("\\", "/").strip("./") if rel_root else file
                    files_info[rel_path] = {"path": local_file, "mtime": mod_time, "md5": file_md5, "size": file_stat.st_size, "is_directory": False}
                    log_message(f"本地文件: {rel_path}（{mod_time}，校验值: {file_md5}）")
                except OSError as e:
                    log_message(f"无法访问文件 {local_file}: {e}")
                    continue
        log_message(f"本地扫描完成: {local_dir}（{len(files_info)}项）")
        return files_info
    except Exception as e:
        log_message(f"本地扫描异常: {e}")
        return {}

def upload_file(local_file, remote_file, info, key, rel_path, local_time, local_md5, sync_type, is_directory=False):
    if not is_directory and local_md5 is None:
        log_message(f"文件MD5计算失败，跳过上传: {local_file}")
        return False
    try:
        local_file = os.path.normpath(local_file)
        if not os.path.exists(local_file):
            log_message(f"{'目录' if is_directory else '文件'}不存在: {local_file}")
            return False
        if is_directory:
            return create_webdav_directory(remote_file, known_dirs=set())
        fs_encoding = sys.getfilesystemencoding() or 'utf-8'
        local_file_str = local_file.encode(fs_encoding, errors='replace').decode(fs_encoding)
        for i in range(RETRY_COUNT + 1):
            cmd_args = ['curl', '-T', local_file_str, remote_file]
            ensure_request_interval()
            result, returncode, error_msg = run_curl(cmd_args)
            if returncode == 0 or ("Created" in str(result) or "OK" in str(result)):
                log_message(f"上传成功: {local_file} -> {remote_file}")
                with info_lock:
                    if key not in info:
                        info[key] = {"type": sync_type, "sync_code": key.split()[0], "name": key.split()[1], "dirs": set(), "files": {}}
                    info[key]["files"][rel_path] = {"mtime": local_time, "md5": local_md5}
                return True
            if i < RETRY_COUNT:
                log_message(f"上传失败（重试{i+1}）: {local_file} -> {remote_file} ({error_msg[:50]}...)")
                time.sleep(RETRY_DELAY)
        log_message(f"上传失败: {local_file} -> {remote_file}")
        return False
    except Exception as e:
        log_message(f"{'目录' if is_directory else '文件'}上传异常: {e}")
        return False

def rename_remote_file(old_path, new_path, info, key, old_rel_path, new_rel_path, local_time=None, local_md5=None):
    is_dir = old_path.endswith('/')
    if not is_dir and local_md5 is None and local_time is not None:
        log_message(f"文件MD5计算失败，跳过移动: {old_path} -> {new_path}")
        return False
    for i in range(RETRY_COUNT + 1):
        cmd_args = ['curl', '-X', 'MOVE', '--header', f'Destination: {new_path}', old_path]
        ensure_request_interval()
        result, returncode, error_msg = run_curl(cmd_args)
        if returncode == 0 or ("Created" in str(result) or "OK" in str(result)):
            log_message(f"{'目录' if is_dir else '文件'}移动/改名成功: {old_path} -> {new_path}")
            with info_lock:
                if key not in info:
                    info[key] = {"type": "FS", "sync_code": key.split()[0], "name": key.split()[1], "dirs": set(), "files": {}}
                if not is_dir and old_rel_path in info[key]["files"]:
                    file_info = info[key]["files"].pop(old_rel_path)
                    if local_time and local_md5:
                        file_info["mtime"] = local_time
                        file_info["md5"] = local_md5
                    info[key]["files"][new_rel_path] = file_info
                elif is_dir:
                    if old_rel_path in info[key]["dirs"]:
                        info[key]["dirs"].remove(old_rel_path)
                    info[key]["dirs"].add(new_rel_path)
            return True
        if i < RETRY_COUNT:
            log_message(f"{'目录' if is_dir else '文件'}移动/改名失败（重试{i+1}）: {old_path} -> {new_path} ({error_msg[:50]}...)")
            time.sleep(RETRY_DELAY)
    log_message(f"{'目录' if is_dir else '文件'}移动/改名失败: {old_path} -> {new_path}")
    return False

def get_webdav_directory_contents(remote_dir):
    try:
        cmd_args = ['curl', '-X', 'PROPFIND', '--header', 'Depth: 1', remote_dir]
        ensure_request_interval()
        result, returncode, error_msg = run_curl(cmd_args)
        if returncode not in (200, 207):
            log_message(f"获取目录内容失败: {remote_dir} ({error_msg[:50]}...)")
            return []
        root = ET.fromstring(result)
        hrefs = [elem.text for elem in root.findall('.//d:href', {'d': 'DAV:'})]
        return [h for h in hrefs if h != remote_dir and h != f"{remote_dir}/"]
    except Exception as e:
        log_message(f"解析目录内容异常: {remote_dir} ({e})")
        return []

def move_webdav_directory(old_dir, new_dir, info, key, old_rel_path, new_rel_path, known_dirs):
    old_dir = old_dir.rstrip('/') + '/'
    new_dir = new_dir.rstrip('/') + '/'
    for i in range(RETRY_COUNT + 1):
        cmd_args = ['curl', '-X', 'MOVE', '--header', f'Destination: {new_dir}', old_dir]
        ensure_request_interval()
        result, returncode, error_msg = run_curl(cmd_args)
        if returncode in (0, 201, 204) or "Created" in str(result) or "No Content" in str(result):
            log_message(f"文件夹移动成功: {old_dir} -> {new_dir}")
            with info_lock:
                if key in info:
                    if old_rel_path in info[key]["dirs"]:
                        info[key]["dirs"].remove(old_rel_path)
                    info[key]["dirs"].add(new_rel_path)
                    old_prefix = f"{old_rel_path}/"
                    new_files = {}
                    for file_path in info[key]["files"]:
                        if file_path.startswith(old_prefix):
                            new_file_path = file_path.replace(old_prefix, f"{new_rel_path}/", 1)
                            new_files[new_file_path] = info[key]["files"][file_path]
                        else:
                            new_files[file_path] = info[key]["files"][file_path]
                    info[key]["files"] = new_files
            known_dirs.discard(old_dir)
            known_dirs.add(new_dir)
            return True
        if i < RETRY_COUNT:
            log_message(f"文件夹移动失败（重试{i+1}）: {old_dir} -> {new_dir} ({error_msg[:50]}...)")
            time.sleep(RETRY_DELAY)
    log_message(f"文件夹移动失败: {old_dir} -> {new_dir}")
    return False

def download_file(remote_file, local_file, remote_time, remote_md5, is_directory=False, info=None, key=None, rel_path=None):
    try:
        local_file = os.path.normpath(local_file)
        if is_directory:
            os.makedirs(local_file, exist_ok=True)
            log_message(f"创建本地目录: {local_file}")
            return True
        os.makedirs(TEMP_SYNC_DIR, exist_ok=True)
        filename = os.path.basename(local_file)
        temp_file_path = os.path.join(TEMP_SYNC_DIR, filename)
        original_remote_file = remote_file
        original_rel_path = rel_path
        _, ext = os.path.splitext(filename)
        is_restricted = ext.lower() in RESTRICTED_EXTENSIONS
        temp_remote_file = None
        if is_restricted:
            temp_filename = filename[:-len(ext)] + BYPASS_SUFFIX if BYPASS_SUFFIX else filename[:-len(ext)]
            temp_remote_file = remote_file[:-len(ext)] + BYPASS_SUFFIX if BYPASS_SUFFIX else remote_file[:-len(ext)]
            temp_file_path = os.path.join(TEMP_SYNC_DIR, temp_filename)
            temp_rel_path = rel_path[:-len(ext)] + BYPASS_SUFFIX if BYPASS_SUFFIX else rel_path[:-len(ext)]
            log_message(f"受限扩展名{ext}，远程临时改名: {remote_file} -> {temp_remote_file}")
            if not rename_remote_file(remote_file, temp_remote_file, info, key, rel_path, temp_rel_path):
                log_message(f"远程临时改名失败: {remote_file} -> {temp_remote_file}")
                return False
            remote_file = temp_remote_file
            rel_path = temp_rel_path
        os.makedirs(os.path.dirname(local_file), exist_ok=True)
        fs_encoding = sys.getfilesystemencoding() or 'utf-8'
        temp_file_str = temp_file_path.encode(fs_encoding, errors='replace').decode(fs_encoding)
        ignored_files.add(local_file)
        download_success = False
        for i in range(RETRY_COUNT + 1):
            cmd_args = ['curl', '-o', temp_file_str, remote_file]
            ensure_request_interval()
            result, returncode, error_msg = run_curl(cmd_args)
            if returncode == 0 or ("OK" in str(result) or "Created" in str(result)):
                log_message(f"下载到临时文件: {remote_file} -> {temp_file_path}")
                download_success = True
                break
            if i < RETRY_COUNT:
                log_message(f"下载失败（重试{i+1}）: {remote_file} -> {temp_file_path} ({error_msg[:50]}...)")
                time.sleep(RETRY_DELAY)
        if download_success:
            if is_restricted:
                original_temp_file_path = os.path.join(TEMP_SYNC_DIR, filename)
                try:
                    shutil.move(temp_file_path, original_temp_file_path)
                    log_message(f"临时文件改名: {temp_file_path} -> {original_temp_file_path}")
                    temp_file_path = original_temp_file_path
                except Exception as e:
                    log_message(f"临时文件改名失败: {temp_file_path} -> {original_temp_file_path} ({e})")
                    if is_restricted and temp_remote_file:
                        rename_remote_file(temp_remote_file, original_remote_file, info, key, rel_path, original_rel_path)
                    ignored_files.discard(local_file)
                    return False
            local_md5_after = calculate_md5(temp_file_path)
            if local_md5_after is None:
                log_message(f"下载文件MD5计算失败，跳过: {temp_file_path}")
                if is_restricted and temp_remote_file:
                    rename_remote_file(temp_remote_file, original_remote_file, info, key, rel_path, original_rel_path)
                ignored_files.discard(local_file)
                return False
            if local_md5_after != remote_md5:
                log_message(f"校验值不匹配: {temp_file_path}（本地: {local_md5_after}，远程: {remote_md5}）")
                if is_restricted and temp_remote_file:
                    rename_remote_file(temp_remote_file, original_remote_file, info, key, rel_path, original_rel_path)
                ignored_files.discard(local_file)
                return False
            try:
                shutil.move(temp_file_path, local_file)
                log_message(f"移动临时文件: {temp_file_path} -> {local_file}")
                os.utime(local_file, (remote_time.timestamp(), remote_time.timestamp()))
                log_message(f"下载成功: {original_remote_file} -> {local_file}")
            except Exception as e:
                log_message(f"移动文件异常: {e}")
                if is_restricted and temp_remote_file:
                    rename_remote_file(temp_remote_file, original_remote_file, info, key, rel_path, original_rel_path)
                ignored_files.discard(local_file)
                return False
            if is_restricted and temp_remote_file:
                if not rename_remote_file(temp_remote_file, original_remote_file, info, key, rel_path, original_rel_path):
                    log_message(f"恢复远程文件名失败: {temp_remote_file} -> {original_remote_file}")
                    ignored_files.discard(local_file)
                    return False
        else:
            log_message(f"下载失败: {remote_file} -> {local_file}")
            if is_restricted and temp_remote_file:
                rename_remote_file(temp_remote_file, original_remote_file, info, key, rel_path, original_rel_path)
            ignored_files.discard(local_file)
            return False
        ignored_files.discard(local_file)
        return True
    except Exception as e:
        log_message(f"{'目录' if is_directory else '文件'}下载异常: {e}")
        if is_restricted and temp_remote_file:
            rename_remote_file(temp_remote_file, original_remote_file, info, key, rel_path, original_rel_path)
        ignored_files.discard(local_file)
        return False

def get_webdav_file_mtime(remote_file):
    try:
        cmd_args = ['curl', '-X', 'PROPFIND', '--header', 'Depth: 0', remote_file]
        ensure_request_interval()
        result, returncode, error_msg = run_curl(cmd_args)
        if returncode not in (200, 207):
            return None
        root = ET.fromstring(result)
        mtime_elem = root.find('.//d:getlastmodified', {'d': 'DAV:'})
        if mtime_elem is None:
            return None
        mtime = datetime.datetime.strptime(mtime_elem.text, '%a, %d %b %Y %H:%M:%S %Z')
        return mtime.timestamp()
    except Exception as e:
        log_message(f"获取远程文件时间失败: {e}")
        return None

def delete_webdav_file(remote_file, info, key, rel_path, is_directory=False):
    is_dir = is_directory or remote_file.endswith('/')
    for i in range(RETRY_COUNT + 1):
        cmd_args = ['curl', '-X', 'DELETE', remote_file]
        ensure_request_interval()
        result, returncode, error_msg = run_curl(cmd_args)
        if returncode == 0:
            log_message(f"{'目录' if is_dir else '文件'}删除成功: {remote_file}")
            with info_lock:
                if key in info:
                    if not is_dir and rel_path in info[key]["files"]:
                        del info[key]["files"][rel_path]
                    elif is_dir:
                        if rel_path in info[key]["dirs"]:
                            info[key]["dirs"].remove(rel_path)
                        to_delete = [p for p in info[key]["files"] if p.startswith(f"{rel_path}/")]
                        for p in to_delete:
                            del info[key]["files"][p]
                        to_delete_dirs = [d for d in info[key]["dirs"] if d.startswith(f"{rel_path}/")]
                        for d in to_delete_dirs:
                            info[key]["dirs"].remove(d)
            return True
        if i < RETRY_COUNT:
            log_message(f"{'目录' if is_dir else '文件'}删除失败（重试{i+1}）: {remote_file} ({error_msg[:50]}...)")
            time.sleep(RETRY_DELAY)
    log_message(f"{'目录' if is_dir else '文件'}删除失败: {remote_file}")
    return False

def update_info_file(info, configs):
    try:
        content = []
        with info_lock:
            for config in configs:
                local_path = config['path']
                name = local_path.rstrip('\\/').split('\\')[-1] if os.name == 'nt' else local_path.rstrip('/').split('/')[-1]
                key = f"{config['sync_code']} {name}"
                if key in info:
                    content.append(f"{info[key]['type']} {config['sync_code']} {name}")
                    for dir_path in sorted(info[key]["dirs"]):
                        content.append(f"{dir_path}/")
                    for file_path, file_info in sorted(info[key]["files"].items()):
                        mtime_str = file_info["mtime"].strftime('%Y-%m-%d-%H-%M-%S')
                        content.append(f"{mtime_str} MD5:{file_info['md5']} {file_path}")
                    content.append(",")
        content_str = "\n".join(content) if content else ""
        log_message(f"更新同步记录（{len(content)}行）")
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as temp_file:
            temp_file_path = temp_file.name
            with open(temp_file_path, 'w', encoding='utf-8') as f:
                f.write(content_str)
        if os.path.exists(LAST_INFO_CACHE):
            with open(LAST_INFO_CACHE, 'r', encoding='utf-8') as f:
                old_content = f.read().strip()
        else:
            old_content = ""
        if old_content == content_str:
            log_message("同步记录无变化，跳过上传")
            os.remove(temp_file_path)
            return
        cmd_args = ['curl', '-T', temp_file_path, f"{WEBDAV_URL.rstrip('/')}/{INFO_FILE}"]
        upload_success = False
        for i in range(RETRY_COUNT + 1):
            ensure_request_interval()
            result, returncode, error_msg = run_curl(cmd_args)
            if returncode == 0 or ("Created" in str(result) or "OK" in str(result)):
                log_message("同步记录上传成功")
                shutil.copy2(temp_file_path, LAST_INFO_CACHE)
                upload_success = True
                break
            if i < RETRY_COUNT:
                log_message(f"同步记录上传失败（重试{i+1}）: {error_msg[:50]}...")
                time.sleep(RETRY_DELAY)
        os.remove(temp_file_path)
        if not upload_success:
            log_message("同步记录上传失败")
    except Exception as e:
        log_message(f"同步记录更新异常: {e}")

def should_sync(local_time, local_md5, remote_info):
    if local_md5 is None:
        return False
    if local_md5 == remote_info["md5"]:
        local_trunc = local_time.replace(microsecond=0)
        remote_trunc = remote_info["mtime"].replace(microsecond=0)
        time_diff = (local_trunc - remote_trunc).total_seconds()
        if abs(time_diff) <= TIME_TOLERANCE:
            return False
        return local_trunc > remote_trunc
    return local_time > remote_info["mtime"]

def sync_folder(config, info, known_dirs, configs):
    try:
        local_path = config["path"]
        sync_code = config["sync_code"]
        name = local_path.rstrip('\\/').split('\\')[-1] if os.name == 'nt' else local_path.rstrip('/').split('/')[-1]
        key = f"{sync_code} {name}"
        log_message(f"=== 同步文件夹: {key}（{local_path}） ===")
        if config["type"] == "FS" and not os.path.exists(local_path):
            os.makedirs(local_path, exist_ok=True)
            log_message(f"创建本地文件夹: {local_path}")
        if config["type"] == "FS":
            remote_root = get_webdav_path(sync_code, name, is_folder=True)
            with info_lock:
                if key not in info and not create_webdav_directory(remote_root, known_dirs):
                    log_message("创建远程根文件夹失败，终止同步")
                    return
                if key not in info:
                    info[key] = {"type": "FS", "sync_code": sync_code, "name": name, "dirs": set(), "files": {}}
            local_files = get_local_files_info(local_path)
            with info_lock:
                remote_dirs = info[key]["dirs"].copy()
                remote_files = info[key]["files"].copy()
            log_message(f"对比: 本地{len(local_files)}项，远程{len(remote_dirs)}文件夹+{len(remote_files)}文件")
            local_dirs = {p: info for p, info in local_files.items() if info["is_directory"]}
            local_files_only = {p: info for p, info in local_files.items() if not info["is_directory"]}
            to_upload_files = []
            to_download_files = []
            to_create_remote_dirs = []
            to_create_local_dirs = []
            for rel_path in local_dirs:
                if rel_path not in remote_dirs:
                    remote_dir = get_webdav_path(sync_code, name, rel_path, is_folder=True)
                    to_create_remote_dirs.append((rel_path, remote_dir))
            for rel_path in remote_dirs:
                if rel_path not in local_dirs:
                    local_dir_path = os.path.join(local_path, rel_path.replace("/", os.sep))
                    to_create_local_dirs.append((rel_path, local_dir_path))
            for rel_path, local_dir_path in to_create_local_dirs:
                if not os.path.exists(local_dir_path):
                    os.makedirs(local_dir_path, exist_ok=True)
                    log_message(f"创建本地目录: {rel_path} -> {local_dir_path}")
            for rel_path, remote_dir in to_create_remote_dirs:
                if create_webdav_directory(remote_dir, known_dirs):
                    log_message(f"创建远程目录: {rel_path} -> {remote_dir}")
                    with info_lock:
                        info[key]["dirs"].add(rel_path)
                    update_info_file(info, configs)
            for rel_path, file_info in local_files_only.items():
                local_time = file_info["mtime"]
                local_md5 = file_info["md5"]
                remote_info = remote_files.get(rel_path)
                if rel_path not in remote_files:
                    log_message(f"需上传: {rel_path}（远程无记录）")
                    to_upload_files.append((file_info["path"], get_webdav_path(sync_code, name, rel_path, False), local_time, local_md5, rel_path))
                elif should_sync(local_time, local_md5, remote_info):
                    log_message(f"需更新（本地较新）: {rel_path}")
                    to_upload_files.append((file_info["path"], get_webdav_path(sync_code, name, rel_path, False), local_time, local_md5, rel_path))
                else:
                    # 修复：检查远程是否较新
                    if remote_info["mtime"] > local_time and local_md5 != remote_info["md5"]:
                        log_message(f"需更新（远程较新）: {rel_path}")
                        local_path_full = os.path.join(local_path, rel_path.replace("/", os.sep))
                        to_download_files.append((get_webdav_path(sync_code, name, rel_path, False), local_path_full, remote_info["mtime"], remote_info["md5"], rel_path))
                    else:
                        log_message(f"文件内容一致（时间差在容错范围内或MD5相同）: {rel_path}")
                        with info_lock:
                            info[key]["files"][rel_path]["mtime"] = local_time
                        update_info_file(info, configs)
            for rel_path in remote_files:
                remote_info = remote_files[rel_path]
                remote_time = remote_info["mtime"]
                remote_md5 = remote_info["md5"]
                local_file_info = local_files_only.get(rel_path)
                if rel_path not in local_files_only:
                    log_message(f"需下载: {rel_path}（本地无记录）")
                    local_path_full = os.path.join(local_path, rel_path.replace("/", os.sep))
                    to_download_files.append((get_webdav_path(sync_code, name, rel_path, False), local_path_full, remote_time, remote_md5, rel_path))
                else:
                    local_time = local_file_info["mtime"]
                    local_md5 = local_file_info["md5"]
                    if remote_time > local_time and local_md5 != remote_md5:
                        log_message(f"需更新（远程较新）: {rel_path}")
                        local_path_full = os.path.join(local_path, rel_path.replace("/", os.sep))
                        to_download_files.append((get_webdav_path(sync_code, name, rel_path, False), local_path_full, remote_time, remote_md5, rel_path))
                    elif local_md5 == remote_md5:
                        os.utime(local_file_info["path"], (remote_time.timestamp(), remote_time.timestamp()))
                        log_message(f"文件内容一致（MD5相同，更新本地时间）: {rel_path}")
            log_message(f"待上传文件: {len(to_upload_files)}个")
            for local_file, remote_path, local_time, local_md5, rel_path in to_upload_files:
                if upload_file(local_file, remote_path, info, key, rel_path, local_time, local_md5, config["type"], False):
                    update_info_file(info, configs)
            for remote_path, local_file, remote_time, remote_md5, rel_path in to_download_files:
                if download_file(remote_path, local_file, remote_time, remote_md5, False, info, key, rel_path):
                    update_info_file(info, configs)
        else:
            remote_file = get_webdav_path(sync_code, name)
            with info_lock:
                if key not in info:
                    info[key] = {"type": "F", "sync_code": sync_code, "name": name, "dirs": set(), "files": {}}
                remote_info = info[key]["files"].get(name)
            if os.path.exists(local_path):
                local_time = datetime.datetime.fromtimestamp(os.path.getmtime(local_path)).replace(microsecond=0)
                local_md5 = calculate_md5(local_path)
                if local_md5 is None:
                    log_message(f"文件MD5计算失败，跳过同步: {local_path}")
                    return
                if not remote_info:
                    log_message(f"需上传: {local_path}（远程无记录）")
                    if upload_file(local_path, remote_file, info, key, name, local_time, local_md5, config["type"]):
                        update_info_file(info, configs)
                elif should_sync(local_time, local_md5, remote_info):
                    log_message(f"需更新（本地较新）: {local_path}")
                    if upload_file(local_path, remote_file, info, key, name, local_time, local_md5, config["type"]):
                        update_info_file(info, configs)
                else:
                    # 修复：检查远程是否较新
                    if remote_info["mtime"] > local_time and local_md5 != remote_info["md5"]:
                        log_message(f"需更新（远程较新）: {local_path}")
                        download_file(remote_file, local_path, remote_info["mtime"], remote_info["md5"], False, info, key, name)
                        update_info_file(info, configs)
                    else:
                        log_message(f"文件内容一致（时间差在容错范围内或MD5相同）: {local_path}")
                        with info_lock:
                            info[key]["files"][name]["mtime"] = local_time
                        update_info_file(info, configs)
            else:
                if remote_info:
                    remote_time = remote_info["mtime"]
                    remote_md5 = remote_info["md5"]
                    log_message(f"本地文件不存在，下载: {local_path}")
                    download_file(remote_file, local_path, remote_time, remote_md5, False, info, key, name)
                    update_info_file(info, configs)
                else:
                    log_message(f"本地和远程均无文件: {local_path}")
        log_message(f"=== 同步完成: {key} ===")
    except Exception as e:
        log_message(f"同步异常: {config['path']} ({e})")
        import traceback
        log_message(f"异常堆栈: {traceback.format_exc()}")

def clear_temp_dir():
    try:
        if os.path.exists(TEMP_SYNC_DIR):
            for item in os.listdir(TEMP_SYNC_DIR):
                item_path = os.path.join(TEMP_SYNC_DIR, item)
                try:
                    if os.path.isfile(item_path) or os.path.islink(item_path):
                        os.unlink(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    log_message(f"删除临时文件: {item_path}")
                except Exception as e:
                    log_message(f"删除临时文件失败: {item_path} ({e})")
            log_message(f"临时目录已清空: {TEMP_SYNC_DIR}")
        else:
            os.makedirs(TEMP_SYNC_DIR, exist_ok=True)
            log_message(f"创建临时目录: {TEMP_SYNC_DIR}")
    except Exception as e:
        log_message(f"清理临时目录异常: {e}")

class SyncEventHandler(FileSystemEventHandler):
    def __init__(self, config, info, configs, known_dirs):
        self.config = config
        self.info = info
        self.configs = configs
        self.known_dirs = known_dirs
        self.last_event_time = 0
        self.last_event_path = None
        self.is_directory = config["type"] == "FS"
        self.local_path = config["path"]
        self.sync_code = config["sync_code"]
        self.name = self.local_path.rstrip('\\/').split('\\')[-1] if os.name == 'nt' else self.local_path.rstrip('/').split('/')[-1]
        self.key = f"{self.sync_code} {self.name}"
        self.event_lock = threading.Lock()
        self.folder_rename_mapping = {}
        self.path_sep = os.sep

    def _get_real_is_directory(self, path):
        if os.path.exists(path):
            return os.path.isdir(path)
        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1]
        return not ext and filename not in ["README", "LICENSE", "Makefile", "Dockerfile"]

    def _is_child_of_renamed_folder(self, old_path, new_path):
        old_path_abs = os.path.abspath(old_path) + self.path_sep
        new_path_abs = os.path.abspath(new_path) + self.path_sep
        for old_folder_prefix, new_folder_prefix in self.folder_rename_mapping.items():
            if old_path_abs.startswith(old_folder_prefix) and new_path_abs.startswith(new_folder_prefix):
                return True
        return False

    def _handle_dir_deleted(self, event):
        global pending_batch_dir, recent_deletes_dir
        current_time = time.time()
        rel_path = os.path.relpath(event.src_path, self.local_path).replace("\\", "/").strip("./")
        with pending_delete_dir_lock:
            if pending_batch_dir is None:
                pending_batch_dir = BatchDeleteDir()
                pending_batch_dir.final_timer = threading.Timer(DELAY_FIRST_CHECK, self.process_pending_dir_deletes)
                pending_batch_dir.final_timer.start()
            pending_delete = PendingDeleteDir(path=event.src_path, rel_path=rel_path)
            pending_batch_dir.missing_dirs[event.src_path] = pending_delete
            recent_deletes_dir.append({
                "path": event.src_path,
                "rel_path": rel_path,
                "filename": pending_delete.filename,
                "timestamp": current_time
            })
            log_message(f"检测到文件夹删除: {event.src_path}，加入待处理")

    def _handle_dir_created(self, event):
        global recent_deletes_dir
        current_time = time.time()
        new_dir_name = os.path.basename(event.src_path)
        new_rel_path = os.path.relpath(event.src_path, self.local_path).replace("\\", "/").strip("./")
        with pending_delete_dir_lock:
            valid_deletes = [
                d for d in recent_deletes_dir
                if current_time - d["timestamp"] < MOVE_DETECTION_WINDOW
                and d["filename"] == new_dir_name
                and d["path"] != event.src_path
            ]
            if valid_deletes:
                src_delete = max(valid_deletes, key=lambda x: x["timestamp"])
                old_rel_path = src_delete["rel_path"]
                old_remote_path = get_webdav_path(self.sync_code, self.name, old_rel_path, is_folder=True)
                new_remote_path = get_webdav_path(self.sync_code, self.name, new_rel_path, is_folder=True)
                log_message(f"识别为文件夹移动: {src_delete['path']} -> {event.src_path}")
                if move_webdav_directory(old_remote_path, new_remote_path, self.info, self.key, old_rel_path, new_rel_path, self.known_dirs):
                    if pending_batch_dir and src_delete["path"] in pending_batch_dir.missing_dirs:
                        pending_batch_dir.missing_dirs[src_delete["path"]].is_moved = True
                    recent_deletes_dir[:] = [d for d in recent_deletes_dir if d["path"] != src_delete["path"]]
                    update_info_file(self.info, self.configs)
                return
        remote_path = get_webdav_path(self.sync_code, self.name, new_rel_path, is_folder=True)
        if create_webdav_directory(remote_path, self.known_dirs):
            with info_lock:
                self.info[self.key]["dirs"].add(new_rel_path)
            update_info_file(self.info, self.configs)

    def process_pending_dir_deletes(self):
        global pending_batch_dir
        with pending_delete_dir_lock:
            if pending_batch_dir is None:
                return
            if not pending_batch_dir.first_check_done:
                pending_batch_dir.first_check_done = True
                still_missing = {}
                for path, pending in pending_batch_dir.missing_dirs.items():
                    if not os.path.exists(path) and not pending.is_moved:
                        still_missing[path] = pending
                        log_message(f"首次检查仍未找到: {path} (文件夹)")
                    else:
                        log_message(f"确认文件夹已移动或恢复: {path}")
                pending_batch_dir.missing_dirs = still_missing
                if still_missing:
                    pending_batch_dir.final_timer = threading.Timer(DELAY_FINAL_ACTION - DELAY_FIRST_CHECK, self.process_pending_dir_deletes)
                    pending_batch_dir.final_timer.start()
                else:
                    pending_batch_dir = None
                return
            for path, pending in pending_batch_dir.missing_dirs.items():
                if not os.path.exists(path) and not pending.is_moved:
                    remote_path = get_webdav_path(self.sync_code, self.name, pending.rel_path, is_folder=True)
                    log_message(f"执行删除文件夹: {path} -> {remote_path}")
                    if delete_webdav_file(remote_path, self.info, self.key, pending.rel_path, is_directory=True):
                        update_info_file(self.info, self.configs)
            recent_deletes_dir[:] = [
                d for d in recent_deletes_dir
                if time.time() - d["timestamp"] < MOVE_DETECTION_WINDOW
            ]
            pending_batch_dir = None

    def dispatch(self, event):
        global pending_batch, recent_deletes, pending_batch_dir, recent_deletes_dir
        current_time = time.time()
        with self.event_lock:
            if event.src_path in ignored_files:
                return
            if current_time - self.last_event_time < DEBOUNCE_INTERVAL and event.src_path == self.last_event_path:
                return
            self.last_event_time = current_time
            self.last_event_path = event.src_path
        real_is_dir = self._get_real_is_directory(event.src_path)
        if event.event_type == 'moved':
            dest_real_is_dir = self._get_real_is_directory(event.dest_path)
            event.is_directory = real_is_dir and dest_real_is_dir
        else:
            event.is_directory = real_is_dir
        if self.is_directory and event.src_path.startswith(self.local_path):
            if event.is_directory and event.event_type == 'moved':
                old_folder_abs = os.path.abspath(event.src_path) + self.path_sep
                new_folder_abs = os.path.abspath(event.dest_path) + self.path_sep
                self.folder_rename_mapping[old_folder_abs] = new_folder_abs
                threading.Timer(5, lambda: self.folder_rename_mapping.pop(old_folder_abs, None)).start()
                old_rel_path = os.path.relpath(event.src_path, self.local_path).replace("\\", "/").strip("./")
                new_rel_path = os.path.relpath(event.dest_path, self.local_path).replace("\\", "/").strip("./")
                old_remote = get_webdav_path(self.sync_code, self.name, old_rel_path, True)
                new_remote = get_webdav_path(self.sync_code, self.name, new_rel_path, True)
                log_message(f"检测到文件夹移动/重命名: {event.src_path} -> {event.dest_path}")
                if move_webdav_directory(old_remote, new_remote, self.info, self.key, old_rel_path, new_rel_path, self.known_dirs):
                    update_info_file(self.info, self.configs)
                return
            if event.is_directory and event.event_type == 'deleted':
                self._handle_dir_deleted(event)
                return
            if event.is_directory and event.event_type == 'created':
                self._handle_dir_created(event)
                return
            if not event.is_directory:
                rel_path = os.path.relpath(event.src_path, self.local_path).replace("\\", "/").strip("./")
                remote_path = get_webdav_path(self.sync_code, self.name, rel_path, False)
                if event.event_type == 'created':
                    filename = os.path.basename(event.src_path)
                    with pending_delete_lock:
                        recent_deletes = [d for d in recent_deletes if current_time - d["timestamp"] < MOVE_DETECTION_WINDOW]
                        for delete_event in recent_deletes[:]:
                            if delete_event["filename"] == filename and delete_event["path"] != event.src_path:
                                old_rel_path = os.path.relpath(delete_event["path"], self.local_path).replace("\\", "/").strip("./")
                                old_remote_path = get_webdav_path(self.sync_code, self.name, old_rel_path, False)
                                local_time = datetime.datetime.fromtimestamp(os.path.getmtime(event.src_path)).replace(microsecond=0)
                                local_md5 = calculate_md5(event.src_path)
                                if local_md5 is None:
                                    log_message(f"文件MD5计算失败，跳过移动: {delete_event['path']} -> {event.src_path}")
                                    return
                                log_message(f"识别为文件移动: {delete_event['path']} -> {event.src_path}")
                                if rename_remote_file(old_remote_path, remote_path, self.info, self.key, old_rel_path, rel_path, local_time, local_md5):
                                    if pending_batch and delete_event["path"] in pending_batch.missing_files:
                                        pending_batch.missing_files[delete_event["path"]].is_moved = True
                                    recent_deletes.remove(delete_event)
                                    update_info_file(self.info, self.configs)
                                return
                        with info_lock:
                            if self.key in self.info and rel_path in self.info[self.key]["files"]:
                                log_message(f"文件已在同步记录中: {rel_path}，跳过上传")
                                return
                        local_time = datetime.datetime.fromtimestamp(os.path.getmtime(event.src_path)).replace(microsecond=0)
                        local_md5 = calculate_md5(event.src_path)
                        if local_md5 is None:
                            log_message(f"文件MD5计算失败，跳过新建同步: {event.src_path}")
                            return
                        log_message(f"检测到文件新建: {event.src_path}")
                        if upload_file(event.src_path, remote_path, self.info, self.key, rel_path, local_time, local_md5, self.config["type"], False):
                            update_info_file(self.info, self.configs)
                    return
                elif event.event_type == 'modified':
                    local_time = datetime.datetime.fromtimestamp(os.path.getmtime(event.src_path)).replace(microsecond=0)
                    local_md5 = calculate_md5(event.src_path)
                    if local_md5 is None:
                        log_message(f"文件MD5计算失败，跳过修改同步: {event.src_path}")
                        return
                    with info_lock:
                        remote_info = self.info[self.key]["files"].get(rel_path) if self.key in self.info else None
                    if remote_info and local_md5 == remote_info["md5"]:
                        remote_time_trunc = remote_info["mtime"].replace(microsecond=0)
                        time_diff = (local_time - remote_time_trunc).total_seconds()
                        if abs(time_diff) <= TIME_TOLERANCE:
                            log_message(f"文件内容一致（时间差在容错范围内）: {rel_path}")
                            return
                    if not remote_info or should_sync(local_time, local_md5, remote_info):
                        log_message(f"检测到文件修改: {event.src_path}")
                        if upload_file(event.src_path, remote_path, self.info, self.key, rel_path, local_time, local_md5, self.config["type"], False):
                            update_info_file(self.info, self.configs)
                    elif remote_info["mtime"] > local_time and local_md5 != remote_info["md5"]:
                        log_message(f"需更新（远程较新）: {rel_path}")
                        if download_file(remote_path, event.src_path, remote_info["mtime"], remote_info["md5"], False, self.info, self.key, rel_path):
                            update_info_file(self.info, self.configs)
                    else:
                        log_message(f"文件内容一致（MD5相同，更新本地时间）: {rel_path}")
                        os.utime(event.src_path, (remote_info["mtime"].timestamp(), remote_info["mtime"].timestamp()))
                    return
                elif event.event_type == 'deleted':
                    with pending_delete_lock:
                        if pending_batch is None:
                            pending_batch = BatchDelete()
                            pending_batch.final_timer = threading.Timer(DELAY_FIRST_CHECK, self.process_pending_deletes)
                            pending_batch.final_timer.start()
                        pending_delete = PendingDelete(path=event.src_path, is_directory=event.is_directory, filename=rel_path)
                        pending_batch.missing_files[event.src_path] = pending_delete
                        recent_deletes.append({"path": event.src_path, "filename": os.path.basename(event.src_path), "timestamp": current_time})
                        log_message(f"检测到文件删除: {event.src_path}，加入待处理")
                    return
                elif event.event_type == 'moved':
                    if self._is_child_of_renamed_folder(event.src_path, event.dest_path):
                        log_message(f"忽略子文件假移动（因文件夹重命名）: {event.src_path} -> {event.dest_path}")
                        return
                    old_rel_path = os.path.relpath(event.src_path, self.local_path).replace("\\", "/").strip("./")
                    new_rel_path = os.path.relpath(event.dest_path, self.local_path).replace("\\", "/").strip("./")
                    old_remote_path = get_webdav_path(self.sync_code, self.name, old_rel_path, False)
                    new_remote_path = get_webdav_path(self.sync_code, self.name, new_rel_path, False)
                    local_time = datetime.datetime.fromtimestamp(os.path.getmtime(event.dest_path)).replace(microsecond=0)
                    local_md5 = calculate_md5(event.dest_path)
                    if local_md5 is None:
                        log_message(f"文件MD5计算失败，跳过移动: {event.src_path} -> {event.dest_path}")
                        return
                    log_message(f"检测到文件移动: {event.src_path} -> {event.dest_path}")
                    if rename_remote_file(old_remote_path, new_remote_path, self.info, self.key, old_rel_path, new_rel_path, local_time, local_md5):
                        update_info_file(self.info, self.configs)
                    return
    def process_pending_deletes(self):
        global pending_batch
        with pending_delete_lock:
            if pending_batch is None:
                return
            if not pending_batch.first_check_done:
                pending_batch.first_check_done = True
                still_missing = {}
                for path, pending in pending_batch.missing_files.items():
                    if not os.path.exists(path) and not pending.is_moved:
                        still_missing[path] = pending
                        log_message(f"首次检查仍未找到: {path} (文件)")
                    else:
                        log_message(f"确认文件已移动或恢复: {path}")
                pending_batch.missing_files = still_missing
                if still_missing:
                    pending_batch.final_timer = threading.Timer(DELAY_FINAL_ACTION - DELAY_FIRST_CHECK, self.process_pending_deletes)
                    pending_batch.final_timer.start()
                else:
                    pending_batch = None
                return
            for path, pending in pending_batch.missing_files.items():
                if not os.path.exists(path) and not pending.is_moved:
                    rel_path = pending.filename
                    remote_path = get_webdav_path(self.sync_code, self.name, rel_path, pending.is_directory)
                    log_message(f"执行删除文件: {path} -> {remote_path}")
                    if delete_webdav_file(remote_path, self.info, self.key, rel_path, pending.is_directory):
                        update_info_file(self.info, self.configs)
            pending_batch = None

def main():
    clear_temp_dir()
    mutex = check_single_instance()
    configs, webdav_config = load_config()
    global WEBDAV_URL, USERNAME, PASSWORD
    WEBDAV_URL = webdav_config['url']
    USERNAME = webdav_config['username']
    PASSWORD = webdav_config['password']
    if not configs:
        log_message("无有效同步任务，退出")
        sys.exit(1)
    info_path = download_info_file()
    if not info_path:
        log_message("无法加载同步记录，退出")
        sys.exit(1)
    info, known_dirs = parse_info_file(info_path)
    if os.path.exists(info_path):
        os.remove(info_path)
    observers = []
    log_message("=== 启动实时监控 ===")
    for config in configs:
        if config["type"] == "FS" and os.path.exists(config["path"]):
            event_handler = SyncEventHandler(config, info, configs, known_dirs)
            observer = Observer()
            observer.schedule(event_handler, config["path"], recursive=True)
            observer.start()
            observers.append(observer)
            log_message(f"监控启动: {config['path']}（类型: {config['type']}）")
    log_message("=== 初始同步 ===")
    for config in configs:
        sync_folder(config, info, known_dirs, configs)
    log_message("=== 初始同步完成 ===")
    try:
        while True:
            time.sleep(SYNC_INTERVAL)
            log_message("=== 定期同步 ===")
            for config in configs:
                sync_folder(config, info, known_dirs, configs)
            log_message("=== 定期同步完成 ===")
    except KeyboardInterrupt:
        log_message("程序终止")
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join()
        sys.exit(0)

if __name__ == "__main__":
    main()
