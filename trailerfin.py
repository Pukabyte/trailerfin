import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import argparse
from dotenv import load_dotenv
import urllib.parse
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

try:
    import schedule
except ImportError:
    schedule = None

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

base_path = os.getenv("SCAN_PATH")
video_filename = os.getenv("VIDEO_FILENAME")
schedule_days = int(os.getenv("SCHEDULE_DAYS", 1))
video_start_time = int(os.getenv("VIDEO_START_TIME", 10))  # Default to 10 seconds if not set
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.imdb.com/',
    'Connection': 'keep-alive',
    'DNT': '1',
    'Upgrade-Insecure-Requests': '1',
}

workers_env = os.getenv("WORKERS")
try:
    default_worker_count = int(workers_env) if workers_env is not None else 4
except ValueError:
    default_worker_count = 4

def tmdb_to_imdb(tmdb_id):
    """Convert TMDB ID to IMDB ID using TMDB API. Try both as movie and TV show."""
    if not tmdb_api_key:
        logging.error("TMDB_API_KEY not set. Please set it in the .env file or as an environment variable.")
        return None
    # Try as movie
    url_movie = f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
    try:
        response = requests.get(url_movie, timeout=10)
        if response.status_code == 200:
            data = response.json()
            imdb_id = data.get("imdb_id")
            if imdb_id and imdb_id.startswith("tt"):
                return imdb_id
        # Try as TV Shows if it fails
        url_tv = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
        response = requests.get(url_tv, timeout=10)
        if response.status_code == 200:
            data = response.json()
            imdb_id = data.get("imdb_id")
            if imdb_id and imdb_id.startswith("tt"):
                return imdb_id
        else:
            logging.error(f"TMDB API error {response.status_code} for TMDB ID {tmdb_id}")
    except Exception as e:
        logging.error(f"Error converting TMDB->IMDB: {e}")
    return None

def get_trailer_video_page_url(imdb_id):
    def find_trailer_in_page(soup):
        trailer_spans = soup.find_all('span', class_='ipc-lockup-overlay__text ipc-lockup-overlay__text--clamp-none')
        logging.debug(f"Found {len(trailer_spans)} spans with video class")
        
        # First pass: look for trailers
        for span in trailer_spans:
            span_text = span.get_text(strip=True)
            logging.debug(f"Checking span text: {span_text}")
            if 'Trailer' in span_text:
                parent_link = span.find_parent('a', href=lambda x: x and '/video/vi' in x)
                if parent_link:
                    video_page_url = f"https://www.imdb.com{parent_link['href']}"
                    logging.debug(f"Found trailer link: {video_page_url}")
                    return video_page_url
        
        # Second pass: look for clips if no trailer found
        for span in trailer_spans:
            span_text = span.get_text(strip=True)
            logging.debug(f"Checking span text: {span_text}")
            if 'Clip' in span_text:
                parent_link = span.find_parent('a', href=lambda x: x and '/video/vi' in x)
                if parent_link:
                    video_page_url = f"https://www.imdb.com{parent_link['href']}"
                    logging.debug(f"Found clip link: {video_page_url}")
                    return video_page_url
        
        return None

    try:
        # First try descending order (newest first) for trailers
        url = f"https://www.imdb.com/title/{imdb_id}/videogallery/?sort=date,desc"
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            video_page_url = find_trailer_in_page(soup)
            if video_page_url:
                return video_page_url

        # If no trailer found in descending order, try ascending order
        url = f"https://www.imdb.com/title/{imdb_id}/videogallery/?sort=date,asc"
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            video_page_url = find_trailer_in_page(soup)
            if video_page_url:
                return video_page_url

            # If still no trailer found, look for first video longer than 30 seconds
            video_links = soup.find_all('a', href=lambda x: x and '/video/vi' in x)
            logging.debug(f"Found {len(video_links)} video links")
            
            for link in video_links:
                # Get the duration from the parent div
                parent_div = link.find_parent('div', class_='video-item')
                if parent_div:
                    duration_text = parent_div.find('span', class_='video-duration')
                    if duration_text:
                        duration = duration_text.get_text(strip=True)
                        logging.debug(f"Found duration: {duration}")
                        # Parse duration (format: "X min Y sec")
                        minutes = 0
                        seconds = 0
                        if 'min' in duration:
                            minutes = int(duration.split('min')[0].strip())
                        if 'sec' in duration:
                            seconds = int(duration.split('sec')[0].strip().split()[-1])
                        total_seconds = minutes * 60 + seconds
                        if total_seconds > 30:
                            video_page_url = f"https://www.imdb.com{link['href']}"
                            return video_page_url

        logging.warning(f"No suitable video found for {imdb_id}")
        return None
    except Exception as e:
        logging.error(f"Error fetching videos for {imdb_id}: {e}")
        return None

def get_direct_video_url_from_page(video_page_url):
    try:
        response = requests.get(video_page_url, headers=headers, timeout=20)
        if response.status_code != 200:
            logging.error(f"Failed to fetch video page: {video_page_url} (status {response.status_code})")
            return None
        soup = BeautifulSoup(response.text, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__', type='application/json')
        if not script_tag:
            logging.error(f"No __NEXT_DATA__ script tag found on page: {video_page_url}")
            return None
        data = json.loads(script_tag.string)
        playback_urls = data['props']['pageProps']['videoPlaybackData']['video']['playbackURLs']
        mp4_urls = [item for item in playback_urls if item.get('videoMimeType') == 'MP4']
        if mp4_urls:
            def quality_key(item):
                if '1080' in item.get('videoDefinition', ''):
                    return 3
                if '720' in item.get('videoDefinition', ''):
                    return 2
                if '480' in item.get('videoDefinition', ''):
                    return 1
                return 0
            best = sorted(mp4_urls, key=quality_key, reverse=True)[0]
            return best['url'] + f'#t={video_start_time}'
        if playback_urls:
            return playback_urls[0]['url'] + f'#t={video_start_time}'
        logging.warning(f"No playback URLs found in JSON on page: {video_page_url}")
        return None
    except Exception as e:
        logging.error(f"Error parsing playback URLs from JSON: {e}")
        return None

def create_or_update_strm_file(folder_path, video_url):
    backdrops_path = os.path.join(folder_path, "backdrops")
    os.makedirs(backdrops_path, exist_ok=True)
    strm_path = os.path.join(backdrops_path, video_filename)
    with open(strm_path, "w") as f:
        f.write(video_url)
    logging.info(f"Updated {strm_path}")

def is_strm_expired(strm_path):
    if not os.path.exists(strm_path):
        return True
    try:
        with open(strm_path, 'r') as f:
            url = f.read().strip()
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        expires_list = query.get('Expires')
        if not expires_list:
            return True
        expires = int(expires_list[0])
        import time
        now = int(time.time())
        return now >= expires
    except Exception as e:
        logging.error(f"Error checking expiration for {strm_path}: {e}")
        return True

def get_expiration_time(url):
    try:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        expires_list = query.get('Expires')
        if not expires_list:
            return None
        return int(expires_list[0])
    except Exception as e:
        logging.error(f"Error parsing expiration time from URL: {e}")
        return None

def save_expiration_times(expiration_times):
    cache_file = Path("trailer_expirations.json")
    try:
        with open(cache_file, 'w') as f:
            json.dump(expiration_times, f)
    except Exception as e:
        logging.error(f"Error saving expiration times: {e}")

def load_expiration_times():
    cache_file = Path("trailer_expirations.json")
    try:
        if cache_file.exists():
            with open(cache_file, 'r') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Error loading expiration times: {e}")
    return {}

def load_ignored_titles():
    """Load the list of ignored titles from a JSON file"""
    ignore_file = Path("ignored_titles.json")
    try:
        if ignore_file.exists():
            with open(ignore_file, 'r') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Error loading ignored titles: {e}")
    return {}

def save_ignored_titles(ignored_titles):
    """Save the list of ignored titles to a JSON file"""
    ignore_file = Path("ignored_titles.json")
    try:
        with open(ignore_file, 'w') as f:
            json.dump(ignored_titles, f)
    except Exception as e:
        logging.error(f"Error saving ignored titles: {e}")

def format_duration(seconds):
    """Format seconds into minutes and seconds"""
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    return f"{minutes}min {remaining_seconds}sec"

def process_imdb_folder(root, imdb_id, expiration_times, ignored_titles):
    try:
        # Check if this title is in the ignore list
        if imdb_id in ignored_titles:
            logging.info(f"Skipping ignored title {imdb_id} in {root}")
            return

        backdrops_path = os.path.join(root, "backdrops")
        strm_path = os.path.join(backdrops_path, video_filename)
        
        # Check if we need to refresh based on expiration time
        current_time = int(time.time())
        expiration_time = expiration_times.get(strm_path)
        
        if expiration_time and current_time < expiration_time:
            time_until_expiry = expiration_time - current_time
            formatted_duration = format_duration(time_until_expiry)
            logging.info(f"Trailer link still valid for {imdb_id} in {root} (expires in {formatted_duration})")
            return
        
        logging.info(f"Refreshing trailer for {imdb_id} in {root}")
        video_page_url = get_trailer_video_page_url(imdb_id)
        if video_page_url:
            video_url = get_direct_video_url_from_page(video_page_url)
            if video_url:
                create_or_update_strm_file(root, video_url)
                # Update expiration time
                new_expiration = get_expiration_time(video_url)
                if new_expiration:
                    expiration_times[strm_path] = new_expiration
                    save_expiration_times(expiration_times)
        else:
            # Add to ignored titles if no trailer found
            ignored_titles[imdb_id] = {
                'path': root,
                'last_checked': int(time.time()),
                'reason': 'No trailer available'
            }
            save_ignored_titles(ignored_titles)
            logging.info(f"Added {imdb_id} to ignored titles list")
    except Exception as e:
        logging.error(f"Worker error for {imdb_id} in {root}: {e}")

def scan_and_refresh_trailers(scan_path=None, worker_count=4):
    path_to_scan = scan_path if scan_path else base_path
    if not os.path.exists(path_to_scan):
        logging.error(f"Provided path does not exist: {path_to_scan}")
        return
    
    # Load existing expiration times and ignored titles
    expiration_times = load_expiration_times()
    ignored_titles = load_ignored_titles()
    
    imdb_folders = []
    for root, dirs, files in os.walk(path_to_scan):
        match = re.search(r'\{imdb-(tt\d+)\}', root)
        if match:
            if not root.rstrip(os.sep).endswith(f'{{imdb-{match.group(1)}}}'): 
                continue
            imdb_id = match.group(1)
            imdb_folders.append((root, imdb_id))
    
    if not imdb_folders:
        logging.info("No IMDb folders found to process.")
        return
    
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_folder = {
            executor.submit(process_imdb_folder, root, imdb_id, expiration_times, ignored_titles): (root, imdb_id) 
            for root, imdb_id in imdb_folders
        }
        for future in as_completed(future_to_folder):
            root, imdb_id = future_to_folder[future]
            try:
                future.result()
            except Exception as exc:
                logging.error(f"Exception in worker for {imdb_id} in {root}: {exc}")

def run_scheduler(scan_path=None, worker_count=4):
    if not schedule:
        logging.error("schedule module not installed. Please install with 'pip install schedule'.")
        return
    def job():
        scan_and_refresh_trailers(scan_path, worker_count)
    job()
    schedule.every(schedule_days).days.do(job)
    logging.info(f"Scheduler started. Running every {schedule_days} day(s).")
    while True:
        schedule.run_pending()
        time.sleep(60)

def check_expiring_links(expiration_times, scan_path=None, worker_count=4, ignored_titles=None):
    """Check for links that are about to expire and refresh them"""
    if ignored_titles is None:
        ignored_titles = load_ignored_titles()
        
    current_time = int(time.time())
    expiring_links = []
    
    # Find links that will expire in the next hour
    for strm_path, expiration_time in expiration_times.items():
        if expiration_time - current_time < 3600:  # Less than 1 hour until expiration
            # Extract IMDb ID from the path
            root = os.path.dirname(os.path.dirname(strm_path))
            match = re.search(r'\{imdb-(tt\d+)\}', root)
            if match:
                imdb_id = match.group(1)
                # Only include if not in ignored titles
                if imdb_id not in ignored_titles:
                    expiring_links.append(strm_path)
    
    if expiring_links:
        logging.info(f"Found {len(expiring_links)} links expiring soon")
        # Extract IMDb IDs from the paths
        imdb_folders = []
        for strm_path in expiring_links:
            root = os.path.dirname(os.path.dirname(strm_path))
            match = re.search(r'\{imdb-(tt\d+)\}', root)
            if match:
                imdb_id = match.group(1)
                imdb_folders.append((root, imdb_id))
        
        if imdb_folders:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_to_folder = {
                    executor.submit(process_imdb_folder, root, imdb_id, expiration_times, ignored_titles): (root, imdb_id) 
                    for root, imdb_id in imdb_folders
                }
                for future in as_completed(future_to_folder):
                    root, imdb_id = future_to_folder[future]
                    try:
                        future.result()
                    except Exception as exc:
                        logging.error(f"Exception in worker for {imdb_id} in {root}: {exc}")

def initialize_expiration_database(scan_path=None):
    """Initialize the expiration database by scanning existing .strm files"""
    path_to_scan = scan_path if scan_path else base_path
    if not os.path.exists(path_to_scan):
        logging.error(f"Provided path does not exist: {path_to_scan}")
        return {}
    
    expiration_times = {}
    strm_files_found = False
    
    # First, try to find existing .strm files
    for root, dirs, files in os.walk(path_to_scan):
        if video_filename in files:
            strm_path = os.path.join(root, video_filename)
            try:
                with open(strm_path, 'r') as f:
                    url = f.read().strip()
                expiration_time = get_expiration_time(url)
                if expiration_time:
                    expiration_times[strm_path] = expiration_time
                    strm_files_found = True
                    logging.info(f"Found existing .strm file: {strm_path}")
            except Exception as e:
                logging.error(f"Error reading .strm file {strm_path}: {e}")
    
    if not strm_files_found:
        logging.info("No existing .strm files found, performing full scan")
        # If no .strm files found, do a full scan
        scan_and_refresh_trailers(scan_path)
        # Reload expiration times after full scan
        expiration_times = load_expiration_times()
    
    return expiration_times

def watch_for_new_media(scan_path=None, worker_count=4):
    """Watch for new media folders and process them"""
    path_to_scan = scan_path if scan_path else base_path
    if not os.path.exists(path_to_scan):
        logging.error(f"Provided path does not exist: {path_to_scan}")
        return set()
    
    # Get current folders
    current_folders = set()
    for root, dirs, files in os.walk(path_to_scan):
        match = re.search(r'\{imdb-(tt\d+)\}', root)
        if match and root.rstrip(os.sep).endswith(f'{{imdb-{match.group(1)}}}'):
            # Verify this is a media folder by checking for video files
            has_video = any(f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv')) for f in files)
            if has_video:
                current_folders.add(root)
                logging.debug(f"Found media folder: {root}")
    
    return current_folders

def run_continuous_monitor(scan_path=None, worker_count=4):
    logging.info("Starting continuous monitor for expiring links")
    
    # Initialize the database
    expiration_times = initialize_expiration_database(scan_path)
    save_expiration_times(expiration_times)
    
    # Load ignored titles
    ignored_titles = load_ignored_titles()
    
    # Get initial set of folders
    last_known_folders = watch_for_new_media(scan_path, worker_count)
    logging.info(f"Initial scan found {len(last_known_folders)} media folders")
    
    while True:
        try:
            # Check for new media
            current_folders = watch_for_new_media(scan_path, worker_count)
            new_folders = current_folders - last_known_folders
            
            if new_folders:
                logging.info(f"Found {len(new_folders)} new media folders")
                for root in new_folders:
                    match = re.search(r'\{imdb-(tt\d+)\}', root)
                    if match:
                        imdb_id = match.group(1)
                        logging.info(f"Processing new media: {root}")
                        process_imdb_folder(root, imdb_id, expiration_times, ignored_titles)
                last_known_folders = current_folders
                save_expiration_times(expiration_times)
            
            # Check for expiring links
            check_expiring_links(expiration_times, scan_path, worker_count, ignored_titles)
            
            # Sleep for 5 minutes before next check
            time.sleep(300)
        except KeyboardInterrupt:
            logging.info("Continuous monitor stopped by user")
            break
        except Exception as e:
            logging.error(f"Error in continuous monitor: {e}")
            time.sleep(60)  # Wait a minute before retrying on error

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan and refresh IMDb trailers.")
    parser.add_argument('--dir', type=str, help='Directory to scan (defaults to /mnt/plex)')
    parser.add_argument('--schedule', action='store_true', help='Run as a weekly scheduled job')
    parser.add_argument('--workers', type=int, default=default_worker_count, help=f'Number of worker threads (default: {default_worker_count})')
    parser.add_argument('--monitor', action='store_true', help='Run in continuous monitoring mode')
    args = parser.parse_args()
    
    if args.monitor:
        run_continuous_monitor(args.dir, args.workers)
    elif args.schedule:
        run_scheduler(args.dir, args.workers)
    else:
        scan_and_refresh_trailers(args.dir, args.workers)
