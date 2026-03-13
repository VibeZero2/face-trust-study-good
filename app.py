import sys
import os
import csv
import random
import json
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from urllib.parse import quote, unquote
from cryptography.fernet import Fernet
from dotenv import load_dotenv
import traceback

# Session management (IRB-safe addition)
try:
    from session_manager import save_session_state, load_session_state, check_session_exists, mark_session_complete, get_session_progress
    SESSION_MANAGEMENT_ENABLED = True
except ImportError:
    SESSION_MANAGEMENT_ENABLED = False
    print("       Session management not available - continuing without save/resume functionality")

# ----------------------------------------------------------------------------
# Initial setup
# ----------------------------------------------------------------------------
load_dotenv()

# Ensure stdout/stderr can emit Unicode on Windows
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))

DASHBOARD_SESSION_KEYS = {"authenticated", "username", "role"}

def _preserve_dashboard_session():
    return {key: session[key] for key in DASHBOARD_SESSION_KEYS if key in session}

def clear_participant_session():
    preserved = _preserve_dashboard_session()
    session.clear()
    if preserved:
        session.update(preserved)
        session.modified = True

# Import and register dashboard blueprint
from dashboard import dashboard_blueprint
app.register_blueprint(dashboard_blueprint, url_prefix='/dashboard')

# Configure dashboard specific settings - use same secret key for session compatibility
app.config.update(
    DASHBOARD_SECRET_KEY=os.getenv("FLASK_SECRET_KEY", os.urandom(24)),
    DASHBOARD_DEBUG=True,
    DASHBOARD_TEMPLATES_AUTO_RELOAD=True
)

# Encryption key (must be 32 url-safe base64-encoded bytes)
FERNET_KEY = os.getenv("FERNET_KEY")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY missing in .env")
fernet = Fernet(FERNET_KEY)

# Folder constants
BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "static" / "images"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Load image list once at startup.
# We accept any JPG/PNG in the folder and will present the SAME image three times
# (left crop, right crop, full) using CSS clipping.
# Accept .jpg or .jpeg in any capitalization
FACE_FILES = []
for pattern in ("*.jpg", "*.jpeg", "*.JPG", "*.JPEG"):
    FACE_FILES.extend([p.name for p in IMAGES_DIR.glob(pattern)])
# Deduplicate by filename stem (without extension) to avoid counting duplicates
unique = {}
for fname in FACE_FILES:
    stem = Path(fname).stem
    if stem not in unique:
        unique[stem] = fname  # keep first occurrence
FACE_FILES = sorted(unique.values())
FACE_FILE_MAP = {Path(fname).stem: fname for fname in FACE_FILES}
assert FACE_FILES, "No face images found. Place images in static/images/."

# ----------------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------------

# (legacy save_participant_data removed; consolidated into long-format implementation below)

def create_participant_run(pid: str, prolific_pid: str = None):
    """Initialises session variables for a new participant."""
    
    # CRITICAL: Clear all existing session data first
    clear_participant_session()
    print(f"     SESSION RESET: Cleared all session data for new participant {pid}")
    
    # Randomly pick left-first or right-first presentation
    left_first = random.choice([True, False])
    
    # Randomize the order of face files for this participant
    # Use a copy to avoid modifying the original FACE_FILES list
    randomized_faces = FACE_FILES.copy()
    random.shuffle(randomized_faces)
    
    # Store the randomized order for analysis
    face_order = [Path(fname).stem for fname in randomized_faces]
    
    sequence = []
    for fname in randomized_faces:
        # Create order with both toggle and full versions
        if left_first:
            halves = [
                {"version": "left", "file": fname},
                {"version": "right", "file": fname},
            ]
        else:
            halves = [
                {"version": "right", "file": fname},
                {"version": "left", "file": fname},
            ]
        sequence.append({
            "face_id": Path(fname).stem,
            "order": [
                {"version": "toggle", "file": fname, "start": halves[0]["version"]},
                {"version": "full", "file": fname}
            ]
        })
        
        # Debug: Print first sequence item
        if len(sequence) == 1:
            print(f"     SEQUENCE DEBUG: First sequence item: {sequence[0]}")
    
    # CRITICAL DEBUG: Log the sequence creation
    print(f"     SEQUENCE DEBUG: Creating session for participant {pid}")
    print(f"     SEQUENCE DEBUG: FACE_FILES count: {len(FACE_FILES)}")
    print(f"     SEQUENCE DEBUG: randomized_faces count: {len(randomized_faces)}")
    print(f"     SEQUENCE DEBUG: sequence count: {len(sequence)}")
    print(f"     SEQUENCE DEBUG: face_order count: {len(face_order)}")
    
    session["pid"] = pid
    session["index"] = 0  # index in sequence
    session["sequence"] = sequence
    session["responses"] = {}
    session["face_order"] = face_order  # Store the randomized face order
    session["left_first"] = left_first  # Store the left_first value for session resumption
    
    # Store Prolific ID if provided
    if prolific_pid:
        session["prolific_pid"] = prolific_pid
        
    # FINAL DEBUG: Confirm session values
    print(f"     SEQUENCE DEBUG: Final session sequence count: {len(session['sequence'])}")
    print(f"     SEQUENCE DEBUG: Final session face_order count: {len(session['face_order'])}")
    


def _build_sequence_from_face_order(face_order, left_first):
    sequence = []
    start_side = "left" if left_first else "right"
    for face_id in face_order:
        face_file = FACE_FILE_MAP.get(face_id)
        if not face_file:
            for candidate_name in FACE_FILES:
                if Path(candidate_name).stem == face_id:
                    face_file = candidate_name
                    break
        if not face_file:
            print(f"     RESUME WARNING: Missing face asset for {face_id}")
            continue
        sequence.append({
            "face_id": face_id,
            "order": [
                {"version": "toggle", "file": face_file, "start": start_side},
                {"version": "full", "file": face_file}
            ]
        })
    return sequence


def _attempt_session_restore(participant_id: str, fallback_prolific: str | None = None) -> bool:
    if not SESSION_MANAGEMENT_ENABLED:
        return False
    try:
        state_candidates = []
        session_state = load_session_state(participant_id)
        if session_state:
            state_candidates.append(session_state)
        backup_file = DATA_DIR / "sessions" / f"{participant_id}_backup.json"
        if backup_file.exists():
            with open(backup_file, "r", encoding="utf-8") as backup_handle:
                try:
                    backup_state = json.load(backup_handle)
                    state_candidates.append(backup_state)
                except json.JSONDecodeError:
                    print(f"     RESUME WARNING: Backup file for {participant_id} is corrupted")
        active_states = [candidate for candidate in state_candidates if candidate and not candidate.get("session_complete")]
        if not active_states:
            return False
        best_state = max(active_states, key=lambda candidate: candidate.get("index", 0) or 0)
        face_order = best_state.get("face_order") or []
        left_first = best_state.get("left_first")
        if left_first is None:
            for candidate in state_candidates:
                if candidate and candidate.get("left_first") is not None:
                    left_first = candidate.get("left_first")
                    break
        if left_first is None:
            left_first = True
        sequence = best_state.get("sequence")
        if not sequence:
            sequence = _build_sequence_from_face_order(face_order, bool(left_first))
        if not sequence:
            print(f"     RESUME WARNING: Unable to rebuild sequence for {participant_id}")
            return False
        clear_participant_session()
        session["consent"] = True
        session["pid"] = participant_id
        session["index"] = int(best_state.get("index", 0) or 0)
        session["face_order"] = face_order
        session["left_first"] = bool(left_first)
        session["sequence"] = sequence
        session["responses"] = best_state.get("responses") or {}
        prolific_pid = best_state.get("prolific_pid") or fallback_prolific or participant_id
        session["prolific_pid"] = prolific_pid
        print(f"     RESUME SUCCESS: Restored session for {participant_id} at index {session['index']}")
        return True
    except Exception as resume_error:
        print(f"     RESUME ERROR: {resume_error}")
        traceback.print_exc()
        return False


def save_encrypted_csv(pid: str, rows: list):
    """Encrypts and saves participant data."""
    csv_content = csv.StringIO()
    writer = csv.writer(csv_content)
    # header
    writer.writerow([
        "pid", "timestamp", "face_id", "version", "order_presented",
        "trust_rating", "masc_choice", "fem_choice",
        "emotion_rating", "trust_q2", "trust_q3",
        "pers_q1", "pers_q2", "pers_q3", "pers_q4", "pers_q5",
        "prolific_pid"  # Add Prolific ID to header
    ])
    writer.writerows(rows)
    
    # Save face order information in a separate row
    if "face_order" in session:
        # Get prolific_pid from session if available
        prolific_pid = session.get("prolific_pid", "")
        face_order_row = [pid, datetime.utcnow().isoformat(), "face_order", "metadata", "", "", "", "", "", "", "", "", "", "", "", "", prolific_pid]
        writer.writerow(face_order_row)
        # Add the face order as additional rows with index numbers
        for i, face_id in enumerate(session["face_order"]):
            order_row = [pid, "", face_id, "order_index", i+1, "", "", "", "", "", "", "", "", "", "", ""]
            writer.writerow(order_row)
    
    # Encrypt the CSV content
    encrypted_data = fernet.encrypt(csv_content.getvalue().encode())
    
    # Save to file
    enc_path = DATA_DIR / f"{pid}.enc"
    with open(enc_path, "wb") as f:
        f.write(encrypted_data)
        
    # Also save as CSV for easy access
    csv_path = DATA_DIR / f"{pid}.csv"
    with open(csv_path, "w", newline="") as f:
        f.write(csv_content.getvalue())

    # Remove older autosaves for this participant so only the latest CSV remains
    def _participant_base(name: str) -> str:
        stem = Path(name).stem
        parts = stem.split('_')
        return parts[0] if parts else stem

    new_base = _participant_base(csv_path.name)
    for existing in DATA_DIR.glob('*.csv'):
        if existing == csv_path:
            continue
        if _participant_base(existing.name) == new_base:
            try:
                existing.unlink()
            except Exception as cleanup_error:
                print(f"[csv] Cleanup skipped for {existing.name}: {cleanup_error}")

    # Return both paths for confirmation
    return {"enc": enc_path, "csv": csv_path}




def save_survey_responses(pid: str, survey_payload: dict):
    """Persist post-task survey responses for later export."""
    try:
        surveys_dir = DATA_DIR / 'surveys'
        surveys_dir.mkdir(exist_ok=True)
        timestamp = survey_payload.get('timestamp') or datetime.utcnow().isoformat()
        rows = []

        for item, value in survey_payload.get('trust_scale', {}).items():
            if value is None:
                continue
            rows.append({
                'pid': pid,
                'scale': 'general_trust',
                'item': item,
                'response': value,
                'timestamp': timestamp
            })

        for item, value in survey_payload.get('tipi', {}).items():
            if value is None:
                continue
            rows.append({
                'pid': pid,
                'scale': 'tipi',
                'item': item,
                'response': value,
                'timestamp': timestamp
            })

        if not rows:
            return None

        output_path = surveys_dir / f"{pid}_survey.csv"
        fieldnames = ['pid', 'scale', 'item', 'response', 'timestamp']
        with open(output_path, 'w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return output_path
    except Exception as survey_error:
        print(f"    Survey save failed for {pid}: {survey_error}")
        return None

def convert_dict_to_long_format(participant_id, response_dict):
    """Convert nested session responses to strict long-format rows."""
    long_responses = []

    print(f"[csv] Processing {len(response_dict)} faces")

    version_question_map = {
        "left": {
            "trust_left": "trust_left",
            "trust_rating": "trust_left",
            "emotion_left": "emotion_left",
            "emotion_rating": "emotion_left",
        },
        "right": {
            "trust_right": "trust_right",
            "trust_rating": "trust_right",
            "emotion_right": "emotion_right",
            "emotion_rating": "emotion_right",
        },
        "half": {
            "masc_choice_half": "masc_choice_half",
            "masc_choice": "masc_choice_half",
            "fem_choice_half": "fem_choice_half",
            "fem_choice": "fem_choice_half",
        },
        "full": {
            "trust_full": "trust_full",
            "trust_rating": "trust_full",
            "emotion_full": "emotion_full",
            "emotion_rating": "emotion_full",
            "masc_choice_full": "masc_choice_full",
            "masc_choice": "masc_choice_full",
            "fem_choice_full": "fem_choice_full",
            "fem_choice": "fem_choice_full",
        },
        # Legacy support - map old "both" entries to the new "full" version
        "both": {
            "trust_rating": "trust_full",
            "emotion_rating": "emotion_full",
            "masc_choice": "masc_choice_full",
            "fem_choice": "fem_choice_full",
        },
    }

    for face_id, face_data in response_dict.items():
        if not isinstance(face_data, dict):
            print(f"[csv] Skipping {face_id} - not a dictionary")
            continue

        face_timestamp = face_data.get("timestamp") or datetime.utcnow().isoformat()
        actual_pid = face_data.get("participant_id") or face_data.get("prolific_pid") or participant_id
        if not actual_pid or str(actual_pid).strip().upper() in {'UNKNOWN', 'UNKNOWN_PID', 'NAN'}:
            actual_pid = participant_id

        for version, question_map in version_question_map.items():
            version_data = face_data.get(version)
            if not isinstance(version_data, dict):
                continue

            output_version = "full" if version == "both" else version

            for source_key, question_label in question_map.items():
                if source_key not in version_data:
                    continue

                response_value = version_data[source_key]
                if response_value is None or response_value == "":
                    continue

                long_responses.append({
                    "pid": actual_pid,
                    "face_id": face_id,
                    "version": output_version,
                    "question": question_label,
                    "response": response_value,
                    "timestamp": face_timestamp,
                })

    print(f"[csv] Final CSV rows = {len(long_responses)}")
    return long_responses
def convert_wide_to_long_format(wide_responses: list) -> list:
    """
    Convert wide format responses to long format.
    
    Args:
        wide_responses: List of dictionaries in wide format
        
    Returns:
        List of dictionaries in long format
    """
    long_responses = []
    
    print(f"     CONVERT DEBUG: Processing {len(wide_responses)} wide format responses")
    
    for i, response in enumerate(wide_responses):
        print(f"     CONVERT DEBUG: Processing response {i}: {response}")
        
        participant_id = response.get("pid", "")
        timestamp = response.get("timestamp", "")
        face_id = response.get("face_id", "")
        face_view = response.get("version", "")
        
        print(f"     CONVERT DEBUG: Extracted - pid: {participant_id}, face_id: {face_id}, version: {face_view}")
        
        # Skip survey rows
        if face_id == "survey":
            print(f"     CONVERT DEBUG: Skipping survey row")
            continue
            
        # Define question types and their corresponding response values
        # Map the wide format columns to the expected question types
        question_mappings = []
        
        # Trust ratings (different versions)
        if response.get("trust_rating"):
            question_mappings.append(("trust_rating", response.get("trust_rating")))
        
        # Emotion ratings (different versions)  
        if response.get("emotion_rating"):
            question_mappings.append(("emotion_rating", response.get("emotion_rating")))
            
        # Masculinity/femininity choices (from toggle version)
        if response.get("masc_choice"):
            question_mappings.append(("masc_choice", response.get("masc_choice")))
        if response.get("fem_choice"):
            question_mappings.append(("fem_choice", response.get("fem_choice")))
            
        # Masculinity/femininity ratings (from full version) - only if not already created as choices
        if response.get("masculinity"):
            question_mappings.append(("masculinity", response.get("masculinity")))
        if response.get("femininity"):
            question_mappings.append(("femininity", response.get("femininity")))
            
        # Additional questions (if any)
        if response.get("trust_q2"):
            question_mappings.append(("trust_q2", response.get("trust_q2")))
        if response.get("trust_q3"):
            question_mappings.append(("trust_q3", response.get("trust_q3")))
        if response.get("pers_q1"):
            question_mappings.append(("pers_q1", response.get("pers_q1")))
        if response.get("pers_q2"):
            question_mappings.append(("pers_q2", response.get("pers_q2")))
        if response.get("pers_q3"):
            question_mappings.append(("pers_q3", response.get("pers_q3")))
        if response.get("pers_q4"):
            question_mappings.append(("pers_q4", response.get("pers_q4")))
        if response.get("pers_q5"):
            question_mappings.append(("pers_q5", response.get("pers_q5")))
        
        print(f"     CONVERT DEBUG: Question mappings: {question_mappings}")
        
        # Create a long format row for each non-null response
        for question_type, response_value in question_mappings:
            if response_value is not None and response_value != "":
                long_row = {
                    "pid": participant_id,
                    "face_id": face_id,
                    "version": face_view,
                    "question": question_type,
                    "response": response_value,
                    "timestamp": timestamp
                }
                long_responses.append(long_row)
                print(f"     CONVERT DEBUG: Added long row: {long_row}")
            else:
                print(f"     CONVERT DEBUG: Skipping {question_type} = {response_value} (null/empty)")
    
    print(f"     CONVERT DEBUG: Final result: {len(long_responses)} long format responses")
    return long_responses

# Wide format conversion removed - using long format only

# Wide format export removed - using long format only

def save_participant_data_long(participant_id: str, responses: dict) -> str:
    """Save responses to CSV in strict LONG format (one row per question)."""
    try:
        responses_dir = DATA_DIR / "responses"
        responses_dir.mkdir(exist_ok=True)

        base_id = participant_id or "anon"
        safe_id = base_id.replace(" ", "_").replace("/", "_").replace("\\", "_")
        filepath = responses_dir / f"{safe_id}.csv"

        # Remove legacy timestamped files for this participant
        for legacy in responses_dir.glob(f"{safe_id}_*.csv"):
            try:
                legacy.unlink()
            except Exception as cleanup_error:
                print(f"       Cleanup skipped for legacy file {legacy}: {cleanup_error}")

        long_rows = convert_dict_to_long_format(participant_id, responses)
        if not long_rows:
            print(f"       No valid responses after conversion to long format for participant {participant_id}")
            return None

        headers = ["pid", "face_id", "version", "question", "response", "timestamp"]
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(long_rows)

        print(f"    Exported long-format CSV for pid={participant_id}: {filepath}")
        return filepath
    except Exception as e:
        print(f"    Error saving participant data (long): {e}")
        return None

# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@app.route("/consent", methods=["GET", "POST"])
def consent():
    """Informed consent page shown before anything else."""
    app.logger.info(
        "[consent] %s request args=%s session_keys=%s",
        request.method,
        dict(request.args),
        list(session.keys()),
    )
    pending_pid = session.get("pending_pid")
    pending_prolific = session.get("pending_prolific_pid")
    if request.method == "GET" and not pending_pid and pending_prolific:
        session["pending_pid"] = pending_prolific
        session.modified = True
        app.logger.info("[consent] promoted prolific pid to pending pid=%s", pending_prolific)
    if request.method == "POST":
        choice = request.form.get("choice")
        app.logger.info("[consent] choice=%s", choice)
        if choice == "agree":
            session["consent"] = True
            session.modified = True
            app.logger.info(
                "[consent] consent granted; redirecting to landing with pending pid=%s",
                session.get("pending_pid"),
            )
            return redirect(url_for("landing"))
        app.logger.info("[consent] consent declined; clearing session")
        clear_participant_session()
        return render_template("declined.html")
    if not session.get("pending_prolific_pid"):
        app.logger.warning("[consent] missing pending prolific pid in session")
    return render_template("consent.html")



@app.route("/survey", methods=["GET", "POST"])
def survey():
    pid = session.get("pid")
    if not pid:
        return redirect(url_for("landing"))
    if request.method == "POST":
        # Collect trust and personality survey responses
        trust_scale = {}
        missing_trust = []
        for idx in range(1, 7):
            value = request.form.get(f"trust{idx}")
            if value is None:
                missing_trust.append(idx)
            trust_scale[f"trust_{idx}"] = value

        tipi_scale = {}
        missing_tipi = []
        for idx in range(1, 11):
            value = request.form.get(f"tipi{idx}")
            if value is None:
                missing_tipi.append(idx)
            tipi_scale[f"tipi_{idx}"] = value

        if missing_trust or missing_tipi:
            flash('Please answer every survey question before continuing.', 'error')
            return render_template('survey.html')

        prolific_pid = session.get('prolific_pid', '')

        survey_payload = {
            'timestamp': datetime.utcnow().isoformat(),
            'trust_scale': trust_scale,
            'tipi': tipi_scale
        }
        session['survey_responses'] = survey_payload
        save_survey_responses(pid, survey_payload)

        # Log the form data for debugging and finish session
        print(f"Form data received (survey): {dict(request.form)}")

        # We already saved responses incrementally during /task POSTs
        # Mark session complete and ensure backups reflect the final state
        session["session_complete"] = True
        if SESSION_MANAGEMENT_ENABLED:
            try:
                completion_marked = mark_session_complete(pid)
                if not completion_marked:
                    print(f"       Session completion flag not persisted for {pid}")
            except Exception as e:
                print(f"       Session completion marking failed (non-critical): {e}")

        try:
            backup_dir = DATA_DIR / "sessions"
            backup_file = backup_dir / f"{pid}_backup.json"
            if backup_file.exists():
                with open(backup_file, "r", encoding="utf-8") as f:
                    backup_data = json.load(f)
                backup_data["session_complete"] = True
                backup_data["completion_timestamp"] = datetime.utcnow().isoformat()
                with open(backup_file, "w", encoding="utf-8") as f:
                    json.dump(backup_data, f, indent=2)
        except Exception as backup_error:
            print(f"       Backup completion update failed: {backup_error}")

        prolific_pid = session.get("prolific_pid", "")
        clear_participant_session()
        return redirect(url_for("done", pid=pid, PROLIFIC_PID=prolific_pid))
    return render_template("survey.html")

@app.route("/")
def landing():
    pid = request.args.get("pid")
    prolific_pid = request.args.get("PROLIFIC_PID", "")
    study_id = request.args.get("STUDY_ID")
    session_id = request.args.get("SESSION_ID")

    app.logger.info(
        "[landing] incoming args pid=%s prolific=%s study=%s session=%s",
        pid,
        prolific_pid,
        study_id,
        session_id,
    )

    if pid:
        session["pending_pid"] = pid
    elif "pending_pid" in session:
        pid = session["pending_pid"]

    if prolific_pid:
        session["pending_prolific_pid"] = prolific_pid
    elif "pending_prolific_pid" in session:
        prolific_pid = session["pending_prolific_pid"]
    elif "prolific_pid" in session:
        prolific_pid = session["prolific_pid"]

    if study_id:
        session["pending_study_id"] = study_id
    if session_id:
        session["pending_session_id"] = session_id

    stored = {k: session.get(k) for k in ("pending_pid", "pending_prolific_pid", "pending_study_id", "pending_session_id")}
    if any(stored.values()):
        app.logger.info("[landing] stored session params: %s", stored)
        session.modified = True

    if "consent" not in session:
        # Show homepage when no query params and no pending session data
        if not any(stored.values()) and not request.args:
            return render_template("home.html")
        app.logger.info("[landing] redirecting to consent (pending pid=%s)", stored.get("pending_pid"))
        return redirect(url_for("consent"))

    if not pid and prolific_pid:
        pid = prolific_pid
        session["pending_pid"] = pid
        session.modified = True

    app.logger.info(
        "[landing] resolved identifiers pid=%s prolific=%s stored_pid=%s",
        pid,
        prolific_pid,
        session.get("pending_pid"),
    )
    if study_id or session_id:
        app.logger.info(
            "[landing] study metadata study=%s session=%s",
            study_id,
            session_id,
        )

    candidate_pid = pid or session.get("pending_pid") or prolific_pid
    fallback_prolific = prolific_pid or session.get("pending_prolific_pid")
    if candidate_pid and SESSION_MANAGEMENT_ENABLED:
        try:
            existing_state = load_session_state(candidate_pid)
            if not existing_state and fallback_prolific and fallback_prolific != candidate_pid:
                app.logger.info(
                    "[landing] no state for %s, checking prolific id %s",
                    candidate_pid,
                    fallback_prolific,
                )
                existing_state = load_session_state(fallback_prolific)
                if existing_state:
                    candidate_pid = fallback_prolific
                    session["pending_pid"] = candidate_pid
                    session.modified = True
                    pid = candidate_pid
            if existing_state:
                if existing_state.get("session_complete"):
                    app.logger.info(
                        "[landing] session already complete for %s; sending to done",
                        candidate_pid,
                    )
                    completion_pid = existing_state.get("participant_id", candidate_pid)
                    completion_prolific = (
                        existing_state.get("prolific_pid")
                        or fallback_prolific
                        or completion_pid
                    )
                    return redirect(
                        url_for(
                            "done",
                            pid=completion_pid,
                            PROLIFIC_PID=completion_prolific,
                        )
                    )
                if _attempt_session_restore(candidate_pid, fallback_prolific):
                    app.logger.info(
                        "[landing] restored existing session for %s",
                        candidate_pid,
                    )
                    return redirect(url_for("task", pid=candidate_pid))
        except Exception:
            app.logger.exception(
                "[landing] failed to resume session for %s",
                candidate_pid,
            )

    try:
        if pid:
            app.logger.info("[landing] creating session for pid=%s", pid)
            print(f"     LANDING DEBUG: Creating session for PID: {pid}")
            create_participant_run(pid, prolific_pid or pid)
            return redirect(url_for("task", pid=pid))

        return render_template("index.html", prolific_pid=prolific_pid)
    except Exception as e:
        with open("error.log", "a", encoding="utf-8") as log_file:
            log_file.write(f"[landing] {e}\n")
            traceback.print_exc(file=log_file)
        app.logger.exception("[landing] failed")
        raise
@app.route("/instructions")
def instructions():
    if "pid" not in session:
        return redirect(url_for("landing"))
    return render_template("instructions.html")


@app.route("/start", methods=["POST"])
def start_manual():
    pid = request.form.get("pid", "").strip()
    print(f"     START DEBUG: Received start request for PID: {pid}")
    if not pid:
        abort(400)
    prolific_pid = request.form.get("prolific_pid", "").strip()
    if not prolific_pid or prolific_pid == "UNKNOWN_PID":
        prolific_pid = pid  # Use participant ID as fallback
    print(f"     START DEBUG: Prolific PID: {prolific_pid}")

    restored = False
    if SESSION_MANAGEMENT_ENABLED:
        try:
            print(f"     START DEBUG: Checking for existing session data for {pid}")
            restored = _attempt_session_restore(pid, prolific_pid)
        except Exception as e:
            print(f"       Session resume failed (non-critical): {e}")
            import traceback
            traceback.print_exc()

    if restored:
        return redirect(url_for("task", pid=pid))

    create_participant_run(pid, prolific_pid)
    return redirect(url_for("instructions"))

@app.route("/task", methods=["GET", "POST"])
def task():
    # If session missing but pid present in query (e.g., redirect loop), try to resume first
    # Use an explicit local flag to avoid accidental UnboundLocalError
    task_is_complete = False
    if "pid" not in session:
        if request.method == "GET":
            qpid = request.args.get("pid")
            if qpid:
                prolific_query = request.args.get("PROLIFIC_PID", None)
                if SESSION_MANAGEMENT_ENABLED and _attempt_session_restore(qpid, prolific_query):
                    print(f"     RESUME DEBUG: Session restored for {qpid}")
                else:
                    print(f"     RESUME DEBUG: Starting fresh session for {qpid}")
                    create_participant_run(qpid, prolific_query)
                    return redirect(url_for("instructions"))
            else:
                return redirect(url_for("landing"))
        else:
            # For POST requests without session, redirect to landing
            return redirect(url_for("landing"))

    # Handle POST (save previous answer)
    if request.method == "POST":
        print(f"     POST DEBUG: Form submission received for {request.form.get('version', 'UNKNOWN')}")
        print(f"     POST DEBUG: Session keys: {list(session.keys()) if 'pid' in session else 'NO SESSION'}")
        print(f"     POST DEBUG: Session index: {session.get('index', 'NOT SET')}")
        
        # Check if session exists
        if "pid" not in session:
            print(f"    POST DEBUG: No session found, redirecting to landing")
            return redirect(url_for("landing"))
        
        data = session["sequence"][session["index"] // 2]
        face_id = data["face_id"]
        version = request.form["version"]
        timestamp = datetime.utcnow().isoformat()
        
        # Get prolific PID from form or session
        prolific_pid = request.form.get("prolific_pid", "").strip()
        if not prolific_pid:
            prolific_pid = session.get("prolific_pid", session.get("pid", "UNKNOWN_PID"))
        
        print(f"     POST DEBUG: Processing face_id: {face_id}, version: {version}")
        
        # Initialize face responses dictionary if not exists
        if face_id not in session["responses"]:
            session["responses"][face_id] = {
                "participant_id": session["pid"],
                "timestamp": timestamp,
                "face_id": face_id,
                "prolific_pid": prolific_pid,
                "left": {},
                "right": {},
                "half": {},
                "full": {}
            }
        
        if version == "full":
            # Full face rating - store in "full" section
            trust_full = request.form.get("trust_full")
            emotion_full = request.form.get("emotion_full")
            masc = request.form.get("masc")
            fem = request.form.get("fem")
            
            # Store responses in dictionary format (overwrite duplicates)
            if trust_full:
                session["responses"][face_id]["full"]["trust_full"] = trust_full
            if emotion_full:
                session["responses"][face_id]["full"]["emotion_full"] = emotion_full
            if masc:
                session["responses"][face_id]["full"]["masc_choice_full"] = masc
            if fem:
                session["responses"][face_id]["full"]["fem_choice_full"] = fem
                
        elif version == "toggle":
            # Toggle version - capture half-face and side-specific responses
            trust_left = request.form.get("trust_left")
            emotion_left = request.form.get("emotion_left")
            trust_right = request.form.get("trust_right")
            emotion_right = request.form.get("emotion_right")
            masc_toggle = request.form.get("masc_toggle")
            fem_toggle = request.form.get("fem_toggle")

            left_responses = session["responses"][face_id]["left"]
            right_responses = session["responses"][face_id]["right"]
            half_responses = session["responses"][face_id]["half"]

            if trust_left:
                left_responses["trust_left"] = trust_left
            if emotion_left:
                left_responses["emotion_left"] = emotion_left

            if trust_right:
                right_responses["trust_right"] = trust_right
            if emotion_right:
                right_responses["emotion_right"] = emotion_right

            if masc_toggle:
                half_responses["masc_choice_half"] = masc_toggle
            if fem_toggle:
                half_responses["fem_choice_half"] = fem_toggle
        else:
            # Legacy support for other versions
            trust_rating = request.form.get("trust")
            emotion_rating = request.form.get("emotion")
            masc_choice = request.form.get("masc")
            fem_choice = request.form.get("fem")
            prolific_pid = session.get("prolific_pid", "")
            
            # Store responses in dictionary format with version information
            if face_id not in session["responses"]:
                session["responses"][face_id] = {
                    "participant_id": session["pid"],
                    "timestamp": datetime.utcnow().isoformat(),
                    "face_id": face_id,
                    "prolific_pid": prolific_pid
                }
            
            # Store version-specific responses
            if version not in session["responses"][face_id]:
                session["responses"][face_id][version] = {}
            
            if trust_rating:
                session["responses"][face_id][version]["trust_rating"] = trust_rating
            if emotion_rating:
                session["responses"][face_id][version]["emotion_rating"] = emotion_rating
            if masc_choice:
                session["responses"][face_id][version]["masc_choice"] = masc_choice
            if fem_choice:
                session["responses"][face_id][version]["fem_choice"] = fem_choice
        # Save the current index before advancing
        current_index = session["index"]
        session["index"] += 1
        
        total_steps = len(session.get("sequence") or []) * 2
        task_is_complete = total_steps > 0 and session["index"] >= total_steps
        if task_is_complete:
            session["session_complete"] = True
            session["completion_timestamp"] = datetime.utcnow().isoformat()
        else:
            session.pop("completion_timestamp", None)
        
        print(f"     FORM PROCESSING COMPLETE: Advanced session index from {current_index} to {session['index']}")
        
        
        # IRB-Safe: Save session state after each response (non-intrusive addition)
        if SESSION_MANAGEMENT_ENABLED:
            try:
                save_result = save_session_state(session["pid"], dict(session))
                if task_is_complete:
                    try:
                        completion_marked = mark_session_complete(session["pid"])
                        if not completion_marked:
                            print(f"       Session completion flag not persisted for {session['pid']}")
                    except Exception as mark_error:
                        print(f"       Session completion update failed: {mark_error}")

                # Also save a backup copy directly to ensure it works
                try:
                    import json
                    backup_file = DATA_DIR / "sessions" / f"{session['pid']}_backup.json"
                    backup_data = {
                        "participant_id": session["pid"],
                        "timestamp": datetime.utcnow().isoformat(),
                        "index": session["index"],
                        "face_order": session["face_order"],
                        "responses": session["responses"],
                        "prolific_pid": session.get("prolific_pid", ""),
                        "session_complete": session.get("session_complete", False)
                    }
                    if session.get("completion_timestamp"):
                        backup_data["completion_timestamp"] = session["completion_timestamp"]
                    with open(backup_file, 'w') as f:
                        json.dump(backup_data, f, indent=2)
                    print(f"    Backup session saved to {backup_file}")
                except Exception as backup_e:
                    print(f"       Backup save failed: {backup_e}")
                    
            except Exception as e:
                print(f"       Session save failed (non-critical): {e}")
                import traceback
                traceback.print_exc()
        
        # Save responses to CSV immediately for dashboard visibility
        print(f"     ENTERING CSV SAVE SECTION for participant {session.get('pid', 'UNKNOWN')}")
        try:
            # Save directly using the nested dictionary format
            participant_id = session["pid"]
            prolific_pid = session.get("prolific_pid", participant_id)
            
            # Use the Prolific ID for filename if available, otherwise use the participant ID
            save_id = prolific_pid if prolific_pid else participant_id
            
            print(f"     CSV SAVE DEBUG: Attempting to save CSV for participant {participant_id}")
            print(f"     CSV SAVE DEBUG: Session responses keys: {list(session['responses'].keys())}")
            print(f"     CSV SAVE DEBUG: Session responses structure:")
            for face_id, face_data in session["responses"].items():
                print(f"  Face {face_id}: {list(face_data.keys())}")
            
            # Save both formats only after full screen (ensures a complete face row)
            # Save only in long format
            long_path = None
            try:
                long_path = save_participant_data_long(participant_id, session["responses"])
            except Exception as e:
                print(f"       Long export failed: {e}")
            if not long_path:
                print("    CSV SAVE FAILED: long format export failed")
            
            # Single timestamped save only (no extra per-participant CSV)
                
        except Exception as e:
            print(f"       Live response saving failed (non-critical): {e}")
            import traceback
            traceback.print_exc()

    # Check if finished
    if task_is_complete:
        return redirect(url_for("survey"))

    # Determine current image to show
    face_index = session["index"] // 2
    image_index = session["index"] % 2
    current = session["sequence"][face_index]
    
    # Debug logging
    print(f"     TASK DEBUG: session['index']: {session['index']}")
    print(f"     TASK DEBUG: face_index: {face_index}, image_index: {image_index}")
    print(f"     TASK DEBUG: current sequence item: {current}")
    print(f"     TASK DEBUG: current['order'] length: {len(current['order'])}")
    
    image_dict = current["order"][image_index]
    image_file = image_dict["file"]
    version = image_dict["version"]
    
    if version == "toggle":
        side = image_dict.get("start", "left")
    else:
        side = version

    # Determine which blocks to show in template
    show_mf_questions = version == "compare"
    show_trust_questions = version in ("toggle", "full")

    progress = face_index + 1
    
    # CRITICAL DEBUG: Log the display values
    print(f"     DISPLAY DEBUG: Participant {session['pid']} - Face {progress} of {len(session['face_order'])}")
    print(f"     DISPLAY DEBUG: session['index']: {session['index']}")
    print(f"     DISPLAY DEBUG: face_index: {face_index}")
    print(f"     DISPLAY DEBUG: len(session['sequence']): {len(session['sequence'])}")
    print(f"     DISPLAY DEBUG: len(session['face_order']): {len(session['face_order'])}")
    
    return render_template(
        "task.html",
        pid=session["pid"],
        image_url=url_for("static", filename=f"images/{image_file}"),
        face_id=current["face_id"],
        version=version,
        progress=progress,
        total=len(session["face_order"]),  # Show number of faces, not phases
        show_mf=show_mf_questions,
        show_trust=show_trust_questions,
        side=side,
    )


@app.route("/done")
def done():
    pid = request.args.get("pid")
    prolific_pid = request.args.get("PROLIFIC_PID", "")
    
    # Prepare completion URL for Prolific if needed
    completion_url = ""
    if prolific_pid:
        # You would replace this with your actual Prolific completion URL
        completion_url = "https://app.prolific.co/submissions/complete?cc=COMPLETION_CODE"
    
    return render_template("done.html", pid=pid, prolific_pid=prolific_pid, completion_url=completion_url)

# ----------------------------------------------------------------------------
if __name__ == "__main__":
    # Set default port to 3000
    port = 3000
    print(f"Starting Facial Trust Study on port {port}")
    print(f"Study available at http://localhost:{port}")
    print("Using localhost binding for Windows compatibility")
    try:
        # Use 0.0.0.0 for both Render deployment and local development (allows localhost access)
        host = "0.0.0.0"
        app.run(host=host, port=port, debug=False)
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"Port {port} is already in use. Please stop other services on this port.")
        else:
            print(f"Error starting server: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")
