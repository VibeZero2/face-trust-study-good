"""
Face Viewer Dashboard - Integrated with main app
Renders the dashboard template at /dashboard
"""
import os
import sys
import pandas as pd
import json
from datetime import datetime
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, send_file, session, redirect, url_for, flash, current_app
from functools import wraps
import io
from werkzeug.utils import secure_filename
import zipfile
import tempfile
import threading
import time

# Disable watchdog to fix Render deployment
Observer = None
FileSystemEventHandler = None
WATCHDOG_AVAILABLE = False

# Import from the dashboard.analysis package

def _empty_exclusion_summary():
    return {
        'total_raw': 0,
        'total_cleaned': 0,
        'session_level': {
            'total_sessions': 0,
            'excluded_sessions': 0,
            'exclusion_reasons': {}
        },
        'trial_level': {
            'total_trials': 0,
            'excluded_trials': 0,
            'exclusion_reasons': {}
        }
    }


def _empty_data_summary(mode):
    return {
        'mode': mode,
        'total_rows': 0,
        'real_participants': 0,
        'test_files': 0,
        'real_files': [],
        'test_files_list': []
    }


from .analysis.cleaning import DataCleaner
from .analysis.stats import StatisticalAnalyzer
from .analysis.filters import DataFilter
from .config import DATA_DIR

# Create a blueprint for dashboard routes
dashboard_bp = Blueprint('dashboard', __name__,
                        template_folder='templates',
                        static_folder='static',
                        static_url_path='/dashboard/static')

# Global variables for data management
data_cleaner = None
statistical_analyzer = None
data_filter = None
last_data_refresh = None
data_files_hash = None

DATA_VIEW_MODES = ['PRODUCTION', 'TEST', 'ALL']
dashboard_mode = 'PRODUCTION'

# Dashboard settings - now always shows all data

if WATCHDOG_AVAILABLE:
    class DataFileHandler(FileSystemEventHandler):
        """Watchdog handler for detecting new data files"""
        
        def __init__(self, dashboard_app):
            self.dashboard_app = dashboard_app
            self.last_modified = {}
        
        def on_created(self, event):
            if not event.is_directory and event.src_path.endswith('.csv'):
                filename = Path(event.src_path).name
                print(f"[new] New data file detected: {filename}")
                print(f"   [pin] Full path: {event.src_path}")
                trigger_data_refresh()
        
        def on_modified(self, event):
            if not event.is_directory and event.src_path.endswith('.csv'):
                # Avoid duplicate triggers for the same file
                current_time = time.time()
                if (event.src_path not in self.last_modified or 
                    current_time - self.last_modified[event.src_path] > 1):
                    filename = Path(event.src_path).name
                    print(f"[note] Data file modified: {filename}")
                    print(f"   [pin] Full path: {event.src_path}")
                    self.last_modified[event.src_path] = current_time
                    trigger_data_refresh()
else:
    class DataFileHandler:
        """Fallback handler when watchdog is not available"""
        
        def __init__(self, dashboard_app):
            self.dashboard_app = dashboard_app
            self.last_modified = {}
        
        def on_created(self, event):
            pass  # No file watching without watchdog
        
        def on_modified(self, event):
            pass  # No file watching without watchdog

def start_file_watcher():
    """Start watching the data directory for new files."""
    if not WATCHDOG_AVAILABLE:
        print("[watcher] Watchdog not available - file monitoring disabled")
        return None

    try:
        data_dir = DATA_DIR
        if data_dir.exists():
            event_handler = DataFileHandler(app)
            observer = Observer()
            observer.schedule(event_handler, str(data_dir), recursive=False)
            observer.start()
            print(f"[watcher] Started watching data directory: {data_dir}")
            return observer
        else:
            print(f"[watcher] Data directory not found: {data_dir}")
            return None
    except Exception as e:
        print(f"[watcher] Error starting file watcher: {e}")
        return None

def is_data_available():
    """Check if data is available and initialized."""
    return data_cleaner is not None and data_filter is not None and statistical_analyzer is not None

def initialize_data():
    """Initialize data processing components based on the selected mode."""
    global data_cleaner, statistical_analyzer, data_filter, last_data_refresh

    try:
        data_dir = DATA_DIR
        if not data_dir.exists():
            raise FileNotFoundError(f"Data directory not found: {data_dir}")

        csv_files = list(data_dir.glob('*.csv'))
        if not csv_files:
            raise FileNotFoundError(f"No CSV files found in {data_dir}")

        print(f"[init] Found {len(csv_files)} CSV files in {data_dir}. Mode: {dashboard_mode}")
        print(f"[init] Files: {[f.name for f in csv_files[:5]]}{'...' if len(csv_files) > 5 else ''}")

        print("[init] Step 1: Creating DataCleaner...")
        data_cleaner = DataCleaner(str(data_dir), mode=dashboard_mode)
        print("[init] Step 2: Loading data...")
        data_cleaner.load_data()
        print(f"[init] Step 2 done: raw_data shape = {data_cleaner.raw_data.shape if data_cleaner.raw_data is not None else 'None'}")
        print("[init] Step 3: Standardizing data...")
        data_cleaner.standardize_data()
        print(f"[init] Step 3 done: raw_data shape = {data_cleaner.raw_data.shape if data_cleaner.raw_data is not None else 'None'}")
        print("[init] Step 4: Applying exclusion rules...")
        data_cleaner.apply_exclusion_rules()
        print(f"[init] Step 4 done: cleaned_data shape = {data_cleaner.cleaned_data.shape if data_cleaner.cleaned_data is not None else 'None'}")

        if len(data_cleaner.raw_data) > 0:
            print("[init] Step 5: Creating StatisticalAnalyzer + DataFilter...")
            statistical_analyzer = StatisticalAnalyzer(data_cleaner)
            data_filter = DataFilter(data_cleaner)
            print("[init] Step 5 done")
        else:
            print("[init] No data rows — skipping analyzer/filter")
            statistical_analyzer = None
            data_filter = None

        last_data_refresh = datetime.now()
        print(f"[init] SUCCESS — data initialized in {dashboard_mode} mode")
        return True
    except FileNotFoundError as e:
        print(f"No data files found: {e}")
        print("Dashboard will start in empty state - upload data to begin analysis")
        data_cleaner = None
        statistical_analyzer = None
        data_filter = None
        last_data_refresh = datetime.now()
        return True
    except Exception as e:
        print(f"Error initializing data: {e}")
        import traceback
        traceback.print_exc()
        # Start in empty state so dashboard + upload still work
        data_cleaner = None
        statistical_analyzer = None
        data_filter = None
        last_data_refresh = datetime.now()
        return True



def trigger_data_refresh():
    """Trigger a data refresh when new files are detected"""
    global last_data_refresh
    
    try:
        # Triggering data refresh
        
        # Log current files before refresh
        data_dir = DATA_DIR
        if data_dir.exists():
            current_files = list(data_dir.glob("*.csv"))
            print(f"[folder] Current files in {data_dir}: {[f.name for f in current_files]}")
        
        if initialize_data():
            last_data_refresh = datetime.now()
            print("[ok] Data refresh completed")
            
            # Log data after refresh
            if data_cleaner and hasattr(data_cleaner, 'data') and data_cleaner.data is not None:
                print(f"[chart] Total responses loaded: {len(data_cleaner.data)}")
                if len(data_cleaner.data) > 0:
                    participants = data_cleaner.data['pid'].unique() if 'pid' in data_cleaner.data.columns else []
                    print(f"[users] Participants: {list(participants)}")
        else:
            print("[error] Data refresh failed")
    except Exception as e:
        print(f"[error] Error during data refresh: {e}")



def _count_faces_from_responses(responses):
    """Estimate how many faces have responses stored in a session JSON payload."""
    if not responses:
        return 0

    try:
        if isinstance(responses, dict):
            face_ids = set()
            for key, value in responses.items():
                if key:
                    face_ids.add(str(key))
                if isinstance(value, dict):
                    fid = value.get('face_id') or value.get('faceId') or value.get('face')
                    if fid:
                        face_ids.add(str(fid))
            return len(face_ids)

        if isinstance(responses, list):
            face_ids = set()
            for item in responses:
                if isinstance(item, dict):
                    fid = item.get('face_id') or item.get('faceId') or item.get('face')
                    if not fid and isinstance(item.get('responses'), dict):
                        nested = item['responses']
                        fid = nested.get('face_id') if isinstance(nested, dict) else None
                    if fid:
                        face_ids.add(str(fid))
                elif item:
                    face_ids.add(str(item))
            return len(face_ids)
    except Exception:
        return 0

    return 0


def set_dashboard_mode(mode: str):
    global dashboard_mode
    selected = (mode or 'PRODUCTION').upper()
    if selected not in DATA_VIEW_MODES:
        selected = 'PRODUCTION'
    dashboard_mode = selected


@dashboard_bp.route('/set_mode', methods=['POST'])
def set_mode():
    mode = request.form.get('mode', 'PRODUCTION')
    previous_mode = dashboard_mode
    set_dashboard_mode(mode)
    initialize_data()
    message = f"Data mode switched to {dashboard_mode}"
    if dashboard_mode != previous_mode:
        flash(message, 'info')
    else:
        flash(f"Data mode remains {dashboard_mode}", 'info')
    return redirect(url_for('dashboard.dashboard'))




# Start file watcher in a separate thread
file_observer = None
# Enable file watcher in both debug and production modes for real-time updates
file_observer = start_file_watcher()

# Initialize data on startup - force initialization
print("[refresh] Forcing dashboard data initialization...")
if initialize_data():
    print("[ok] Dashboard data initialized successfully")
else:
    print("[error] Dashboard data initialization failed")

# Simple file-based user authentication
import json
import os

def load_users():
    """Load users from JSON file."""
    users_file = 'data/users.json'
    if os.path.exists(users_file):
        try:
            with open(users_file, 'r') as f:
                return json.load(f)
        except:
            pass
    # Default admin user
    default_users = {
        'admin': {'password': 'admin123', 'role': 'admin', 'email': 'admin@example.com'}
    }
    save_users(default_users)
    return default_users

def save_users(users):
    """Save users to JSON file."""
    users_file = 'data/users.json'
    os.makedirs('data', exist_ok=True)
    with open(users_file, 'w') as f:
        json.dump(users, f, indent=2)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'authenticated' not in session:
            flash('Please log in to access this page', 'warning')
            return redirect(url_for('dashboard.login'))
        return f(*args, **kwargs)
    return decorated_function

@dashboard_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        users = load_users()
        
        if username in users and users[username]['password'] == password:
            session['authenticated'] = True
            session['username'] = username
            session['role'] = users[username].get('role', 'user')
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard.dashboard'))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')

@dashboard_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        users = load_users()
        
        # Validation
        if not username or not email or not password:
            flash('All fields are required', 'error')
            return render_template('register.html')
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('register.html')
        
        if username in users:
            flash('Username already exists', 'error')
            return render_template('register.html')
        
        # Add new user
        users[username] = {
            'password': password,
            'email': email,
            'role': 'user'
        }
        save_users(users)
        
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('dashboard.login'))
    
    return render_template('register.html')

@dashboard_bp.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('dashboard.login'))

@dashboard_bp.route('/')
@login_required
def dashboard():
    """Main dashboard page."""
    global data_cleaner, statistical_analyzer, data_filter
    
    if not is_data_available():
        initialize_data()
    
    try:
        # Starting dashboard function
        # Fail-safe: if anything goes wrong below, show empty overview instead of error card
        # Get overview statistics
        if not is_data_available():
            # No data available
            flash('No data available. Please upload data files or check data directory.', 'warning')
            return render_template('dashboard.html',
                             exclusion_summary={},
                             descriptive_stats={},
                             dashboard_stats={},
                             data_summary={'mode': dashboard_mode, 'total_rows': 0, 'real_participants': 0},
                             available_filters={},
                             data_files=[],
                             available_modes=DATA_VIEW_MODES,
                             current_mode=dashboard_mode)
        
        # Data is available, proceeding with calculations
        
        try:
            exclusion_summary = data_cleaner.get_exclusion_summary()
        except Exception as e:
            print(f"[error] Error getting exclusion summary: {e}")
            exclusion_summary = {}
        
        try:
            descriptive_stats = statistical_analyzer.get_descriptive_stats() if statistical_analyzer is not None else {}
        except Exception as e:
            print(f"[error] Error getting descriptive stats: {e}")
            descriptive_stats = {}
        
        try:
            data_summary = data_cleaner.get_data_summary()
        except Exception as e:
            print(f"[error] Error getting data summary: {e}")
            data_summary = {'mode': 'ERROR'}
        
        # Set mode to production only
        data_summary['mode'] = dashboard_mode
        
        # Calculate additional stats for the dashboard
        cleaned_data = data_cleaner.get_cleaned_data()
        # Early empty-state guard: render safe page when no data
        if cleaned_data is None or len(cleaned_data) == 0:
            return render_template('dashboard.html',
                             exclusion_summary=exclusion_summary,
                             descriptive_stats={},
                             dashboard_stats={
                                 'total_participants': 0,
                                 'total_responses': 0,
                                 'avg_trust_rating': 0,
                                 'std_trust_rating': 0,
                                 'included_participants': 0,
                                 'cleaned_trials': 0,
                                 'raw_responses': 0,
                                 'excluded_responses': 0
                             },
                             data_summary={'mode': dashboard_mode, 'total_rows': 0, 'real_participants': 0},
                             available_filters={},
                             data_files=[],
                             available_modes=DATA_VIEW_MODES,
                             current_mode=dashboard_mode)
        
        # Filter out test data (prolific_pid contains "TEST")
        if cleaned_data is not None and len(cleaned_data) > 0:
            if 'prolific_pid' in cleaned_data.columns and dashboard_mode == 'PRODUCTION':
                cleaned_data = cleaned_data[~cleaned_data['prolific_pid'].str.contains('TEST', na=False)]
        
        if cleaned_data is not None and len(cleaned_data) > 0 and 'include_in_primary' in cleaned_data.columns:
            included_data = cleaned_data[cleaned_data['include_in_primary']]
        else:
            included_data = cleaned_data
        # Normalize types for reliable grouping/counting
        if included_data is not None and len(included_data) > 0:
            try:
                included_data = included_data.copy()
                included_data['pid'] = included_data['pid'].astype(str)
                if 'face_id' in included_data.columns:
                    included_data['face_id'] = included_data['face_id'].astype(str)
            except Exception:
                pass
        
        # Check for active sessions first - only show data if there are active sessions
        sessions_dir = Path("data/sessions")
        if not sessions_dir.exists():
            sessions_dir = Path("../data/sessions")
        
        active_sessions_exist = False
        incomplete_participants = set()
        session_responses_count = 0
        
        if sessions_dir.exists():
            import json
            # Look for active session files (not backup files)
            session_files = [f for f in sessions_dir.glob("*.json") if not f.name.endswith('_backup.json')]
            
            for session_file in session_files:
                try:
                    with open(session_file, 'r') as f:
                        session_info = json.load(f)
                    participant_id = session_info.get('participant_id', 'Unknown')
                    session_complete = session_info.get('session_complete', False)
                    
                    # Only count non-test sessions
                    is_test_session = (
                        'test' in participant_id.lower() or
                        participant_id.startswith('P008') or
                        participant_id.startswith('P0') and participant_id != '200'
                    )
                    
                    if not is_test_session:
                        active_sessions_exist = True
                        if not session_complete:
                            incomplete_participants.add(participant_id)
                            # Count responses from incomplete sessions
                            responses = session_info.get('responses', [])
                            session_responses_count += len(responses)
                except Exception as e:
                    print(f"Error reading session file {session_file}: {e}")
        
        # Always filter for complete participants only - ignore session files completely
        # Only use CSV data and only count participants with complete faces (10 responses per face_id)
        if len(included_data) > 0:
            print(f"[search] DEBUG: CSV data loaded, shape: {included_data.shape}")
            print(f"[search] DEBUG: Columns: {list(included_data.columns)}")
            if len(included_data) > 0:
                print(f"[search] DEBUG: First 5 rows:")
                print(included_data.head())
                if 'question' in included_data.columns:
                    try:
                        uq = included_data['question'].dropna().astype(str).unique().tolist()
                        print(f"[search] DEBUG: Unique questions: {sorted(uq, key=lambda x: str(x))}")
                    except Exception as e:
                        print(f"[search] DEBUG: Unique questions print failed: {e}")
                if 'face_id' in included_data.columns:
                    try:
                        uf = included_data['face_id'].dropna().astype(str).unique().tolist()
                        print(f"[search] DEBUG: Unique face_ids: {sorted(uf, key=lambda x: str(x))}")
                    except Exception as e:
                        print(f"[search] DEBUG: Unique face_ids print failed: {e}")
            
            complete_participants = []
            complete_responses = 0
            
            for pid in included_data['pid'].unique():
                pid_data = included_data[included_data['pid'] == pid]
                
                # Check if this participant has at least one complete face (10 responses per face_id)
                has_complete_face = False
                complete_face_count = 0
                
                # Check if we have long format data (question/response columns)
                if 'question' in pid_data.columns and 'response' in pid_data.columns:
                    # Long format: count responses per face_id
                    # Convert face_id to string to avoid type comparison issues
                    pid_data_copy = pid_data.copy()
                    pid_data_copy['face_id'] = pid_data_copy['face_id'].astype(str)
                    face_counts = pid_data_copy.groupby('face_id').size()
                    
                    # A complete face should have 10 responses
                    for face_id, count in face_counts.items():
                        if count >= 10:
                            has_complete_face = True
                            complete_face_count += 1
                            complete_responses += count  # Count actual responses
                
                # Only count participants with at least one complete face
                if has_complete_face:
                    complete_participants.append(str(pid))
            
            completed_participants = complete_participants
            all_participants = set(complete_participants)
            total_responses = complete_responses
        else:
            completed_participants = []
            all_participants = set()
            total_responses = 0
        
        # Calculate participant and response counts
        
        completed_count = len(all_participants)
        data_summary['complete_participants'] = completed_count
        data_summary.setdefault('real_participants', completed_count)
        data_summary['total_responses'] = total_responses
            
        
        # Dashboard statistics - only use complete participants with complete faces
        try:
            # Prefer computing trust stats directly from long-format if present
            trust_mean = None
            trust_std = None
            if len(included_data) > 0 and ('question' in included_data.columns or 'question_type' in included_data.columns):
                qcol = 'question' if 'question' in included_data.columns else 'question_type'
                ts = pd.to_numeric(included_data[included_data[qcol] == 'trust_rating']['response'], errors='coerce').dropna()
                if len(ts) > 0:
                    trust_mean = ts.mean()
                    trust_std = ts.std()
                    if pd.isna(trust_std):
                        trust_std = 0.0

            # Fallback: compute from complete faces data
            if trust_mean is None:
                complete_data = pd.DataFrame()
                if len(included_data) > 0:
                    required_columns = ['trust_rating', 'emotion_rating', 'masc_choice', 'fem_choice']
                    for pid in included_data['pid'].unique():
                        pid_data = included_data[included_data['pid'] == pid]
                        if 'face_id' in pid_data.columns and 'trust_rating' in pid_data.columns:
                            for _, row in pid_data.iterrows():
                                if all(pd.notna(row[col]) and str(row[col]).strip() != '' for col in required_columns):
                                    complete_data = pd.concat([complete_data, pd.DataFrame([row])], ignore_index=True)
                if len(complete_data) > 0 and 'trust_rating' in complete_data.columns:
                    trust_data = pd.to_numeric(complete_data['trust_rating'], errors='coerce').dropna()
                    trust_mean = trust_data.mean() if len(trust_data) > 0 else 0
                    trust_std = trust_data.std() if len(trust_data) > 1 else 0.0
                else:
                    trust_mean = 0
                    trust_std = 0
            
            dashboard_stats = {
                'total_participants': 0,  # Placeholder; updated after metadata aggregation
                'total_responses': total_responses,  # Responses from all visible sessions
                'avg_trust_rating': trust_mean,  # From completed data only
                'std_trust_rating': trust_std,  # From completed data only
                'included_participants': len(all_participants),  # Fully completed participants
                'cleaned_trials': len(included_data) if len(included_data) > 0 else 0,  # Completed trials only
                'raw_responses': exclusion_summary['total_raw'],
                'excluded_responses': exclusion_summary['total_raw'] - len(included_data) if len(included_data) > 0 else exclusion_summary['total_raw']
            }
        except Exception as e:
            print(f"[error] Error calculating dashboard stats: {e}")
            # Fallback stats to prevent crashes
            dashboard_stats = {
                'total_participants': 0,
                'total_responses': 0,
                'avg_trust_rating': 0,
                'std_trust_rating': 0,
                'included_participants': 0,
                'cleaned_trials': 0,
                'raw_responses': 0,
                'excluded_responses': 0
            }
        
        # Get available filters
        available_filters = data_filter.get_available_filters()
        
        # ================================================================================================
        # FILE LIST SECTION: Session data is ONLY for monitoring display - NEVER affects statistics
        # ================================================================================================
        data_files = []
        session_data = []

        visible_participants = set()
        metadata_entries = []

        def _normalize_pid(value, fallback_name=None):
            if value:
                cleaned = str(value).strip()
                if cleaned and cleaned.upper() not in {'UNKNOWN', 'UNKNOWN_PID', 'NAN'}:
                    return cleaned
            if fallback_name:
                try:
                    stem = Path(fallback_name).stem
                    parts = stem.split('_')
                    if parts:
                        candidate = parts[0]
                        if candidate and candidate.upper() not in {'UNKNOWN', 'UNKNOWN_PID', 'NAN'}:
                            return candidate
                except Exception:
                    pass
            return None

        def _matches_mode(is_test: bool) -> bool:
            mode = (dashboard_mode or 'PRODUCTION').upper()
            if mode == 'PRODUCTION':
                return not is_test
            if mode == 'TEST':
                return is_test
            return True

        file_metadata = getattr(data_cleaner, 'file_metadata', [])
        for meta in file_metadata:
            if not _matches_mode(meta.get('is_test', False)):
                continue

            pid_candidate = _normalize_pid(meta.get('pid'), meta.get('name')) or ''
            participant_id_display = pid_candidate.lower() if pid_candidate else ''
            if participant_id_display:
                visible_participants.add(participant_id_display)
            responses = int(meta.get('row_count', 0) or 0)
            total_faces = meta.get('total_faces') or data_cleaner.expected_total_faces
            completed_faces = meta.get('completed_faces', 0)
            progress_percent = meta.get('progress_percent', 0.0)
            status = 'Complete' if meta.get('complete') else f"Incomplete ({progress_percent:.1f}%)"

            first_ts = meta.get('first_timestamp')
            if hasattr(first_ts, 'strftime'):
                modified_display = first_ts.strftime('%Y-%m-%d %H:%M:%S')
            else:
                modified_display = meta.get('modified_display', '')

            data_files.append({
                'name': meta.get('name'),
                'size': f"{completed_faces}/{total_faces} faces",
                'modified': modified_display,
                'type': 'Test' if meta.get('is_test') else 'Production',
                'status': status,
                'participant_id': participant_id_display or meta.get('pid'),
                'normalized_id': participant_id_display or pid_candidate,
            })
            metadata_entries.append({
                'pid': participant_id_display or meta.get('pid'),
                'row_count': responses,
                'completed_faces': completed_faces,
                'total_faces': total_faces,
                'complete': bool(meta.get('complete')),
            })

        sessions_dir = Path('data/sessions')
        if not sessions_dir.exists():
            sessions_dir = Path('../data/sessions')
        if sessions_dir.exists():
            import json
            session_files = list(sessions_dir.glob('*_session.json'))
            for session_file in session_files:
                try:
                    with open(session_file, 'r') as f:
                        session_info = json.load(f)

                    participant_id = session_info.get('participant_id', 'Unknown')
                    session_complete = session_info.get('session_complete', False)

                    is_test_session = (
                        'test' in participant_id.lower() or
                        participant_id.startswith('P008') or
                        (participant_id.startswith('P0') and participant_id != '200')
                    )

                    if session_complete or not _matches_mode(is_test_session):
                        continue

                    face_order = session_info.get('face_order', [])
                    total_faces = len(face_order) if face_order else data_cleaner.expected_total_faces

                    session_info_data = session_info.get('session_data', {})
                    responses = session_info.get('responses', session_info_data.get('responses', {}))

                    normalized_session_pid = _normalize_pid(participant_id) or participant_id
                    if normalized_session_pid:
                        visible_participants.add(normalized_session_pid.lower())

                    completed_faces_count = 0
                    if data_cleaner and hasattr(data_cleaner, 'cleaned_data') and not data_cleaner.cleaned_data.empty:
                        participant_csv_data = data_cleaner.cleaned_data[data_cleaner.cleaned_data['pid'] == participant_id]
                        if not participant_csv_data.empty and 'face_id' in participant_csv_data.columns:
                            pcopy = participant_csv_data.copy()
                            pcopy['face_id'] = pcopy['face_id'].astype(str)
                            counts = pcopy.groupby('face_id').size()
                            completed_faces_count = int((counts >= 10).sum())

                    if completed_faces_count == 0:
                        completed_faces_count = _count_faces_from_responses(responses)

                    completed_faces = completed_faces_count
                    progress_percent = (completed_faces / total_faces * 100) if total_faces > 0 else 0

                    session_data.append({
                        'name': f"{participant_id} (Session)",
                        'size': f"{completed_faces}/{total_faces} faces",
                        'modified': session_info.get('timestamp', 'Unknown'),
                        'type': 'Test' if is_test_session else 'Production',
                        'status': f'Incomplete ({progress_percent:.1f}%)',
                        'participant_id': participant_id,
                        'normalized_id': (normalized_session_pid or participant_id).lower(),
                    })
                except Exception as e:
                    print(f"Error reading session file {session_file}: {e}")

        # Update overview metrics from collected metadata
        normalized_visible = {pid for pid in visible_participants if pid}
        unique_csv_ids = set()
        completed_csv_ids = set()
        total_rows = 0
        if metadata_entries:
            for entry in metadata_entries:
                pid_value = entry.get('pid')
                if not pid_value:
                    continue
                normalized_pid = str(pid_value).strip()
                if not normalized_pid:
                    continue
                unique_csv_ids.add(normalized_pid)
                if entry.get('complete'):
                    completed_csv_ids.add(normalized_pid)
                try:
                    total_rows += int(entry.get('row_count', 0) or 0)
                except Exception:
                    pass
                normalized_visible.add(normalized_pid.lower())
            data_summary['total_rows'] = total_rows
            data_summary['total_responses'] = total_rows
            dashboard_stats['total_responses'] = total_rows
            data_summary['real_participants'] = len(unique_csv_ids)
            data_summary['complete_participants'] = len(completed_csv_ids)
            data_summary['completion_rate'] = round((len(completed_csv_ids) / len(unique_csv_ids) * 100), 1) if unique_csv_ids else 0.0
        else:
            data_summary.setdefault('total_rows', len(included_data) if included_data is not None else 0)
            if 'complete_participants' in data_summary:
                total_visible_candidates = data_summary.get('real_participants') or data_summary['complete_participants']
                if total_visible_candidates:
                    data_summary['completion_rate'] = round((data_summary['complete_participants'] / total_visible_candidates) * 100, 1)
                else:
                    data_summary['completion_rate'] = 0.0
        if session_data:
            data_summary['active_sessions'] = len(session_data)
            data_summary['session_responses'] = session_responses_count
        total_visible = len(normalized_visible)
        data_summary['total_participants'] = total_visible
        dashboard_stats['total_participants'] = total_visible

        combined = []
        entries_by_pid = {}

        def _entry_key(entry):
            return entry.get('normalized_id') or (entry.get('participant_id') or entry.get('name'))

        for entry in data_files:
            key = _entry_key(entry)
            entries_by_pid.setdefault(key, {})['csv'] = entry

        for entry in session_data:
            key = _entry_key(entry)
            entries_by_pid.setdefault(key, {})['session'] = entry

        for parts in entries_by_pid.values():
            if 'session' in parts:
                combined.append(parts['session'])
            elif 'csv' in parts:
                combined.append(parts['csv'])

        all_files = combined

        return render_template('dashboard.html',
                         exclusion_summary=exclusion_summary,
                         descriptive_stats=descriptive_stats,
                         dashboard_stats=dashboard_stats,
                         data_summary=data_summary,
                         available_filters=available_filters,
                         data_files=all_files,
                         available_modes=DATA_VIEW_MODES,
                         current_mode=dashboard_mode)
    except Exception as e:
        # Last-resort fallback: render empty overview so the app stays usable
        print(f"DASHBOARD FAIL-SAFE triggered: {e}")
        try:
            return render_template('dashboard.html',
                             exclusion_summary={},
                             descriptive_stats={},
                             dashboard_stats={
                                 'total_participants': 0,
                                 'total_responses': 0,
                                 'avg_trust_rating': 0,
                                 'std_trust_rating': 0,
                                 'included_participants': 0,
                                 'cleaned_trials': 0,
                                 'raw_responses': 0,
                                 'excluded_responses': 0
                             },
                             data_summary={'mode': dashboard_mode, 'total_rows': 0, 'real_participants': 0},
                             available_filters={},
                             data_files=[],
                             available_modes=DATA_VIEW_MODES,
                             current_mode=dashboard_mode)
        except Exception as inner:
            flash(f'Error loading dashboard: {str(e)}', 'error')
            return render_template('error.html', message=str(e))

@dashboard_bp.route('/api/overview')
@dashboard_bp.route('/dashboard/api/overview')
@login_required
def api_overview():
    """API endpoint for overview statistics."""
    global data_cleaner, statistical_analyzer, data_filter, dashboard_mode
    
    try:
        # Check if components are initialized
        if data_cleaner is None or statistical_analyzer is None:
            if not initialize_data():
                empty_payload = {
                    'status': 'empty',
                    'exclusion_summary': _empty_exclusion_summary(),
                    'descriptive_stats': {},
                    'data_summary': _empty_data_summary(dashboard_mode),
                    'total_participants': 0,
                    'total_responses': 0,
                    'trust_mean': None,
                    'trust_std': None,
                    'timestamp': datetime.now().isoformat()
                }
                return jsonify(empty_payload)
        
        if data_cleaner is None or statistical_analyzer is None:
            empty_payload = {
                'status': 'empty',
                'exclusion_summary': _empty_exclusion_summary(),
                'descriptive_stats': {},
                'data_summary': _empty_data_summary(dashboard_mode),
                'total_participants': 0,
                'total_responses': 0,
                'trust_mean': None,
                'trust_std': None,
                'timestamp': datetime.now().isoformat()
            }
            return jsonify(empty_payload)
        
        # Get data with error handling
        try:
            exclusion_summary = data_cleaner.get_exclusion_summary()
        except Exception as e:
            print(f"ERROR: API Overview - Failed to get exclusion summary: {e}")
            exclusion_summary = _empty_exclusion_summary()
        
        try:
            descriptive_stats = statistical_analyzer.get_descriptive_stats()
        except Exception as e:
            print(f"ERROR: API Overview - Failed to get descriptive stats: {e}")
            descriptive_stats = {}
        
        # Convert numpy types to native Python types for JSON serialization
        def convert_numpy_types(obj):
            if hasattr(obj, 'item'):  # numpy scalar
                return obj.item()
            elif isinstance(obj, dict):
                return {k: convert_numpy_types(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_numpy_types(item) for item in obj]
            else:
                return obj
        
        # Compute top-card metrics from cleaned data (supports long or wide formats)
        try:
            cleaned = data_cleaner.get_cleaned_data()
        except Exception as e:
            print(f"ERROR: API Overview - Failed to get cleaned data: {e}")
            cleaned = pd.DataFrame()

        if cleaned is None:
            cleaned = pd.DataFrame()
        if not isinstance(cleaned, pd.DataFrame):
            cleaned = pd.DataFrame(cleaned)

        included = cleaned[cleaned['include_in_primary']] if 'include_in_primary' in cleaned.columns else cleaned
        try:
            included = included.copy()
            if 'pid' in included.columns:
                included['pid'] = included['pid'].astype(str)
        except Exception:
            pass

        has_pid = 'pid' in included.columns
        total_participants = included['pid'].nunique() if has_pid and len(included) > 0 else 0
        # total_responses: prefer long-format trust rows
        if len(included) > 0 and ('question' in included.columns or 'question_type' in included.columns) and 'response' in included.columns:
            qcol = 'question' if 'question' in included.columns else 'question_type'
            trust_only = included[included[qcol] == 'trust_rating']
            total_responses = pd.to_numeric(trust_only['response'], errors='coerce').notna().sum()
            trust_vals = pd.to_numeric(trust_only['response'], errors='coerce').dropna()
        elif len(included) > 0 and 'trust_rating' in included.columns:
            total_responses = pd.to_numeric(included['trust_rating'], errors='coerce').notna().sum()
            trust_vals = pd.to_numeric(included['trust_rating'], errors='coerce').dropna()
        else:
            total_responses = len(included) if len(included) > 0 else 0
            trust_vals = pd.Series(dtype=float)

        trust_mean = float(trust_vals.mean()) if len(trust_vals) > 0 else None
        trust_std = float(trust_vals.std()) if len(trust_vals) > 1 else 0.0 if len(trust_vals) == 1 else None

        try:
            data_summary_converted = convert_numpy_types(data_cleaner.get_data_summary()) if data_cleaner else _empty_data_summary(dashboard_mode)
        except Exception as e:
            print(f"ERROR: API Overview - Failed to get data summary: {e}")
            data_summary_converted = _empty_data_summary(dashboard_mode)

        response_data = {
            'exclusion_summary': convert_numpy_types(exclusion_summary),
            'descriptive_stats': convert_numpy_types(descriptive_stats),
            'data_summary': data_summary_converted,
            'total_participants': total_participants,
            'total_responses': int(total_responses),
            'trust_mean': trust_mean,
            'trust_std': trust_std,
            'timestamp': datetime.now().isoformat(),
            'status': 'success'
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        error_msg = f"API Overview error: {str(e)}"
        print(f"ERROR: {error_msg}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': error_msg, 'status': 'error'}), 500

@dashboard_bp.route('/api/statistical-tests')
@login_required
def api_statistical_tests():
    """API endpoint for statistical test results."""
    global statistical_analyzer
    
    if statistical_analyzer is None:
        if not initialize_data():
            return jsonify({'error': 'Data initialization failed'}), 500
    
    try:
        results = {
            'paired_t_test': statistical_analyzer.paired_t_test_half_vs_full(),
            'repeated_measures_anova': statistical_analyzer.repeated_measures_anova(),
            'inter_rater_reliability': statistical_analyzer.inter_rater_reliability(),
            'split_half_reliability': statistical_analyzer.split_half_reliability()
        }
        
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@dashboard_bp.route('/api/image-summary')
@login_required
def api_image_summary():
    """API endpoint for image-level summary statistics."""
    try:
        image_summary = statistical_analyzer.get_image_summary()
        return jsonify(image_summary.to_dict('records'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@dashboard_bp.route('/api/filtered-data', methods=['POST'])
@login_required
def api_filtered_data():
    """API endpoint for filtered data."""
    try:
        filters = request.json
        
        # Apply filters
        filtered_data = data_filter.apply_filters(**filters)
        
        # Get summary
        filter_summary = data_filter.get_filter_summary(filtered_data)
        
        # Return limited data for display (first 1000 rows)
        display_data = filtered_data.head(1000).to_dict('records')
        
        return jsonify({
            'data': display_data,
            'summary': filter_summary,
            'total_rows': len(filtered_data),
            'displayed_rows': len(display_data)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@dashboard_bp.route('/api/available-filters')
@login_required
def api_available_filters():
    """API endpoint for available filter options."""
    try:
        filters = data_filter.get_available_filters()
        presets = data_filter.create_preset_filters()
        
        return jsonify({
            'filters': filters,
            'presets': presets
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@dashboard_bp.route('/export/csv')
@login_required
def export_csv():
    """Export filtered data as CSV."""
    try:
        # Get filters from query parameters
        filters = {}
        if request.args.get('include_excluded') == 'true':
            filters['include_excluded'] = True
        
        if request.args.get('phase_filter'):
            filters['phase_filter'] = request.args.get('phase_filter').split(',')
        
        # Apply filters
        filtered_data = data_filter.apply_filters(**filters)
        
        # Create CSV in memory
        output = io.StringIO()
        filtered_data.to_csv(output, index=False)
        output.seek(0)
        
        # Create response
        response = app.response_class(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename=face_perception_data_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'}
        )
        
        return response
    except Exception as e:
        flash(f'Export error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@dashboard_bp.route('/export/analysis-report')
@login_required
def export_analysis_report():
    """Export comprehensive analysis report."""
    try:
        # Run all analyses
        analysis_results = statistical_analyzer.run_all_analyses()
        
        # Create a temporary file for the report
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(analysis_results, f, indent=2, default=str)
            temp_file = f.name
        
        # Send file
        return send_file(temp_file, 
                        as_attachment=True,
                        download_name=f'analysis_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json',
                        mimetype='application/json')
    except Exception as e:
        flash(f'Export error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@dashboard_bp.route('/participants')
@login_required
def participants():
    """Participants overview page."""
    try:
        # Build a simple participants summary matching the template expectations
        if data_cleaner is None:
            return render_template('participants.html', participants=[])
        cleaned = data_cleaner.get_cleaned_data()
        if cleaned is None or len(cleaned) == 0:
            return render_template('participants.html', participants=[])
        included = cleaned[cleaned['include_in_primary']]
        
        # Handle timestamps properly
        if 'timestamp' in included.columns:
            # Convert timestamp to datetime and handle NaT values
            included['timestamp'] = pd.to_datetime(included['timestamp'], errors='coerce')
            
            # Check if we have trust_rating column (wide format) or question_type/response (long format)
            if 'trust_rating' in included.columns:
                summary_df = included.groupby('pid').agg(
                    submissions=('trust_rating', 'count'),
                    start_time=('timestamp', 'min')
                ).reset_index()
            elif 'question_type' in included.columns and 'response' in included.columns:
                # Long format: count responses per participant
                summary_df = included.groupby('pid').agg(
                    submissions=('response', 'count'),
                    start_time=('timestamp', 'min')
                ).reset_index()
            else:
                # Fallback: count all rows per participant
                summary_df = included.groupby('pid').agg(
                    submissions=('pid', 'count'),
                    start_time=('timestamp', 'min')
                ).reset_index()
            
            summary_df['start_time'] = summary_df['start_time'].apply(
                lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if pd.notna(x) else 'N/A'
            )
        else:
            # Check if we have trust_rating column (wide format) or question_type/response (long format)
            if 'trust_rating' in included.columns:
                summary_df = included.groupby('pid').agg(
                    submissions=('trust_rating', 'count')
                ).reset_index()
            elif 'question_type' in included.columns and 'response' in included.columns:
                # Long format: count responses per participant
                summary_df = included.groupby('pid').agg(
                    submissions=('response', 'count')
                ).reset_index()
            else:
                # Fallback: count all rows per participant
                summary_df = included.groupby('pid').agg(
                    submissions=('pid', 'count')
                ).reset_index()
            summary_df['start_time'] = 'N/A'

        # Sort participants numerically instead of alphabetically
        def extract_numeric_pid(pid):
            """Extract numeric part from participant ID for sorting"""
            import re
            match = re.search(r'(\d+)', str(pid))
            return int(match.group(1)) if match else 0
        
        summary_df['sort_key'] = summary_df['pid'].apply(extract_numeric_pid)
        summary_df = summary_df.sort_values('sort_key').drop('sort_key', axis=1)

        # Render the new participants template
        return render_template('participants.html', participants=summary_df.to_dict('records'))
    except Exception as e:
        flash(f'Error loading participants: {str(e)}', 'error')
        return render_template('error.html', message=str(e))

@dashboard_bp.route('/images')
@login_required
def images():
    """Images analysis page."""
    try:
        if data_cleaner is None or data_cleaner.raw_data is None or data_cleaner.raw_data.empty or statistical_analyzer is None:
            # No data available - show empty state
            return render_template('images.html', images=[])
        
        image_summary = statistical_analyzer.get_image_summary()
        return render_template('images.html', images=image_summary.to_dict('records'))
    except Exception as e:
        flash(f'Error loading images: {str(e)}', 'error')
        return render_template('error.html', message=str(e))

@dashboard_bp.route('/statistics')
@login_required
def statistics():
    """Statistical tests page."""
    try:
        print(f"[chart] STATISTICS ROUTE: statistical_analyzer is None: {statistical_analyzer is None}")
        print(f"[chart] STATISTICS ROUTE: data_cleaner is None: {data_cleaner is None}")
        
        if statistical_analyzer is None:
            print("[error] STATISTICS: statistical_analyzer is None - attempting to initialize data")
            if not initialize_data():
                print("[error] STATISTICS: Data initialization failed")
                return render_template('statistics.html', test_results={})
            print("[ok] STATISTICS: Data initialization successful")
        
        if statistical_analyzer is None:
            print("[error] STATISTICS: statistical_analyzer still None after initialization")
            return render_template('statistics.html', test_results={})
        
        print("[ok] STATISTICS: Running statistical tests...")
        # Run all statistical tests
        test_results = {
            'descriptive_stats': statistical_analyzer.get_descriptive_stats(),
            'paired_t_test': statistical_analyzer.paired_t_test_half_vs_full(),
            'repeated_measures_anova': statistical_analyzer.repeated_measures_anova(),
            'inter_rater_reliability': statistical_analyzer.inter_rater_reliability(),
            'split_half_reliability': statistical_analyzer.split_half_reliability(),
            'all_question_stats': statistical_analyzer.get_all_question_stats(),
            'trust_histogram': statistical_analyzer.get_trust_histogram(),
            'emotion_histogram': statistical_analyzer.get_emotion_histogram(),
            'trust_boxplot': statistical_analyzer.get_boxplot_data('trust_rating'),
            'emotion_boxplot': statistical_analyzer.get_boxplot_data('emotion_rating'),
            'emotion_paired_t_test': statistical_analyzer.emotion_paired_t_test_half_vs_full(),
            'emotion_repeated_measures_anova': statistical_analyzer.emotion_repeated_measures_anova(),
            'choice_preference_analysis': statistical_analyzer.choice_preference_analysis(),
        }
        
        # Debug: Print the structure of test results
        print("[chart] ANOVA Result structure:")
        if test_results['repeated_measures_anova']:
            print(f"Type: {type(test_results['repeated_measures_anova'])}")
            if isinstance(test_results['repeated_measures_anova'], dict):
                print(f"Keys: {test_results['repeated_measures_anova'].keys()}")
            else:
                print(f"Attributes: {dir(test_results['repeated_measures_anova'])}")
        
        # Ensure we have the right structure for the template
        if test_results['repeated_measures_anova'] and hasattr(test_results['repeated_measures_anova'], 'to_dict'):
            test_results['repeated_measures_anova'] = test_results['repeated_measures_anova'].to_dict()
        
        # Add means if available
        if test_results['repeated_measures_anova'] and 'means' not in test_results['repeated_measures_anova']:
            # Try to get means from descriptive stats
            try:
                desc_stats = statistical_analyzer.get_descriptive_stats()
                if desc_stats:
                    test_results['repeated_measures_anova']['means'] = desc_stats
            except:
                pass
        
        print(f"[ok] STATISTICS: Generated {len(test_results)} test results")
        return render_template('statistics.html', test_results=test_results)
    except Exception as e:
        print(f"[error] STATISTICS ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Error loading statistics: {str(e)}', 'error')
        return render_template('error.html', message=str(e))

@dashboard_bp.route('/admin/upload', methods=['POST'])
def upload_data():
    """Handle file upload for participant data."""
    try:
        files = request.files.getlist('file') if 'file' in request.files else []
        files = [f for f in files if f and f.filename]
        if not files:
            flash('No files selected', 'error')
            return redirect(url_for('dashboard.dashboard'))

        import pandas as pd
        import io

        is_test = request.form.get('is_test') == 'on'
        prefix = 'TEST_' if is_test else ''
        base_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        saved_files = []
        errors = []

        for index, file in enumerate(files, start=1):
            original_name = file.filename
            if not original_name.lower().endswith('.csv'):
                errors.append(f"{original_name} (not a CSV file)")
                continue
            try:
                csv_content = file.read().decode('utf-8')
                df = pd.read_csv(io.StringIO(csv_content))
            except Exception as read_error:
                errors.append(f"{original_name} ({read_error})")
                continue

            required_columns = ['pid', 'face_id', 'version', 'question', 'response']
            if not all(col in df.columns for col in required_columns):
                errors.append(f"{original_name} (missing required columns)")
                continue

            participant_id = str(df['pid'].iloc[0]) if len(df) > 0 else 'uploaded'
            safe_id = secure_filename(participant_id) or 'participant'
            timestamp = f"{base_timestamp}_{index:02d}"
            filename = f"{prefix}{safe_id}_{timestamp}.csv"
            filepath = DATA_DIR / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)

            df.to_csv(filepath, index=False)
            saved_files.append((filename, len(df)))

        if saved_files:
            initialize_data()
            total_rows = sum(count for _, count in saved_files)
            mode_note = ' (marked as test data)' if is_test else ''
            if len(saved_files) == 1:
                filename, row_count = saved_files[0]
                flash(f"Successfully uploaded {filename} with {row_count} rows{mode_note}", 'success')
            else:
                filenames = ', '.join(name for name, _ in saved_files)
                flash(f"Uploaded {len(saved_files)} files{mode_note}: {filenames}. Total rows: {total_rows}", 'success')

        if errors:
            message = 'Some files were skipped: ' + '; '.join(errors)
            flash(message, 'warning' if saved_files else 'error')
    except Exception as e:
        flash(f'Error uploading file: {str(e)}', 'error')

    return redirect(url_for('dashboard.dashboard'))


@dashboard_bp.route('/admin/generate-test-data', methods=['POST'])
def generate_test_data():
    """Generate test data."""
    try:
        participants = int(request.form.get('participants', 5))
        responses = int(request.form.get('responses', 10))
        
        # Limit to reasonable numbers
        participants = min(max(participants, 1), 50)
        responses = min(max(responses, 1), 100)
        
        # Run the test data generator
        import subprocess
        import sys
        
        # Modify the generator to use the specified parameters
        result = subprocess.run([sys.executable, 'generate_test_data.py'], 
                              capture_output=True, text=True, cwd='.')
        
        if result.returncode == 0:
            flash(f'Successfully generated test data for {participants} participants', 'success')
        else:
            flash(f'Error generating test data: {result.stderr}', 'error')
            
    except Exception as e:
        flash(f'Error generating test data: {str(e)}', 'error')
    
    return redirect(url_for('dashboard.dashboard'))

@dashboard_bp.route('/admin/generate-random-tests', methods=['POST'])
def generate_random_tests():
    """Generate long-format random test files."""
    try:
        import subprocess
        import sys

        count = int(request.form.get('count', 10))
        count = min(max(count, 1), 200)
        seed = request.form.get('seed')

        command = [sys.executable, 'generate_random_test_files.py', '--count', str(count)]
        if seed:
            command.extend(['--seed', str(seed)])

        result = subprocess.run(command, capture_output=True, text=True, cwd='.')

        if result.returncode == 0:
            initialize_data()
            flash(f'Generated {count} test participants (files saved with TEST_ prefix).', 'success')
        else:
            flash(f'Error generating test files: {result.stderr}', 'error')
    except Exception as e:
        flash(f'Error generating test files: {str(e)}', 'error')

    return redirect(url_for('dashboard.dashboard'))

@dashboard_bp.route('/exclusions')
@login_required
def exclusions():
    """Data exclusions page."""
    global data_cleaner, data_filter, statistical_analyzer
    try:
        if data_cleaner is None:
            if not initialize_data():
                flash('No data available to display exclusions. Upload data first.', 'warning')
                return redirect(url_for('dashboard.dashboard'))

        # Get exclusion summary
        exclusion_summary = data_cleaner.get_exclusion_summary() if data_cleaner else _empty_exclusion_summary()
        
        cleaned_data = data_cleaner.get_cleaned_data() if data_cleaner else pd.DataFrame()
        if cleaned_data is None or cleaned_data.empty or "pid" not in cleaned_data.columns:
            return render_template('exclusions.html', exclusion_summary=exclusion_summary, session_details=[], trial_details=[])
        
        # Session-level details
        session_details = []
        for pid in cleaned_data['pid'].unique():
            participant_data = cleaned_data[cleaned_data['pid'] == pid]
            
            # Handle empty session data
            if len(participant_data) == 0:
                session_details.append({
                    'pid': pid,
                    'total_trials': 0,
                    'included': False,
                    'exclusion_reasons': ['no_data']
                })
                continue
            
            # Get inclusion status safely
            included = participant_data['include_in_primary'].iloc[0] if len(participant_data) > 0 else False
            
            expected_trials = max(1, data_cleaner._estimate_expected_trials(participant_data) if data_cleaner else len(participant_data))
            actual_trials = len(participant_data)
            completion_pct = (actual_trials / expected_trials * 100) if expected_trials else 0

            # Determine exclusion reasons
            exclusion_reasons = []
            if not included:
                if actual_trials < 0.8 * expected_trials:
                    exclusion_reasons.append('low_completion')
                if 'excl_failed_attention' in participant_data.columns and participant_data['excl_failed_attention'].any():
                    exclusion_reasons.append('attention_failed')
                if 'excl_device_violation' in participant_data.columns and participant_data['excl_device_violation'].any():
                    exclusion_reasons.append('device_violation')

            session_details.append({
                'pid': pid,
                'total_trials': actual_trials,
                'expected_trials': expected_trials,
                'completion_rate': completion_pct,
                'included': included,
                'exclusion_reasons': exclusion_reasons
            })
        
        # Trial-level details (sample of excluded trials)
        trial_details = []
        excluded_trials = cleaned_data[~cleaned_data['include_in_primary']]
        if len(excluded_trials) > 0:
            # Show first 50 excluded trials
            sample_trials = excluded_trials.head(50)
            
            # Define columns to include, checking if they exist
            columns_to_include = ['pid', 'include_in_primary']
            optional_columns = ['face_id', 'version', 'trust_rating', 'reaction_time', 'excl_fast_rt', 'excl_slow_rt']
            
            for col in optional_columns:
                if col in sample_trials.columns:
                    columns_to_include.append(col)
            
            trial_details = sample_trials[columns_to_include].to_dict('records')
        
        return render_template('exclusions.html', 
                             exclusion_summary=exclusion_summary,
                             session_details=session_details,
                             trial_details=trial_details)
    except Exception as e:
        flash(f'Error loading exclusions: {str(e)}', 'error')
        return render_template('error.html', message=str(e))

@dashboard_bp.route('/participant/<pid>')
@login_required
def participant_detail(pid):
    """Show detailed view of a specific participant's session."""
    try:
        cleaned_data = data_cleaner.get_cleaned_data()
        participant_data = cleaned_data[cleaned_data['pid'] == pid]
        
        if participant_data.empty:
            flash(f'Participant {pid} not found', 'error')
            return redirect(url_for('dashboard.participants'))
        
        # Calculate session summary
        total_trials = len(participant_data)
        included_trials = participant_data['include_in_primary'].sum()
        excluded_trials = total_trials - included_trials
        # Calculate completion rate based on expected unique combinations
        # Convert face_id to string to avoid type comparison issues
        participant_data_copy = participant_data.copy()
        participant_data_copy['face_id'] = participant_data_copy['face_id'].astype(str)
        unique_combinations = participant_data_copy.groupby(['face_id', 'version']).size().shape[0]
        completion_rate = total_trials / unique_combinations if unique_combinations > 0 else 1.0
        
        # Get trust rating statistics
        trust_stats = {
            'mean': participant_data['trust_rating'].mean(),
            'std': participant_data['trust_rating'].std(),
            'min': participant_data['trust_rating'].min(),
            'max': participant_data['trust_rating'].max(),
            'median': participant_data['trust_rating'].median()
        }
        
        # Get version breakdown
        version_counts = participant_data['version'].value_counts().to_dict()
        
        # Get face breakdown
        face_counts = participant_data['face_id'].value_counts().to_dict()
        
        # Prepare trial data for display
        trial_data = participant_data[['face_id', 'version', 'trust_rating', 'include_in_primary', 'source_file']].copy()
        if 'timestamp' in participant_data.columns:
            trial_data['timestamp'] = participant_data['timestamp']
        
        # Sort by face_id and version for better readability
        trial_data = trial_data.sort_values(['face_id', 'version'])
        
        return render_template('participant_detail.html',
                             pid=pid,
                             participant_data=trial_data,
                             total_trials=total_trials,
                             included_trials=included_trials,
                             excluded_trials=excluded_trials,
                             completion_rate=completion_rate,
                             trust_stats=trust_stats,
                             version_counts=version_counts,
                             face_counts=face_counts,
                             data_summary=data_cleaner.get_data_summary())
    
    except Exception as e:
        flash(f'Error loading participant data: {str(e)}', 'error')
        return redirect(url_for('participants'))

@dashboard_bp.route('/health')
def health():
    """Health check endpoint."""
    try:
        if data_cleaner is None:
            return jsonify({'status': 'initializing'}), 503
        
        # Quick data check
        cleaned_data = data_cleaner.get_cleaned_data()
        
        return jsonify({
            'status': 'healthy',
            'data_rows': len(cleaned_data),
            'participants': cleaned_data['pid'].nunique() if 'pid' in cleaned_data.columns else cleaned_data.get('participant_id', pd.Series()).nunique(),
            'timestamp': datetime.now().isoformat(),
            'last_refresh': last_data_refresh.isoformat() if last_data_refresh else None,
            'live_monitoring': file_observer is not None
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@dashboard_bp.route('/api/refresh-data', methods=['POST'])
@login_required
def api_refresh_data():
    """API endpoint to manually refresh data."""
    try:
        # Manual data refresh requested
        trigger_data_refresh()
        return jsonify({
            'status': 'success',
            'message': 'Data refresh triggered',
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"[error] Manual refresh failed: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@dashboard_bp.route('/api/data-status')
@login_required
def api_data_status():
    """API endpoint to get current data status."""
    try:
        if data_cleaner is None:
            return jsonify({'status': 'no_data'}), 503
        
        cleaned_data = data_cleaner.get_cleaned_data()
        data_summary = data_cleaner.get_data_summary()
        
        # Get list of data files
        data_files = []
        data_dir = DATA_DIR
        if data_dir.exists():
            for file_path in data_dir.glob("*.csv"):
                stat = file_path.stat()
                data_files.append({
                    'name': file_path.name,
                    'size': f"{stat.st_size / 1024:.1f} KB",
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'is_study_data': any(pattern in file_path.name for pattern in ['_2025', 'PROLIFIC_', 'test789', 'participant_'])
                })
        
        return jsonify({
            'status': 'success',
            'data_rows': len(cleaned_data),
            'participants': cleaned_data['pid'].nunique() if 'pid' in cleaned_data.columns else 0,
            'data_summary': data_summary,
            'data_files': data_files,
            'last_refresh': last_data_refresh.isoformat() if last_data_refresh else None,
            'live_monitoring': file_observer is not None,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@dashboard_bp.route('/api/live-updates')
@login_required
def api_live_updates():
    """API endpoint for live data updates (for real-time dashboard updates)."""
    try:
        if data_cleaner is None:
            return jsonify({'status': 'no_data'}), 503
        
        cleaned_data = data_cleaner.get_cleaned_data()
        
        # Get recent data (last 24 hours)
        if 'timestamp' in cleaned_data.columns:
            recent_data = cleaned_data[
                cleaned_data['timestamp'] >= (datetime.now() - pd.Timedelta(hours=24))
            ]
        else:
            recent_data = cleaned_data
        
        return jsonify({
            'status': 'success',
            'total_participants': cleaned_data['pid'].nunique() if 'pid' in cleaned_data.columns else 0,
            'recent_participants': recent_data['pid'].nunique() if 'pid' in recent_data.columns else 0,
            'total_trials': len(cleaned_data),
            'recent_trials': len(recent_data),
            'last_refresh': last_data_refresh.isoformat() if last_data_refresh else None,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@dashboard_bp.route('/reset-participant/<participant_id>', methods=['POST'])
def reset_participant(participant_id):
    """Reset all data for a specific participant"""
    try:
        import os
        import shutil
        from pathlib import Path
        
        # Define paths - now integrated in same repository
        responses_dir = Path("data/responses")
        sessions_dir = Path("data/sessions")
        
        files_removed = 0
        
        # Remove CSV files
        if responses_dir.exists():
            for file_path in responses_dir.glob(f"*{participant_id}*"):
                if file_path.is_file():
                    file_path.unlink()
                    files_removed += 1
        
        # Remove session files
        if sessions_dir.exists():
            for file_path in sessions_dir.glob(f"*{participant_id}*"):
                if file_path.is_file():
                    file_path.unlink()
                    files_removed += 1
        
        # Reinitialize data to refresh the dashboard
        initialize_data()
        
        flash(f'Participant {participant_id} reset successfully. {files_removed} files removed.', 'success')
        return redirect(url_for('dashboard.dashboard'))
        
    except Exception as e:
        flash(f'Error resetting participant {participant_id}: {str(e)}', 'error')
        return redirect(url_for('dashboard.dashboard'))


@dashboard_bp.route('/debug/sessions', methods=['GET'])
def debug_sessions():
    """Debug endpoint to show session data format"""
    sessions_dir = Path("data/sessions")
    
    if not sessions_dir.exists():
        return f"<h1>Debug Sessions</h1><p>Sessions directory not found: {sessions_dir}</p>"
    
    session_files = list(sessions_dir.glob("*.json"))
    
    if not session_files:
        return "<h1>Debug Sessions</h1><p>No session files found</p>"
    
    output = "<h1>Debug Sessions</h1>"
    
    for session_file in session_files:
        try:
            with open(session_file, 'r') as f:
                session_data = json.load(f)
            
            output += f"<h2>{session_file.name}</h2>"
            output += f"<pre>{json.dumps(session_data, indent=2)}</pre><hr>"
            
        except Exception as e:
            output += f"<p>Error reading {session_file.name}: {e}</p>"
    
    return output

@dashboard_bp.route('/cleanup-p008', methods=['GET', 'POST'])
def cleanup_p008():
    """Delete P008 files from dashboard data directories"""
    
    # Check dashboard's data directory
    dashboard_data_dir = Path(DATA_DIR)
    study_sessions_dir = Path("../facial-trust-study/data/sessions")
    
    deleted_files = []
    found_files = []
    
    # Check dashboard data directory
    if dashboard_data_dir.exists():
        for csv_file in dashboard_data_dir.glob("*.csv"):
            found_files.append(f"DASHBOARD CSV: {csv_file.name}")
            if "P008" in csv_file.name or (csv_file.name.startswith("P0") and "200" not in csv_file.name):
                try:
                    csv_file.unlink()
                    deleted_files.append(f"DASHBOARD CSV: {csv_file.name}")
                except Exception as e:
                    found_files.append(f"ERROR deleting {csv_file.name}: {e}")
    
    # Check study program sessions directory  
    if study_sessions_dir.exists():
        for session_file in study_sessions_dir.glob("*.json"):
            found_files.append(f"STUDY SESSION: {session_file.name}")
            if "P008" in session_file.name or (session_file.name.startswith("P0") and "200" not in session_file.name):
                try:
                    session_file.unlink()
                    deleted_files.append(f"STUDY SESSION: {session_file.name}")
                except Exception as e:
                    found_files.append(f"ERROR deleting {session_file.name}: {e}")
    
    files_list = "<br>".join(found_files) if found_files else "No files found"
    deleted_list = "<br>".join(deleted_files) if deleted_files else "No files deleted"
    
    return f"""<h1>P008 Cleanup</h1>
    <p><strong>Found files:</strong><br>{files_list}</p>
    <p><strong>DELETED:</strong><br>{deleted_list}</p>
    <p><a href='/'>Back to Dashboard</a></p>"""


@dashboard_bp.route('/export/cleaned-data')
@login_required
def export_cleaned_data():
    """Export cleaned trial-level dataset with exclusion flags."""
    try:
        cleaned_data = data_cleaner.get_cleaned_data()
        
        # Add export footer information
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_info = f"# Generated by Face Perception Study Dashboard v1.0\n"
        export_info += f"# Mode: PRODUCTION\n"
        export_info += f"# IRB Protocol: Face Perception Study\n"
        export_info += f"# Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        export_info += f"# Total Rows: {len(cleaned_data)}\n"
        export_info += f"# Participants: {cleaned_data['pid'].nunique()}\n"
        export_info += f"# Included Trials: {cleaned_data['include_in_primary'].sum()}\n\n"
        
        # Create CSV in memory
        output = io.StringIO()
        output.write(export_info)
        cleaned_data.to_csv(output, index=False)
        output.seek(0)
        
        # Create response with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'cleaned_trial_data_{timestamp}.csv'
        
        response = app.response_class(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        
        return response
    except Exception as e:
        flash(f'Export error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@dashboard_bp.route('/export/session-metadata')
@login_required
def export_session_metadata():
    """Export session-level metadata with exclusion information."""
    try:
        cleaned_data = data_cleaner.get_cleaned_data()
        exclusion_summary = data_cleaner.get_exclusion_summary()
        
        # Create session-level summary
        session_metadata = []
        for pid in cleaned_data['pid'].unique():
            pdata = cleaned_data[cleaned_data['pid'] == pid]
            included = pdata['include_in_primary'].sum()
            total = len(pdata)
            # Calculate completion rate based on expected unique combinations
            # Convert face_id to string to avoid type comparison issues
            cleaned_data_copy = cleaned_data.copy()
            cleaned_data_copy['face_id'] = cleaned_data_copy['face_id'].astype(str)
            unique_combinations = cleaned_data_copy.groupby(['face_id', 'version']).size().shape[0]
            completion_rate = total / unique_combinations if unique_combinations > 0 else 1.0
            
            session_metadata.append({
                'participant_id': pid,
                'total_trials': total,
                'included_trials': included,
                'excluded_trials': total - included,
                'completion_rate': completion_rate,
                'mean_trust_rating': pdata['trust_rating'].mean(),
                'std_trust_rating': pdata['trust_rating'].std(),
                'versions_seen': pdata['version'].nunique(),
                'faces_seen': pdata['face_id'].nunique(),
                'source_file': pdata['source_file'].iloc[0] if 'source_file' in pdata.columns else 'unknown'
            })
        
        session_df = pd.DataFrame(session_metadata)
        
        # Create CSV
        output = io.StringIO()
        session_df.to_csv(output, index=False)
        output.seek(0)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'session_metadata_{timestamp}.csv'
        
        response = app.response_class(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        
        return response
    except Exception as e:
        flash(f'Export error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@dashboard_bp.route('/export/statistical-results')
@login_required
def export_statistical_results():
    """Export comprehensive statistical results as JSON."""
    try:
        # Run all statistical analyses
        results = {
            'export_timestamp': datetime.now().isoformat(),
            'data_summary': data_cleaner.get_data_summary(),
            'exclusion_summary': data_cleaner.get_exclusion_summary(),
            'descriptive_stats': statistical_analyzer.get_descriptive_stats(),
            'paired_t_test': statistical_analyzer.paired_t_test_half_vs_full(),
            'repeated_measures_anova': statistical_analyzer.repeated_measures_anova(),
            'inter_rater_reliability': statistical_analyzer.inter_rater_reliability(),
            'split_half_reliability': statistical_analyzer.split_half_reliability(),
            'image_summary': statistical_analyzer.get_image_summary().to_dict('records')
        }
        
        # Add export footer information
        export_info = {
            "export_metadata": {
                "generated_by": "Face Perception Study Dashboard v1.0",
                "mode": "PRODUCTION",
                "irb_protocol": "Face Perception Study",
                "exported": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "data_summary": data_cleaner.get_data_summary()
            }
        }
        results.update(export_info)
        
        # Create JSON file
        output = io.StringIO()
        json.dump(results, output, indent=2, default=str)
        output.seek(0)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'statistical_results_{timestamp}.json'
        
        response = app.response_class(
            output.getvalue(),
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        
        return response
    except Exception as e:
        flash(f'Export error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@dashboard_bp.route('/export/participant-list')
@login_required
def export_participant_list():
    """Export list of participants used in each statistical test."""
    try:
        # Get participant lists from each test
        t_test = statistical_analyzer.paired_t_test_half_vs_full()
        anova = statistical_analyzer.repeated_measures_anova()
        
        participant_data = []
        
        # Add paired t-test participants
        if 'included_participants' in t_test:
            for pid in t_test['included_participants']:
                participant_data.append({
                    'participant_id': pid,
                    'test': 'paired_t_test',
                    'n_participants': t_test.get('n_participants', 0),
                    'test_result': 'sufficient_data' if t_test.get('pvalue') is not None else 'insufficient_data'
                })
        
        # Add ANOVA participants
        if 'included_participants' in anova:
            for pid in anova['included_participants']:
                participant_data.append({
                    'participant_id': pid,
                    'test': 'repeated_measures_anova',
                    'n_participants': anova.get('n_participants', 0),
                    'test_result': 'sufficient_data' if anova.get('pvalue') is not None else 'insufficient_data'
                })
        
        participant_df = pd.DataFrame(participant_data)
        
        # Create CSV
        output = io.StringIO()
        participant_df.to_csv(output, index=False)
        output.seek(0)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'participant_list_{timestamp}.csv'
        
        response = app.response_class(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        
        return response
    except Exception as e:
        flash(f'Export error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@dashboard_bp.route('/export/all-reports')
@login_required
def export_all_reports():
    """Export all reports as a ZIP file."""
    try:
        import zipfile
        import tempfile
        
        # Create temporary ZIP file
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_zip:
            with zipfile.ZipFile(temp_zip.name, 'w') as zip_file:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                # Add cleaned data
                cleaned_data = data_cleaner.get_cleaned_data()
                cleaned_csv = io.StringIO()
                cleaned_data.to_csv(cleaned_csv, index=False)
                zip_file.writestr(f'cleaned_trial_data_{timestamp}.csv', cleaned_csv.getvalue())
                
                # Add session metadata
                session_metadata_export = []
                # Prepare denominator for completion rate once
                cleaned_data_copy = cleaned_data.copy()
                cleaned_data_copy['face_id'] = cleaned_data_copy['face_id'].astype(str)
                denom = cleaned_data_copy.groupby(['face_id', 'version']).size().shape[0]
                for pid in cleaned_data['pid'].unique():
                    pdata = cleaned_data[cleaned_data['pid'] == pid]
                    session_metadata_export.append({
                        'participant_id': pid,
                        'total_trials': len(pdata),
                        'included_trials': pdata['include_in_primary'].sum(),
                        'completion_rate': (len(pdata) / denom) if denom > 0 else 1.0,
                        'mean_trust_rating': pdata['trust_rating'].mean(),
                        'versions_seen': pdata['version'].nunique()
                    })
                session_df = pd.DataFrame(session_metadata_export)
                session_csv = io.StringIO()
                session_df.to_csv(session_csv, index=False)
                zip_file.writestr(f'session_metadata_{timestamp}.csv', session_csv.getvalue())
                
                # Add statistical results
                results = {
                    'export_timestamp': datetime.now().isoformat(),
                    'data_summary': data_cleaner.get_data_summary(),
                    'paired_t_test': statistical_analyzer.paired_t_test_half_vs_full(),
                    'repeated_measures_anova': statistical_analyzer.repeated_measures_anova()
                }
                zip_file.writestr(f'statistical_results_{timestamp}.json', json.dumps(results, indent=2, default=str))
        
        # Send ZIP file
        return send_file(temp_zip.name, 
                        as_attachment=True,
                        download_name=f'face_perception_study_reports_{timestamp}.zip',
                        mimetype='application/zip')
    except Exception as e:
        flash(f'Export error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@dashboard_bp.route('/export/methodology-report')
@login_required
def export_methodology_report():
    """Export comprehensive methodology report as PDF."""
    try:
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.pdfgen import canvas
        import tempfile
        
        # Get all the data we need
        cleaned_data = data_cleaner.get_cleaned_data()
        data_summary = data_cleaner.get_data_summary()
        exclusion_summary = data_cleaner.get_exclusion_summary()
        test_results = statistical_analyzer.run_all_analyses()
        
        # Create temporary PDF file
        timestamp = datetime.now().strftime("%Y%m%d_%HMM%S")
        filename = f'methodology_report_{timestamp}.pdf'
        
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_pdf:
            doc = SimpleDocTemplate(temp_pdf.name, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=72)
            
            # Styles
            styles = getSampleStyleSheet()
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=16,
                spaceAfter=30,
                alignment=TA_CENTER,
                textColor=colors.darkblue
            )
            heading_style = ParagraphStyle(
                'CustomHeading',
                parent=styles['Heading2'],
                fontSize=14,
                spaceAfter=12,
                spaceBefore=20,
                textColor=colors.darkblue
            )
            normal_style = styles['Normal']
            table_style = styles['Normal']
            
            # Build the story (content)
            story = []
            
            # Title Page
            story.append(Paragraph("Face Perception Study - Methodology Report", title_style))
            story.append(Spacer(1, 20))
            
            # Metadata
            metadata_data = [
                ['Generated:', datetime.now().strftime('%B %d, %Y, %H:%M')],
                ['Mode:', data_summary.get('mode', 'Unknown')],
                ['IRB Protocol #:', 'Face Perception Study'],
                ['Dashboard Version:', 'v1.0'],
                ['Data Source:', f"{data_summary.get('real_participants', 0)} real participants"]
            ]
            
            metadata_table = Table(metadata_data, colWidths=[2*inch, 4*inch])
            metadata_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ]))
            story.append(metadata_table)
            story.append(Spacer(1, 30))
            
            # Participant Overview
            story.append(Paragraph("Participant Overview", heading_style))
            
            total_participants = cleaned_data['pid'].nunique()
            included_participants = cleaned_data[cleaned_data['include_in_primary']]['pid'].nunique()
            excluded_participants = total_participants - included_participants
            exclusion_rate = (excluded_participants / total_participants * 100) if total_participants > 0 else 0
            
            # Calculate completion rates
            completion_rates = []
            # Get the expected number of unique face_id and version combinations
            # Convert face_id to string to avoid type comparison issues
            cleaned_data_copy = cleaned_data.copy()
            cleaned_data_copy['face_id'] = cleaned_data_copy['face_id'].astype(str)
            unique_combinations = cleaned_data_copy.groupby(['face_id', 'version']).size().shape[0]
            
            for pid in cleaned_data['pid'].unique():
                pdata = cleaned_data[cleaned_data['pid'] == pid]
                # Calculate completion rate based on expected unique combinations
                if unique_combinations > 0:
                    completion_rate = len(pdata) / unique_combinations * 100
                else:
                    completion_rate = 100.0  # If no expected responses, consider complete
                completion_rates.append(completion_rate)
            
            avg_completion_rate = sum(completion_rates) / len(completion_rates) if completion_rates else 0
            
            participant_data = [
                ['Metric', 'Value'],
                ['Total Participants Loaded:', str(total_participants)],
                ['Included in Final Analysis:', str(included_participants)],
                ['Excluded Sessions:', f"{excluded_participants} ({exclusion_rate:.1f}%)"],
                ['Average Completion Rate:', f"{avg_completion_rate:.1f}%"],
                ['Total Trials:', str(len(cleaned_data))],
                ['Included Trials:', str(cleaned_data['include_in_primary'].sum())]
            ]
            
            participant_table = Table(participant_data, colWidths=[3*inch, 2*inch])
            participant_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ]))
            story.append(participant_table)
            story.append(Spacer(1, 20))
            
            # Exclusion Criteria
            story.append(Paragraph("Exclusion Criteria", heading_style))
            exclusion_text = """
            The following criteria were applied to include/exclude sessions and trials:
            
            - <b>Session-level exclusions:</b> Failed attention checks, incomplete sessions, disallowed devices (non-desktop), duplicate participant IDs
            - <b>Trial-level exclusions:</b> Reaction times < 200ms or > 99.5th percentile, missing trust ratings
            - <b>Completion threshold:</b> Minimum 50% completion rate for sessions with < 48 trials, 80% for sessions with >= 48 trials
            - <b>Data quality:</b> Only trials with valid trust ratings (1-7 scale) were included in analysis
            """
            story.append(Paragraph(exclusion_text, normal_style))
            story.append(Spacer(1, 20))
            
            # Exclusion Summary
            if exclusion_summary:
                story.append(Paragraph("Exclusion Summary", heading_style))
                exclusion_data = [
                    ['Level', 'Total', 'Excluded', 'Rate'],
                    ['Sessions', str(exclusion_summary.get('session_level', {}).get('total_sessions', 0)), 
                     str(exclusion_summary.get('session_level', {}).get('excluded_sessions', 0)),
                     f"{exclusion_summary.get('session_level', {}).get('excluded_sessions', 0) / max(exclusion_summary.get('session_level', {}).get('total_sessions', 1), 1) * 100:.1f}%"],
                    ['Trials', str(exclusion_summary.get('trial_level', {}).get('total_trials', 0)),
                     str(exclusion_summary.get('trial_level', {}).get('excluded_trials', 0)),
                     f"{exclusion_summary.get('trial_level', {}).get('excluded_trials', 0) / max(exclusion_summary.get('trial_level', {}).get('total_trials', 1), 1) * 100:.1f}%"]
                ]
                
                exclusion_table = Table(exclusion_data, colWidths=[1.5*inch, 1.5*inch, 1.5*inch, 1.5*inch])
                exclusion_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ]))
                story.append(exclusion_table)
                story.append(Spacer(1, 20))
            
            # Statistical Tests Performed
            story.append(Paragraph("Statistical Tests Performed", heading_style))
            
            # Paired T-Test
            if test_results.get('paired_t_test') and not test_results['paired_t_test'].get('error'):
                t_test = test_results['paired_t_test']
                story.append(Paragraph("<b>1. Paired T-Test: Half-Face vs Full-Face</b>", normal_style))
                
                t_test_data = [
                    ['Statistic', 'Value'],
                    ['t-statistic', f"{t_test.get('statistic', 'N/A'):.3f}" if t_test.get('statistic') is not None else 'N/A'],
                    ['Degrees of Freedom', str(t_test.get('df', 'N/A'))],
                    ['p-value', f"{t_test.get('pvalue', 'N/A'):.4f}" if t_test.get('pvalue') is not None else 'N/A'],
                    ['Effect Size (Cohen\'s d)', f"{t_test.get('effect_size', 'N/A'):.3f}" if t_test.get('effect_size') is not None else 'N/A'],
                    ['N participants', str(t_test.get('n_participants', 'N/A'))],
                    ['Half-face mean', f"{t_test.get('half_face_mean', 'N/A'):.3f}" if t_test.get('half_face_mean') is not None else 'N/A'],
                    ['Full-face mean', f"{t_test.get('full_face_mean', 'N/A'):.3f}" if t_test.get('full_face_mean') is not None else 'N/A'],
                    ['95% CI', f"[{t_test.get('confidence_interval', [None, None])[0]:.3f}, {t_test.get('confidence_interval', [None, None])[1]:.3f}]" if t_test.get('confidence_interval') else 'N/A']
                ]
                
                t_test_table = Table(t_test_data, colWidths=[2.5*inch, 2.5*inch])
                t_test_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                    ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]))
                story.append(t_test_table)
                story.append(Spacer(1, 15))
            
            # Repeated Measures ANOVA
            if test_results.get('repeated_measures_anova') and not test_results['repeated_measures_anova'].get('error'):
                anova = test_results['repeated_measures_anova']
                story.append(Paragraph("<b>2. Repeated Measures ANOVA: Left vs Right vs Full</b>", normal_style))
                
                anova_data = [
                    ['Statistic', 'Value'],
                    ['F-statistic', f"{anova.get('f_statistic', 'N/A'):.3f}" if anova.get('f_statistic') is not None else 'N/A'],
                    ['df (numerator)', str(anova.get('df_num', 'N/A'))],
                    ['df (denominator)', str(anova.get('df_den', 'N/A'))],
                    ['p-value', f"{anova.get('pvalue', 'N/A'):.4f}" if anova.get('pvalue') is not None else 'N/A'],
                    ['Partial eta²', f"{anova.get('effect_size', 'N/A'):.3f}" if anova.get('effect_size') is not None else 'N/A'],
                    ['N participants', str(anova.get('n_participants', 'N/A'))]
                ]
                
                anova_table = Table(anova_data, colWidths=[2.5*inch, 2.5*inch])
                anova_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                    ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]))
                story.append(anova_table)
                story.append(Spacer(1, 15))
            
            # Reliability Measures
            if test_results.get('inter_rater_reliability') and not test_results['inter_rater_reliability'].get('error'):
                icc = test_results['inter_rater_reliability']
                story.append(Paragraph("<b>3. Inter-Rater Reliability (ICC)</b>", normal_style))
                
                icc_data = [
                    ['Statistic', 'Value'],
                    ['ICC', f"{icc.get('icc', 'N/A'):.3f}" if icc.get('icc') is not None else 'N/A'],
                    ['N raters', str(icc.get('n_raters', 'N/A'))],
                    ['N stimuli', str(icc.get('n_stimuli', 'N/A'))],
                    ['Mean ratings per stimulus', f"{icc.get('mean_ratings_per_stimulus', 'N/A'):.1f}" if icc.get('mean_ratings_per_stimulus') is not None else 'N/A']
                ]
                
                icc_table = Table(icc_data, colWidths=[2.5*inch, 2.5*inch])
                icc_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                    ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]))
                story.append(icc_table)
                story.append(Spacer(1, 15))
            
            if test_results.get('split_half_reliability') and not test_results['split_half_reliability'].get('error'):
                split_half = test_results['split_half_reliability']
                story.append(Paragraph("<b>4. Split-Half Reliability</b>", normal_style))
                
                split_half_data = [
                    ['Statistic', 'Value'],
                    ['Split-half correlation', f"{split_half.get('split_half_correlation', 'N/A'):.3f}" if split_half.get('split_half_correlation') is not None else 'N/A'],
                    ['Spearman-Brown correction', f"{split_half.get('spearman_brown', 'N/A'):.3f}" if split_half.get('spearman_brown') is not None else 'N/A'],
                    ['N participants', str(split_half.get('n_participants', 'N/A'))],
                    ['N faces per half', str(split_half.get('n_faces_per_half', 'N/A'))]
                ]
                
                split_half_table = Table(split_half_data, colWidths=[2.5*inch, 2.5*inch])
                split_half_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                    ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]))
                story.append(split_half_table)
                story.append(Spacer(1, 20))
            
            # Data Summary by Version
            story.append(Paragraph("Data Summary by Face Version", heading_style))
            
            version_summary = cleaned_data.groupby('version')['trust_rating'].agg(['count', 'mean', 'std']).round(3)
            version_data = [['Version', 'N', 'Mean', 'Std Dev']]
            
            for version in ['left', 'right', 'full']:
                if version in version_summary.index:
                    row = version_summary.loc[version]
                    version_data.append([version.title(), str(row['count']), f"{row['mean']:.3f}", f"{row['std']:.3f}"])
                else:
                    version_data.append([version.title(), '0', 'N/A', 'N/A'])
            
            version_table = Table(version_data, colWidths=[1.25*inch, 1.25*inch, 1.25*inch, 1.25*inch])
            version_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ]))
            story.append(version_table)
            story.append(Spacer(1, 30))
            
            # Footer Compliance Block
            story.append(Paragraph("Compliance Information", heading_style))
            compliance_text = f"""
            <b>IRB Protocol #:</b> Face Perception Study<br/>
            <b>Data Handling Mode:</b> {data_summary.get('mode', 'Unknown')}<br/>
            <b>Generated by:</b> Face Perception Dashboard v1.0<br/>
            <b>Date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br/>
            <b>Compliance:</b> This report was generated using real, IRB-approved study data.<br/>
            <b>Data Source:</b> {data_summary.get('real_participants', 0)} real participants from {len(data_summary.get('real_files', []))} data files.<br/>
            <b>Exclusion Transparency:</b> All exclusion criteria and rates are documented above.<br/>
            <b>Statistical Validation:</b> All tests performed using validated statistical methods with appropriate effect sizes and confidence intervals.
            """
            story.append(Paragraph(compliance_text, normal_style))
            
            # Build PDF
            doc.build(story)
            
            # Send the file
            return send_file(temp_pdf.name, 
                           as_attachment=True,
                           download_name=filename,
                           mimetype='application/pdf')
    
    except Exception as e:
        flash(f'PDF generation error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@dashboard_bp.route('/api/participant/<pid>/details')
@login_required
def api_participant_details(pid):
    """API endpoint to get detailed participant data for popup graphs."""
    try:
        if data_cleaner is None:
            return jsonify({'error': 'Data not initialized'}), 500
        
        cleaned_data = data_cleaner.get_cleaned_data()
        participant_data = cleaned_data[cleaned_data['pid'] == pid]
        
        if len(participant_data) == 0:
            return jsonify({'error': 'Participant not found'}), 404
        
        # Basic participant info
        participant_info = {
            'pid': pid,
            'total_trials': len(participant_data),
            'start_time': participant_data['timestamp'].min().isoformat() if 'timestamp' in participant_data.columns else None,
            'end_time': participant_data['timestamp'].max().isoformat() if 'timestamp' in participant_data.columns else None,
            'mean_trust': participant_data['trust_rating'].mean() if 'trust_rating' in participant_data.columns else None,
            'std_trust': participant_data['trust_rating'].std() if 'trust_rating' in participant_data.columns else None,
        }
        
        # Trust ratings over time
        trust_over_time = []
        if 'timestamp' in participant_data.columns and 'trust_rating' in participant_data.columns:
            time_data = participant_data[['timestamp', 'trust_rating']].dropna()
            time_data = time_data.sort_values('timestamp')
            trust_over_time = [
                {
                    'timestamp': row['timestamp'].isoformat() if hasattr(row['timestamp'], 'isoformat') else str(row['timestamp']),
                    'trust_rating': float(row['trust_rating'])
                }
                for _, row in time_data.iterrows()
            ]
        
        # Trust ratings by face version
        trust_by_version = {}
        if 'version' in participant_data.columns and 'trust_rating' in participant_data.columns:
            for version in participant_data['version'].unique():
                if pd.notna(version):
                    version_data = participant_data[participant_data['version'] == version]['trust_rating'].dropna()
                    if len(version_data) > 0:
                        trust_by_version[version] = {
                            'mean': float(version_data.mean()),
                            'std': float(version_data.std()),
                            'count': int(len(version_data))
                        }
        
        # Trust ratings by face ID
        trust_by_face = {}
        if 'face_id' in participant_data.columns and 'trust_rating' in participant_data.columns:
            for face_id in participant_data['face_id'].unique():
                if pd.notna(face_id):
                    face_data = participant_data[participant_data['face_id'] == face_id]['trust_rating'].dropna()
                    if len(face_data) > 0:
                        trust_by_face[face_id] = {
                            'mean': float(face_data.mean()),
                            'std': float(face_data.std()),
                            'count': int(len(face_data))
                        }
        
        # Response time analysis (if available)
        response_times = []
        if 'timestamp' in participant_data.columns:
            time_data = participant_data[['timestamp']].dropna()
            time_data = time_data.sort_values('timestamp')
            if len(time_data) > 1:
                # Calculate time differences between consecutive responses
                time_diffs = time_data['timestamp'].diff().dropna()
                response_times = [float(td.total_seconds()) for td in time_diffs if pd.notna(td)]
        
        # Survey responses (if available)
        survey_responses = {}
        survey_columns = ['trust_q1', 'trust_q2', 'trust_q3', 'pers_q1', 'pers_q2', 'pers_q3', 'pers_q4', 'pers_q5']
        for col in survey_columns:
            if col in participant_data.columns:
                values = participant_data[col].dropna()
                if len(values) > 0:
                    survey_responses[col] = {
                        'values': [float(v) for v in values if pd.notna(v)],
                        'mean': float(values.mean()),
                        'count': int(len(values))
                    }
        
        return jsonify({
            'participant_info': participant_info,
            'trust_over_time': trust_over_time,
            'trust_by_version': trust_by_version,
            'trust_by_face': trust_by_face,
            'response_times': response_times,
            'survey_responses': survey_responses,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@dashboard_bp.route('/delete-file/<filename>', methods=['POST'])
# @login_required  # Temporarily disabled for local testing
def delete_file(filename):
    """Delete a participant data file."""
    try:
        import os
        from pathlib import Path
        
        # Security check: ensure filename is safe
        if not filename or '..' in filename or '/' in filename or '\\' in filename:
            # Invalid filename detected
            flash('Invalid filename', 'error')
            return redirect(url_for('dashboard.dashboard'))
        
        # Define the data directory
        data_dir = DATA_DIR
        
        # Check if this is a session file (format: "200 (Session)")
        if filename.endswith(' (Session)'):
            # Extract participant ID and look in sessions directory
            participant_id = filename.replace(' (Session)', '')
            sessions_dir = Path("data/sessions")
            if not sessions_dir.exists():
                sessions_dir = Path("../data/sessions")
            
            # Look for the actual session file
            session_file = sessions_dir / f"{participant_id}_session.json"
            if not session_file.exists():
                # Session file not found
                flash(f'File {filename} not found', 'error')
                return redirect(url_for('dashboard.dashboard'))
            
            # Delete the session file
            try:
                session_file.unlink()
                # Session file deleted successfully
                flash(f'File {filename} deleted successfully', 'success')
                return redirect(url_for('dashboard.dashboard'))
            except Exception as e:
                # Error deleting session file
                flash(f'Error deleting {filename}: {str(e)}', 'error')
                return redirect(url_for('dashboard.dashboard'))
        
        # Check if file exists in responses directory
        file_path = data_dir / filename
        if not file_path.exists():
            # File already removed (maybe by cleanup); refresh dashboard view
            initialize_data()
            flash(f'File {filename} was already removed.', 'info')
            return redirect(url_for('dashboard.dashboard'))
        
        # Delete the file
        file_path.unlink()

        deleted_files = [filename]

        # Remove older CSVs for the same participant so duplicates do not return
        participant_id = None
        if data_cleaner and hasattr(data_cleaner, 'file_metadata'):
            for meta in getattr(data_cleaner, 'file_metadata', []):
                if meta.get('name') == filename:
                    participant_id = meta.get('pid')
                    break
        if participant_id and str(participant_id).upper() in {'UNKNOWN', 'UNKNOWN_PID', 'NAN'}:
            participant_id = None

        if not participant_id:
            stem = Path(filename).stem
            parts = stem.split('_')
            if parts:
                participant_id = parts[0]
            else:
                participant_id = stem

        def _extract_pid(name: str) -> str:
            stem = Path(name).stem
            parts = stem.split('_')
            if parts:
                candidate = parts[0]
                return candidate
            return stem

        if participant_id:
            participant_id_str = str(participant_id)
            # Remove any additional CSV files that belong to the same participant ID
            if data_cleaner and hasattr(data_cleaner, 'file_metadata'):
                for meta in getattr(data_cleaner, 'file_metadata', []):
                    meta_pid = str(meta.get('pid', '')).strip()
                    meta_path = meta.get('path')
                    if not meta_pid or not meta_path:
                        continue
                    if meta_pid == participant_id_str and meta_path.name != filename and meta_path.exists():
                        try:
                            meta_path.unlink()
                            deleted_files.append(meta_path.name)
                        except Exception as cleanup_error:
                            print(f"[delete] Failed to remove duplicate file {meta_path.name}: {cleanup_error}")
            else:
                for other_file in data_dir.glob('*.csv'):
                    if other_file.name == filename:
                        continue
                    if _extract_pid(other_file.name) == participant_id_str:
                        try:
                            other_file.unlink()
                            deleted_files.append(other_file.name)
                        except Exception as cleanup_error:
                            print(f"[delete] Failed to remove duplicate file {other_file.name}: {cleanup_error}")

            sessions_dir = Path('data/sessions')
            if not sessions_dir.exists():
                sessions_dir = Path('../data/sessions')
            if sessions_dir.exists():
                for sess_file in sessions_dir.glob(f"{participant_id_str}_*.json"):
                    try:
                        sess_file.unlink()
                        deleted_files.append(sess_file.name)
                    except Exception as cleanup_error:
                        print(f"[delete] Failed to remove session file {sess_file.name}: {cleanup_error}")

        # Reinitialize data to refresh the dashboard
        deleted_display = ', '.join(deleted_files)
        if initialize_data():
            flash(f'Deleted files: {deleted_display}', 'success')
        else:
            flash(f'Deleted files ({deleted_display}) but data refresh failed', 'warning')

        return redirect(url_for('dashboard.dashboard'))
        
    except Exception as e:
        # Delete error occurred
        flash(f'Error deleting file: {str(e)}', 'error')
        return redirect(url_for('dashboard.dashboard'))

@dashboard_bp.route('/download-file/<filename>')
# @login_required  # Temporarily disabled for local testing
def download_file(filename):
    """Download a single participant data file."""
    try:
        import os
        from pathlib import Path
        from flask import send_file
        
        # Security check: ensure filename is safe
        if not filename or '..' in filename or '/' in filename or '\\' in filename:
            flash('Invalid filename', 'error')
            return redirect(url_for('dashboard.dashboard'))
        
        # Define the data directory
        data_dir = DATA_DIR
        
        # Check if file exists
        file_path = data_dir / filename
        if not file_path.exists():
            # File already removed (maybe by cleanup); refresh dashboard view
            initialize_data()
            flash(f'File {filename} was already removed.', 'info')
            return redirect(url_for('dashboard.dashboard'))
        
        # Send the file
        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='text/csv'
        )
        
    except Exception as e:
        print(f"[inbox] DOWNLOAD ERROR: {str(e)}")
        flash(f'Error downloading file: {str(e)}', 'error')
        return redirect(url_for('dashboard.dashboard'))

@dashboard_bp.route('/download-multiple-files', methods=['POST'])
# @login_required  # Temporarily disabled for local testing
def download_multiple_files():
    """Download multiple files as a zip archive."""
    try:
        import zipfile
        import io
        from pathlib import Path
        from flask import send_file
        
        # Get the list of files from the form
        files = request.form.getlist('files')
        
        if not files:
            flash('No files selected', 'error')
            return redirect(url_for('dashboard.dashboard'))
        
        # Security check: ensure all filenames are safe
        for filename in files:
            if not filename or '..' in filename or '/' in filename or '\\' in filename:
                flash(f'Invalid filename: {filename}', 'error')
                return redirect(url_for('dashboard.dashboard'))
        
        # Define the data directory
        data_dir = DATA_DIR
        
        # Create a zip file in memory
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for filename in files:
                file_path = data_dir / filename
                if file_path.exists():
                    zip_file.write(file_path, filename)
                else:
                    print(f"[warning] File not found: {filename}")
        
        zip_buffer.seek(0)
        
        # Generate zip filename
        zip_filename = f"selected_files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        
        # Send the zip file
        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name=zip_filename,
            mimetype='application/zip'
        )
        
    except Exception as e:
        print(f"[package] ZIP DOWNLOAD ERROR: {str(e)}")
        flash(f'Error creating zip file: {str(e)}', 'error')
        return redirect(url_for('dashboard.dashboard'))

@dashboard_bp.route('/delete-multiple-files', methods=['POST'])
# @login_required  # Temporarily disabled for local testing
def delete_multiple_files():
    """Delete multiple files at once."""
    try:
        import os
        from pathlib import Path
        
        # Get the list of files from the form
        files = request.form.getlist('files')
        
        if not files:
            flash('No files selected for deletion', 'error')
            return redirect(url_for('dashboard.dashboard'))
        
        # Security check: ensure all filenames are safe
        for filename in files:
            if not filename or '..' in filename or '/' in filename or '\\' in filename:
                flash(f'Invalid filename: {filename}', 'error')
                return redirect(url_for('dashboard.dashboard'))
        
        # Define the data directory
        data_dir = DATA_DIR
        
        deleted_files = []
        failed_files = []
        
        # Delete each file
        for filename in files:
            # Check if this is a session file (display name format)
            if filename.endswith(" (Session)"):
                # Extract participant ID from display name
                participant_id = filename.replace(" (Session)", "")
                session_file_path = Path("data/sessions") / f"{participant_id}_session.json"
                backup_file_path = Path("data/sessions") / f"{participant_id}_backup.json"
                
                # Delete session files
                session_deleted = False
                if session_file_path.exists():
                    try:
                        session_file_path.unlink()
                        deleted_files.append(f"{participant_id}_session.json")
                        # Session file deleted
                        session_deleted = True
                    except Exception as e:
                        failed_files.append(f"{filename} (session file error: {str(e)})")
                        # Error deleting session file
                
                # Delete backup file if it exists
                if backup_file_path.exists():
                    try:
                        backup_file_path.unlink()
                        deleted_files.append(f"{participant_id}_backup.json")
                        # Backup file deleted
                    except Exception as e:
                        failed_files.append(f"{filename} (backup file error: {str(e)})")
                        # Error deleting backup file
                
                if not session_deleted:
                    failed_files.append(f"{filename} (session file not found)")
                    # File not found
            else:
                # Regular file deletion (CSV files, etc.)
                file_path = data_dir / filename
                if file_path.exists():
                    try:
                        file_path.unlink()
                        deleted_files.append(filename)
                        # File deleted successfully
                    except Exception as e:
                        failed_files.append(f"{filename} ({str(e)})")
                        # Error deleting file
                else:
                    failed_files.append(f"{filename} (file not found)")
                    # File not found
        
        # Provide feedback to user
        if deleted_files:
            if len(deleted_files) == 1:
                flash(f'Successfully deleted file: {deleted_files[0]}', 'success')
            else:
                flash(f'Successfully deleted {len(deleted_files)} files', 'success')
        
        if failed_files:
            if len(failed_files) == 1:
                flash(f'Failed to delete: {failed_files[0]}', 'warning')
            else:
                flash(f'Failed to delete {len(failed_files)} files', 'warning')
        
        # Also clear session data for participants whose files were deleted
        sessions_dir = Path(__file__).parent.parent / "data" / "sessions"
        if sessions_dir.exists():
            for filename in files:
                # Extract participant ID from filename (e.g., "200.csv" -> "200")
                participant_id = filename.replace('.csv', '').replace('participant_', '')
                if participant_id:
                    # Delete session files for this participant
                    session_files = list(sessions_dir.glob(f"{participant_id}_*.json"))
                    for session_file in session_files:
                        try:
                            session_file.unlink()
                            # Session file deleted
                        except Exception as e:
                            # Error deleting session file
                            pass
        
        # Reinitialize data to refresh the dashboard
        if initialize_data():
            # Data refresh successful
            pass
        else:
            # Data refresh failed
            flash('Files deleted but data refresh failed', 'warning')
        
        return redirect(url_for('dashboard.dashboard'))
        
    except Exception as e:
        # Bulk delete error occurred
        flash(f'Error deleting files: {str(e)}', 'error')
        return redirect(url_for('dashboard.dashboard'))

# Create Flask app for standalone dashboard
from flask import Flask

app = Flask(__name__)
# Use the same secret key as main app for session compatibility
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))
app.register_blueprint(dashboard_bp)

# Initialize data when the blueprint is registered
def init_dashboard():
    if initialize_data():
        print("[chart] Dashboard initialized")
    return app



