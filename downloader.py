import os
import sys
import io
import re
import json
import time
import argparse
import urllib.request
import urllib.parse
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force UTF-8 stdout and stderr
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# Locate local bin/ directory with bundled ffmpeg, ffprobe, ffplay
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLED_BIN_DIR = os.path.join(SCRIPT_DIR, 'bin')
if not os.path.exists(BUNDLED_BIN_DIR):
    # Try one level up if packaged inside resources/app
    parent_bin = os.path.join(os.path.dirname(SCRIPT_DIR), 'bin')
    if os.path.exists(parent_bin):
        BUNDLED_BIN_DIR = parent_bin

# Prepend local bin directory to PATH so local ffmpeg is strictly preferred over system PATH
if os.path.exists(BUNDLED_BIN_DIR):
    os.environ['PATH'] = BUNDLED_BIN_DIR + os.pathsep + os.environ.get('PATH', '')

FFMPEG_EXE = os.path.join(BUNDLED_BIN_DIR, 'ffmpeg.exe') if os.name == 'nt' else os.path.join(BUNDLED_BIN_DIR, 'ffmpeg')
if not os.path.exists(FFMPEG_EXE):
    FFMPEG_EXE = 'ffmpeg'

import yt_dlp
import yt_dlp.extractor.soundcloud
from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError
try:
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:
    MP4 = None

# Patch SoundCloud extractor to avoid slow 20s HEADRequest thumbnail checks that time out
try:
    def _fast_sc_thumbnails(self, info):
        artwork_url = info.get('artwork_url') or info.get('user', {}).get('avatar_url')
        if artwork_url:
            artwork_url = upgrade_soundcloud_artwork(artwork_url)
        return [{'url': artwork_url, 'preference': 10}] if artwork_url else []
    yt_dlp.extractor.soundcloud.SoundcloudIE._extract_thumbnails = _fast_sc_thumbnails
except Exception:
    pass

def upgrade_soundcloud_artwork(url):
    if not url or 'sndcdn.com' not in url:
        return url
    # High resolution artwork variants in order of quality
    candidates = [
        re.sub(r'-[a-zA-Z0-9]+\.([a-zA-Z0-9]+)$', r'-t1080x1080.\1', url),
        re.sub(r'-[a-zA-Z0-9]+\.([a-zA-Z0-9]+)$', r'-original.\1', url),
        re.sub(r'-[a-zA-Z0-9]+\.([a-zA-Z0-9]+)$', r'-t500x500.\1', url)
    ]
    for cand in candidates:
        if cand == url:
            continue
        try:
            req = urllib.request.Request(cand, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return cand
        except Exception:
            continue
    return url

def download_and_format_artwork_1024(artwork_url, target_path):
    """Downloads artwork and ensures high resolution 1024x1024 PNG/JPG"""
    for attempt in range(2):
        try:
            req = urllib.request.Request(artwork_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            raw_bytes = urllib.request.urlopen(req, timeout=7).read()
            if not raw_bytes:
                continue

            # Try formatting with PIL if installed
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(raw_bytes))
                # Convert to RGB if necessary
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGB')
                # Resize or scale up/down to 1024x1024 high quality
                img = img.resize((1024, 1024), Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.ANTIALIAS)
                img.save(target_path, 'JPEG', quality=95)
                return True
            except Exception:
                # Fallback to direct raw write
                with open(target_path, 'wb') as f:
                    f.write(raw_bytes)
                return True
        except Exception:
            time.sleep(0.3)
    return False

def get_downloads_dir():
    if os.name == 'nt':
        import winreg
        try:
            sub_key = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'
            downloads_guid = '{374DE290-123F-4565-9164-39C4925E467B}'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                return winreg.QueryValueEx(key, downloads_guid)[0]
        except Exception:
            return os.path.join(os.path.expanduser('~'), 'Downloads')
    return os.path.join(os.path.expanduser('~'), 'Downloads')

def log_msg(msg):
    print(msg, flush=True)

def make_safe_filename(title):
    safe = re.sub(r'[\\/*?:"<>|]', "", title).strip()
    return safe or "Track"

def yt_dlp_hook(d):
    status = d.get('status')
    if status == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        downloaded = d.get('downloaded_bytes', 0)
        speed = d.get('speed', 0)
        eta = d.get('eta', 0)
        frag_idx = d.get('fragment_index')
        frag_count = d.get('fragment_count')
        
        speed_str = f"{speed / (1024 * 1024):.2f}MiB/s" if speed else "N/A"
        pct_num = (downloaded / total * 100) if total > 0 else 0.0
        pct_str = f"{pct_num:5.1f}%" if total > 0 else "  ...%"
        total_str = f"{total / (1024 * 1024):.2f}MiB" if total > 0 else "..."
        
        if eta is not None:
            mins, secs = divmod(int(eta), 60)
            eta_str = f"{mins:02d}:{secs:02d}"
        else:
            eta_str = "--:--"

        frag_info = f" (frag {frag_idx}/{frag_count})" if frag_idx and frag_count else ""
        formatted_line = f"[download] {pct_str} of ~ {total_str:>9} at {speed_str:>11} ETA {eta_str}{frag_info}"
        
        print(f"__PROGRESS__:{pct_num:.2f}", flush=True)
        log_msg(formatted_line)
    elif status == 'finished':
        print("__PROGRESS__:100.00", flush=True)
        log_msg("[Обработка] Загрузка потоков завершена, объединение и кодирование звука 320 kbps...")

def find_soundcloud_client_id(html):
    js_urls = re.findall(r'src="(https://a-v2\.sndcdn\.com/[^"]+\.js)"', html)
    for js_url in js_urls:
        try:
            req = urllib.request.Request(js_url, headers={'User-Agent': 'Mozilla/5.0'})
            js_code = urllib.request.urlopen(req, timeout=5).read().decode('utf-8', errors='ignore')
            m = re.search(r'client_id[:=]\s*["\']([a-zA-Z0-9]{32})["\']', js_code)
            if m:
                return m.group(1)
        except Exception:
            pass
    return "bQmaW4vAHmJIUxisgjRG4MAWAiDgsk9D"

def search_alternative_soundcloud_stream(orig_url, orig_title, orig_artist):
    log_msg("[Резерв] Поиск доступной альтернативной копии трека на SoundCloud...")
    try:
        req = urllib.request.Request(orig_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        html = urllib.request.urlopen(req, timeout=7).read().decode('utf-8', errors='ignore')
        client_id = find_soundcloud_client_id(html)
    except Exception:
        client_id = "bQmaW4vAHmJIUxisgjRG4MAWAiDgsk9D"

    clean_title = re.sub(r'[\(\[\{].*?[\)\]\}]', '', orig_title).strip()
    query_variants = [
        f"{orig_title}",
        f"{clean_title} {orig_artist}".strip(),
        f"{clean_title}".strip()
    ]

    for q in query_variants:
        if not q:
            continue
        try:
            search_api = f"https://api-v2.soundcloud.com/search/tracks?q={urllib.parse.quote(q)}&client_id={client_id}&limit=12"
            req = urllib.request.Request(search_api, headers={'User-Agent': 'Mozilla/5.0'})
            res = json.loads(urllib.request.urlopen(req, timeout=7).read().decode('utf-8'))
            collection = res.get('collection', [])
            
            for track in collection:
                media = track.get('media', {})
                transcodings = media.get('transcodings', [])
                protocols = [t.get('format', {}).get('protocol') for t in transcodings]
                if any(p in ['hls', 'progressive'] for p in protocols):
                    alt_url = track.get('permalink_url')
                    alt_title = track.get('title')
                    if alt_url and alt_url.lower() != orig_url.lower():
                        log_msg(f"[Резерв] Найдена доступная открытая версия: «{alt_title}» ({alt_url})")
                        return alt_url
        except Exception as e:
            log_msg(f"[Резерв] Ошибка при поиске '{q}': {e}")
            
    return None

def fetch_info(url):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    
    clean_url = url.lower()
    platform = 'youtube' if ('youtube.com' in clean_url or 'youtu.be' in clean_url) else ('soundcloud' if 'soundcloud.com' in clean_url else ('tiktok' if ('tiktok.com' in clean_url or 'douyin.com' in clean_url) else ('pinterest' if ('pinterest.' in clean_url or 'pin.it' in clean_url) else 'unknown')))

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title') or 'Без названия'
            author = info.get('uploader') or info.get('channel') or info.get('artist') or 'Неизвестный автор'
            thumbnail = info.get('thumbnail') or info.get('artwork_url') or ''

            if platform == 'soundcloud':
                thumbnail = upgrade_soundcloud_artwork(thumbnail)

            qualities = []
            if platform == 'youtube':
                formats = info.get('formats', [])
                available_heights = set()
                for f in formats:
                    h = f.get('height')
                    if h and f.get('vcodec') != 'none':
                        available_heights.add(int(h))

                target_specs = [
                    (144, "144p"),
                    (240, "240p"),
                    (360, "360p"),
                    (720, "720p"),
                    (1080, "1080p"),
                    (1440, "1440p"),
                    (2160, "2160p")
                ]

                # Always include resolutions, ensuring 1440p and 2160p (4k) are available options
                qualities = ["144p", "240p", "360p", "720p", "1080p", "1440p", "2160p"]

            result = {
                "success": True,
                "platform": platform,
                "title": title,
                "author": author,
                "thumbnail": thumbnail,
                "qualities": qualities,
                "duration": info.get('duration')
            }
            print("__INFO_JSON__:" + json.dumps(result, ensure_ascii=False), flush=True)
            return True
    except Exception as e:
        # Fallback metadata extraction via page scrape / hydration
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            html = urllib.request.urlopen(req, timeout=6).read().decode('utf-8', errors='ignore')
            
            fb_title = None
            fb_author = None
            fb_thumb = None

            m_title = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html)
            if m_title:
                fb_title = urllib.parse.unquote(m_title.group(1))

            m_image = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\'](.*?)["\']', html)
            if m_image:
                fb_thumb = m_image.group(1)

            if platform == 'soundcloud':
                h = re.findall(r'window\.__sc_hydration\s*=\s*(\[.*?\]);\s*</script>', html, re.DOTALL)
                if h:
                    try:
                        d = json.loads(h[0])
                        for item in d:
                            if item.get('hydratable') == 'sound':
                                sdata = item.get('data', {})
                                fb_title = sdata.get('title') or fb_title
                                fb_author = sdata.get('user', {}).get('username') or fb_author
                                fb_thumb = sdata.get('artwork_url') or fb_thumb
                    except Exception:
                        pass
                if not fb_author:
                    m_artist = re.search(r'<meta\s+property=["\']twitter:audio:artist_name["\']\s+content=["\'](.*?)["\']', html)
                    if m_artist:
                        fb_author = m_artist.group(1)
            elif platform == 'youtube':
                m_author = re.search(r'<link\s+itemprop=["\']name["\']\s+content=["\'](.*?)["\']', html)
                if m_author:
                    fb_author = m_author.group(1)
            elif platform == 'tiktok':
                m_author = re.search(r'<meta\s+name=["\']author["\']\s+content=["\'](.*?)["\']', html)
                if not m_author:
                    m_author = re.search(r'<meta\s+property=["\']twitter:creator["\']\s+content=["\'](.*?)["\']', html)
                if m_author:
                    fb_author = m_author.group(1)
            elif platform == 'pinterest':
                m_author = re.search(r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']', html)
                if m_author:
                    fb_author = m_author.group(1)[:40]

            if fb_title:
                if platform == 'soundcloud' and fb_thumb:
                    fb_thumb = upgrade_soundcloud_artwork(fb_thumb)

                fb_res = {
                    "success": True,
                    "platform": platform,
                    "title": fb_title,
                    "author": fb_author or ('SoundCloud' if platform == 'soundcloud' else ('Pinterest' if platform == 'pinterest' else 'YouTube')),
                    "thumbnail": fb_thumb or '',
                    "qualities": ["144p", "240p", "360p", "720p", "1080p", "1440p", "2160p"] if platform == 'youtube' else [],
                    "duration": None
                }
                print("__INFO_JSON__:" + json.dumps(fb_res, ensure_ascii=False), flush=True)
                return True
        except Exception:
            pass

        err_res = {
            "success": False,
            "error": str(e)
        }
        print("__INFO_JSON__:" + json.dumps(err_res, ensure_ascii=False), flush=True)
        return False

def get_stream_size(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Range': 'bytes=0-0',
                'Connection': 'close'
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                cr = resp.headers.get('Content-Range')
                if cr and '/' in cr:
                    return int(cr.split('/')[-1])
        except Exception:
            time.sleep(0.2)
    return None

def download_chunk(url, start, end, max_retries=6):
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Range': f'bytes={start}-{end}',
                'Connection': 'close'
            })
            with urllib.request.urlopen(req, timeout=7) as resp:
                return resp.read()
        except Exception:
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"Не удалось загрузить фрагмент {start}-{end}")

def download_stream_to_file(stream_url, target_path, filesize=None):
    if not filesize:
        filesize = get_stream_size(stream_url)
    if not filesize:
        raise ValueError("Не удалось определить размер потока")

    chunk_size = 16384
    num_chunks = (filesize + chunk_size - 1) // chunk_size
    chunks_data = [None] * num_chunks

    downloaded_bytes = 0
    t_start = time.time()
    last_log_time = 0

    def task(idx):
        start = idx * chunk_size
        end = min(filesize - 1, (idx + 1) * chunk_size - 1)
        data = download_chunk(stream_url, start, end)
        return idx, data

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(task, i) for i in range(num_chunks)]
        for f in as_completed(futures):
            idx, data = f.result()
            chunks_data[idx] = data
            downloaded_bytes += len(data)

            now = time.time()
            if now - last_log_time >= 0.25 or downloaded_bytes == filesize:
                last_log_time = now
                elapsed = max(now - t_start, 0.001)
                speed = downloaded_bytes / elapsed
                speed_str = f"{speed / (1024 * 1024):.2f}MiB/s"
                pct = (downloaded_bytes / filesize) * 100.0
                pct_str = f"{pct:5.1f}%"
                total_str = f"{filesize / (1024 * 1024):.2f}MiB"
                remaining = (filesize - downloaded_bytes) / max(speed, 1)
                mins, secs = divmod(int(remaining), 60)
                eta_str = f"{mins:02d}:{secs:02d}"

                print(f"__PROGRESS__:{pct:.2f}", flush=True)
                log_msg(f"[download] {pct_str} of ~ {total_str:>9} at {speed_str:>11} ETA {eta_str}")

    with open(target_path, 'wb') as out_f:
        for c in chunks_data:
            out_f.write(c)

    return True

def download_hls_to_file(m3u8_url, target_path):
    req = urllib.request.Request(m3u8_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Connection': 'close'
    })
    m3u8_text = urllib.request.urlopen(req, timeout=7).read().decode('utf-8')
    lines = [l.strip() for l in m3u8_text.splitlines() if l.strip()]
    segments = []
    init_url = None
    for l in lines:
        if l.startswith('#EXT-X-MAP:URI='):
            init_url = l.split(chr(34))[1]
        elif not l.startswith('#'):
            segments.append(l)

    all_urls = ([init_url] if init_url else []) + segments
    total_segs = len(all_urls)
    seg_buffers = [None] * total_segs
    
    last_log_time = 0
    downloaded_count = 0

    def download_segment(idx, seg_url):
        size = get_stream_size(seg_url)
        if not size:
            req_s = urllib.request.Request(seg_url, headers={'User-Agent': 'Mozilla/5.0', 'Connection': 'close'})
            return idx, urllib.request.urlopen(req_s, timeout=8).read()

        chunk_size = 16384
        n_chunks = (size + chunk_size - 1) // chunk_size
        parts = []
        for c_i in range(n_chunks):
            start = c_i * chunk_size
            end = min(size - 1, (c_i + 1) * chunk_size - 1)
            parts.append(download_chunk(seg_url, start, end))
        return idx, b''.join(parts)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(download_segment, i, u) for i, u in enumerate(all_urls)]
        for f in as_completed(futures):
            idx, data = f.result()
            seg_buffers[idx] = data
            downloaded_count += 1
            now = time.time()
            if now - last_log_time >= 0.25 or downloaded_count == total_segs:
                last_log_time = now
                pct = (downloaded_count / total_segs) * 100.0
                print(f"__PROGRESS__:{pct:.2f}", flush=True)
                log_msg(f"[download] {pct:5.1f}% (сегмент {downloaded_count}/{total_segs})")

    with open(target_path, 'wb') as out_f:
        for b in seg_buffers:
            if b:
                out_f.write(b)
    return True

def transcode_to_320k_mp3(input_path, output_mp3_path):
    print("__PROGRESS__:100.00", flush=True)
    log_msg("[Обработка] Загрузка потоков завершена, объединение и кодирование звука 320 kbps...")
    cmd = [
        FFMPEG_EXE, '-y', '-hide_banner', '-loglevel', 'error',
        '-i', input_path,
        '-b:a', '320k',
        '-ar', '44100',
        output_mp3_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if res.returncode != 0:
        err = res.stderr.decode('utf-8', errors='ignore')
        raise RuntimeError(f"FFmpeg ошибка кодирования: {err}")
    return True

def embed_mp3_id3(mp3_path, title, artist, artwork_url=None):
    log_msg("[Теги] Запись ID3 тегов (Название, Автор)...")
    try:
        try:
            audio = ID3(mp3_path)
        except ID3NoHeaderError:
            audio = ID3()
        audio['TIT2'] = TIT2(encoding=3, text=title)
        audio['TPE1'] = TPE1(encoding=3, text=artist)

        if artwork_url:
            artwork_url = upgrade_soundcloud_artwork(artwork_url)
            for art_attempt in range(2):
                try:
                    req = urllib.request.Request(artwork_url, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Connection': 'close'
                    })
                    art_data = urllib.request.urlopen(req, timeout=5).read()
                    if art_data:
                        # Try formatting / resizing to crisp 1024x1024 for ID3 cover
                        try:
                            from PIL import Image
                            img = Image.open(io.BytesIO(art_data))
                            if img.mode not in ('RGB', 'RGBA'):
                                img = img.convert('RGB')
                            img = img.resize((1024, 1024), Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.ANTIALIAS)
                            out_buf = io.BytesIO()
                            img.save(out_buf, 'JPEG', quality=95)
                            art_data = out_buf.getvalue()
                        except Exception:
                            pass
                        mime = 'image/png' if art_data.startswith(b'\x89PNG') else 'image/jpeg'
                        audio['APIC'] = APIC(
                            encoding=3,
                            mime=mime,
                            type=3,
                            desc='Cover',
                            data=art_data
                        )
                        break
                except Exception:
                    pass

        audio.save(mp3_path)
        log_msg("[Теги] ID3 теги успешно сохранены!")
    except Exception as e:
        log_msg(f"[Предупреждение] Ошибка записи ID3: {e}")

def finish_video_metadata(filepath, title, author):
    log_msg(f"[Метаданные] Запись автора «{author}» и названия «{title}» в свойства видеофайла...")
    if not os.path.exists(filepath):
        return
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.mp4' and MP4 is not None:
        try:
            video = MP4(filepath)
            if video.tags is None:
                video.add_tags()
            video['\xa9nam'] = [title]
            video['\xa9ART'] = [author]
            video['aART'] = [author]
            video['\xa9wrt'] = [author]
            video['desc'] = [author]
            video.save()
            log_msg("[Метаданные] Свойства автора и названия успешно внедрены в файл!")
        except Exception as e:
            log_msg(f"[Предупреждение] Ошибка записи метаданных MP4: {e}")

def download_soundcloud(url, mode="audio"):
    downloads_path = get_downloads_dir()
    log_msg(f"[Инфо] Папка для загрузки: {downloads_path}")
    log_msg(f"[Инфо] Обработка SoundCloud ссылки ({'Только обложка 1024x1024' if mode == 'cover' else 'MP3 320 kbps'}): {url}")
    
    info = None
    ydl_opts = {'quiet': True, 'no_warnings': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            log_msg("[Инфо] Получение метаданных SoundCloud...")
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log_msg(f"[Предупреждение] Ошибка извлечения метаданных SoundCloud: {e}")

    if not info:
        log_msg("[ОШИБКА] Не удалось получить метаданные трека SoundCloud.")
        return False

    title = info.get('title') or 'SoundCloud Track'
    uploader = info.get('uploader') or info.get('artist') or 'Unknown Artist'
    artwork_url = info.get('artwork_url') or info.get('thumbnail')
    artwork_url = upgrade_soundcloud_artwork(artwork_url)
    safe_title = make_safe_filename(title)

    if mode == 'cover':
        final_cover = os.path.join(downloads_path, f"{safe_title}_cover_1024x1024.jpg")
        log_msg(f"[Инфо] Загрузка обложки в качестве 1:1 (1024x1024)...")
        print("__PROGRESS__:50.00", flush=True)
        if artwork_url and download_and_format_artwork_1024(artwork_url, final_cover):
            print("__PROGRESS__:100.00", flush=True)
            log_msg(f"[УСПЕХ] Обложка 1024x1024 сохранена: {final_cover}")
            log_msg("=== ЗАГРУЗКА ЗАВЕРШЕНА ===")
            return True
        else:
            log_msg(f"[ОШИБКА] Не удалось скачать обложку для трека «{title}».")
            return False

    formats = info.get('formats') or []
    final_mp3 = os.path.join(downloads_path, f"{safe_title}.mp3")
    temp_raw = os.path.join(downloads_path, f".tmp_{safe_title}.raw")

    log_msg(f"[Инфо] Трек: «{title}»")
    log_msg(f"[Инфо] Автор: «{uploader}»")

    # Clean up any leftover debris before starting
    for ext in ['.tmp', '.raw', '.part', '.m4a', '.jpg', '.jpeg', '.png', '.webp']:
        p = os.path.join(downloads_path, safe_title + ext)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    success = False
    try:
        http_fmt = next((f for f in formats if 'http' in f.get('protocol', '') and f.get('url')), None)
        hls_fmt = next((f for f in formats if 'm3u8' in f.get('protocol', '') and f.get('url')), None)

        if http_fmt:
            log_msg(f"[Инфо] Загрузка прямого аудиопотока ({http_fmt.get('format_id')}) через защищенные параллельные чанки...")
            download_stream_to_file(http_fmt['url'], temp_raw, filesize=http_fmt.get('filesize') or http_fmt.get('filesize_approx'))
            transcode_to_320k_mp3(temp_raw, final_mp3)
            embed_mp3_id3(final_mp3, title, uploader, artwork_url)
            success = True
        elif hls_fmt:
            log_msg(f"[Инфо] Загрузка HLS потока ({hls_fmt.get('format_id')}) через защищенные параллельные чанки...")
            download_hls_to_file(hls_fmt['url'], temp_raw)
            transcode_to_320k_mp3(temp_raw, final_mp3)
            embed_mp3_id3(final_mp3, title, uploader, artwork_url)
            success = True
        else:
            raise RuntimeError("Не найдены доступные потоки для трека")

    except Exception as stream_err:
        log_msg(f"[Предупреждение] Ошибка загрузки основного потока SoundCloud: {stream_err}")
        alt_url = search_alternative_soundcloud_stream(url, title, uploader)
        if alt_url:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_alt:
                    info_alt = ydl_alt.extract_info(alt_url, download=False)
                    alt_formats = info_alt.get('formats') or []
                    alt_http = next((f for f in alt_formats if 'http' in f.get('protocol', '') and f.get('url')), None)
                    alt_hls = next((f for f in alt_formats if 'm3u8' in f.get('protocol', '') and f.get('url')), None)
                    if alt_http:
                        download_stream_to_file(alt_http['url'], temp_raw, filesize=alt_http.get('filesize') or alt_http.get('filesize_approx'))
                    elif alt_hls:
                        download_hls_to_file(alt_hls['url'], temp_raw)
                    transcode_to_320k_mp3(temp_raw, final_mp3)
                    embed_mp3_id3(final_mp3, title, uploader, artwork_url)
                    success = True
            except Exception as alt_e:
                log_msg(f"[ОШИБКА] Ошибка загрузки резервной копии: {alt_e}")

    finally:
        if os.path.exists(temp_raw):
            try:
                os.remove(temp_raw)
            except Exception:
                pass
        # Clean up any stray loose image files or m4a created
        for ext in ['.jpg', '.jpeg', '.png', '.webp', '.m4a', '.part']:
            loose = os.path.join(downloads_path, safe_title + ext)
            if os.path.exists(loose):
                try:
                    os.remove(loose)
                except Exception:
                    pass

    if success and os.path.exists(final_mp3):
        log_msg(f"[УСПЕХ] Файл сохранен: {final_mp3}")
        log_msg("=== ЗАГРУЗКА ЗАВЕРШЕНА ===")
        return True
    else:
        log_msg(f"[ОШИБКА] Не удалось скачать трек «{title}».")
        return False

def download_tiktok(url, mode="no_watermark"):
    downloads_path = get_downloads_dir()
    mode_titles = {
        'no_watermark': 'Без водяного знака',
        'watermark': 'С водяным знаком',
        'audio': 'Только звук'
    }
    mode_name = mode_titles.get(mode, 'Без водяного знака')
    log_msg(f"[Инфо] Папка для загрузки: {downloads_path}")
    log_msg(f"[Инфо] TikTok: режим скачивания «{mode_name}» ({mode})")

    out_tmpl = os.path.join(downloads_path, '%(title)s.%(ext)s')

    if mode == 'audio':
        ydl_opts = {
            'ffmpeg_location': BUNDLED_BIN_DIR,
            'format': 'bestaudio/audio/best',
            'outtmpl': out_tmpl,
            'writethumbnail': True,
            'progress_hooks': [yt_dlp_hook],
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320',
                },
                {
                    'key': 'FFmpegMetadata',
                    'add_metadata': True,
                }
            ],
            'quiet': False,
            'no_warnings': False,
        }
    elif mode == 'watermark':
        ydl_opts = {
            'ffmpeg_location': BUNDLED_BIN_DIR,
            'format': 'download/download_addr/best[format_id=download]/best[format_note*=watermarked]/best',
            'outtmpl': out_tmpl,
            'writethumbnail': True,
            'merge_output_format': 'mp4',
            'progress_hooks': [yt_dlp_hook],
            'postprocessors': [
                {
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                },
                {
                    'key': 'EmbedThumbnail',
                    'already_have_thumbnail': False,
                },
                {
                    'key': 'FFmpegMetadata',
                    'add_metadata': True,
                }
            ],
            'postprocessor_args': {
                'Merger': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k'],
                'FFmpegVideoConvertor': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k']
            },
            'quiet': False,
            'no_warnings': False,
        }
    else:  # no_watermark (default)
        ydl_opts = {
            'ffmpeg_location': BUNDLED_BIN_DIR,
            'format': 'best[format_id!=download][format_note!=watermarked]/bestvideo[format_id!=download][format_note!=watermarked]+bestaudio/best',
            'outtmpl': out_tmpl,
            'writethumbnail': True,
            'merge_output_format': 'mp4',
            'progress_hooks': [yt_dlp_hook],
            'postprocessors': [
                {
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                },
                {
                    'key': 'EmbedThumbnail',
                    'already_have_thumbnail': False,
                },
                {
                    'key': 'FFmpegMetadata',
                    'add_metadata': True,
                }
            ],
            'postprocessor_args': {
                'Merger': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k'],
                'FFmpegVideoConvertor': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k']
            },
            'quiet': False,
            'no_warnings': False,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            log_msg(f"[Инфо] Начало загрузки TikTok ({mode_name})...")
            info = ydl.extract_info(url, download=True)
            title = info.get('title') or 'TikTok Video'
            author = info.get('uploader') or info.get('channel') or info.get('creator') or 'TikTok'
            
            target_path = ydl.prepare_filename(info)
            base_name, _ = os.path.splitext(target_path)

            if mode == 'audio':
                final_mp3 = base_name + '.mp3'
                saved_file = final_mp3 if os.path.exists(final_mp3) else target_path
                if os.path.exists(saved_file):
                    thumb_url = info.get('thumbnail')
                    embed_mp3_id3(saved_file, title, author, thumb_url)
                    log_msg(f"[УСПЕХ] Аудио сохранено: {saved_file}")
            else:
                final_mp4 = base_name + '.mp4'
                saved_file = final_mp4 if os.path.exists(final_mp4) else target_path
                if os.path.exists(saved_file):
                    finish_video_metadata(saved_file, title, author)
                    log_msg(f"[УСПЕХ] Видео сохранено: {saved_file}")

            # Clean up loose thumbnail files
            for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                thumb_f = base_name + ext
                if os.path.exists(thumb_f):
                    try:
                        os.remove(thumb_f)
                    except Exception:
                        pass

            log_msg("=== ЗАГРУЗКА ЗАВЕРШЕНА ===")
            return True
    except Exception as e:
        log_msg(f"[ОШИБКА] Не удалось скачать TikTok: {e}")
        return False

def download_pinterest(url):
    downloads_path = get_downloads_dir()
    log_msg(f"[Инфо] Папка для загрузки: {downloads_path}")
    log_msg(f"[Инфо] Pinterest: загрузка медиафайла {url}")

    out_tmpl = os.path.join(downloads_path, '%(title)s.%(ext)s')
    ydl_opts = {
        'ffmpeg_location': BUNDLED_BIN_DIR,
        'format': 'bestvideo+bestaudio/best',
        'outtmpl': out_tmpl,
        'writethumbnail': True,
        'merge_output_format': 'mp4',
        'progress_hooks': [yt_dlp_hook],
        'postprocessors': [
            {
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            },
            {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': False,
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            }
        ],
        'postprocessor_args': {
            'Merger': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k'],
            'FFmpegVideoConvertor': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k']
        },
        'quiet': False,
        'no_warnings': False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            log_msg(f"[Инфо] Загрузка видео с Pinterest...")
            info = ydl.extract_info(url, download=True)
            title = info.get('title') or 'Pinterest Media'
            author = info.get('uploader') or 'Pinterest'
            
            target_path = ydl.prepare_filename(info)
            base_name, _ = os.path.splitext(target_path)
            final_mp4 = base_name + '.mp4'
            saved_file = final_mp4 if os.path.exists(final_mp4) else target_path
            if os.path.exists(saved_file):
                finish_video_metadata(saved_file, title, author)
                log_msg(f"[УСПЕХ] Файл сохранен: {saved_file}")

            # Clean up loose thumbnail files
            for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                thumb_f = base_name + ext
                if os.path.exists(thumb_f):
                    try:
                        os.remove(thumb_f)
                    except Exception:
                        pass

            log_msg("=== ЗАГРУЗКА ЗАВЕРШЕНА ===")
            return True
    except Exception as e:
        log_msg(f"[ОШИБКА] Не удалось скачать Pinterest: {e}")
        return False

def parse_height(quality_str):
    if not quality_str:
        return 1080
    q = quality_str.lower().strip()
    if '4k' in q or '2160' in q:
        return 2160
    elif '2k' in q or '1440' in q:
        return 1440
    elif '1080' in q:
        return 1080
    elif '720' in q:
        return 720
    elif '480' in q:
        return 480
    elif '360' in q:
        return 360
    elif '240' in q:
        return 240
    elif '144' in q:
        return 144
    return 1080

def download_youtube(url, quality="1080p", bitrate_mode="Стандартный"):
    downloads_path = get_downloads_dir()
    max_h = parse_height(quality)
    log_msg(f"[Инфо] Папка для загрузки: {downloads_path}")
    log_msg(f"[Инфо] YouTube: качество {quality} (макс. {max_h}p), режим битрейта: {bitrate_mode}")

    if bitrate_mode == "Максимальный":
        format_selector = f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]/best"
        format_sort = [f'res:{max_h}', '+size', '+br', '+vbr', '+abr']
    else:
        format_selector = f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]/best"
        format_sort = [f'res:{max_h}']

    out_tmpl = os.path.join(downloads_path, '%(title)s.%(ext)s')

    ydl_opts = {
        'ffmpeg_location': BUNDLED_BIN_DIR,
        'format': format_selector,
        'format_sort': format_sort,
        'outtmpl': out_tmpl,
        'writethumbnail': True,
        'merge_output_format': 'mp4',
        'progress_hooks': [yt_dlp_hook],
        'postprocessors': [
            {
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            },
            {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': False,
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            }
        ],
        'postprocessor_args': {
            'Merger': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k'],
            'FFmpegVideoConvertor': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k']
        },
        'quiet': False,
        'no_warnings': False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            log_msg(f"[Инфо] Начало загрузки видео ({quality}, {bitrate_mode} битрейт, 320 kbps звук)...")
            info = ydl.extract_info(url, download=True)
            title = info.get('title') or 'Видео'
            author = info.get('uploader') or info.get('channel') or info.get('artist') or 'YouTube'
            
            target_path = ydl.prepare_filename(info)
            base_name, _ = os.path.splitext(target_path)
            final_mp4 = base_name + '.mp4'
            
            saved_file = final_mp4 if os.path.exists(final_mp4) else target_path
            if os.path.exists(saved_file):
                finish_video_metadata(saved_file, title, author)
                log_msg(f"[УСПЕХ] Видео сохранено: {saved_file}")

            # Clean up any loose thumbnail files
            for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                thumb_f = base_name + ext
                if os.path.exists(thumb_f):
                    try:
                        os.remove(thumb_f)
                    except Exception:
                        pass

            log_msg("=== ЗАГРУЗКА ЗАВЕРШЕНА ===")
            return True
    except Exception as e:
        log_msg(f"[ОШИБКА] Не удалось скачать видео: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="OmniGrab Media Downloader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    info_parser = subparsers.add_parser("info")
    info_parser.add_argument("url", help="Media URL")

    dl_parser = subparsers.add_parser("download")
    dl_parser.add_argument("url", help="Media URL")
    dl_parser.add_argument("--quality", default="1080p", help="Target resolution")
    dl_parser.add_argument("--bitrate", default="Стандартный", help="Bitrate mode: 'Стандартный' or 'Максимальный'")
    dl_parser.add_argument("--tiktok-mode", default="no_watermark", help="TikTok mode: 'no_watermark', 'watermark', 'audio'")
    dl_parser.add_argument("--soundcloud-mode", default="audio", help="SoundCloud mode: 'audio' or 'cover'")

    args = parser.parse_args()

    if args.command == "info":
        success = fetch_info(args.url.strip())
        sys.exit(0 if success else 1)
    elif args.command == "download":
        url = args.url.strip()
        clean_url = url.lower()
        if 'soundcloud.com' in clean_url:
            success = download_soundcloud(url, mode=args.soundcloud_mode)
        elif 'tiktok.com' in clean_url or 'douyin.com' in clean_url:
            success = download_tiktok(url, mode=args.tiktok_mode)
        elif 'pinterest.' in clean_url or 'pin.it' in clean_url:
            success = download_pinterest(url)
        else:
            success = download_youtube(url, quality=args.quality, bitrate_mode=args.bitrate)
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
