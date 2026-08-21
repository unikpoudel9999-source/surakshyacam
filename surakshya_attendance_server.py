from __future__ import annotations

import csv
import io
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    request,
    send_from_directory,
    url_for,
)
from insightface.app import FaceAnalysis
from ultralytics import YOLO


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

YOLO_MODEL_PATH = BASE_DIR / "best (2).pt"

DATA_DIR = BASE_DIR / "surakshya_data"
ORIGINAL_DIR = DATA_DIR / "events" / "original"
ANNOTATED_DIR = DATA_DIR / "events" / "annotated"
ENROLLMENT_DIR = DATA_DIR / "students"
DATABASE_PATH = DATA_DIR / "surakshya.db"

# YOLO face detector settings.
YOLO_IMAGE_SIZE = 640
YOLO_CONFIDENCE = 0.25

# Face recognition settings.
#
# IMPORTANT:
# These are STARTING values, not universal accuracy guarantees.
# Tune them with your own enrolled-student / unknown-person test set.
RECOGNITION_THRESHOLD = 0.45
AMBIGUITY_MARGIN = 0.03

# InsightFace model pack. On first run InsightFace may download it.
INSIGHTFACE_MODEL_PACK = "buffalo_l"
INSIGHTFACE_DET_SIZE = (640, 640)

# Require a reasonable face size before attempting identity matching.
MIN_FACE_WIDTH = 35
MIN_FACE_HEIGHT = 35


# ============================================================
# DIRECTORIES
# ============================================================

for directory in (
    DATA_DIR,
    ORIGINAL_DIR,
    ANNOTATED_DIR,
    ENROLLMENT_DIR,
):
    directory.mkdir(
        parents=True,
        exist_ok=True
    )


# ============================================================
# MODELS
# ============================================================

if not YOLO_MODEL_PATH.exists():
    raise FileNotFoundError(
        f"YOLO model not found: {YOLO_MODEL_PATH}\n"
        'Put "best (2).pt" beside this server file.'
    )

print("Loading YOLO detector...")
yolo_model = YOLO(
    str(YOLO_MODEL_PATH)
)
yolo_lock = threading.Lock()
print("YOLO loaded.")
print("YOLO classes:", yolo_model.names)

print()
print("Loading InsightFace recognition model...")
face_app = FaceAnalysis(
    name=INSIGHTFACE_MODEL_PACK,
    providers=["CPUExecutionProvider"]
)
face_app.prepare(
    ctx_id=-1,
    det_size=INSIGHTFACE_DET_SIZE
)
face_lock = threading.Lock()
print("InsightFace loaded.")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# DATABASE
# ============================================================

def db() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30
    )
    connection.row_factory = sqlite3.Row
    connection.execute(
        "PRAGMA foreign_keys = ON"
    )
    connection.execute(
        "PRAGMA journal_mode = WAL"
    )
    return connection


def init_database() -> None:
    with db() as connection:

        connection.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                roll_number TEXT,
                class_name TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS student_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                reference_image TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY(student_id)
                    REFERENCES students(id)
                    ON DELETE CASCADE
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                esp_event_number INTEGER,
                event_timestamp TEXT NOT NULL,
                event_date TEXT,
                event_time TEXT,
                pir_sensor TEXT NOT NULL,

                original_image TEXT NOT NULL,
                annotated_image TEXT NOT NULL,

                yolo_face_count INTEGER NOT NULL,
                known_face_count INTEGER NOT NULL,
                unknown_face_count INTEGER NOT NULL,
                unverified_face_count INTEGER NOT NULL,

                buzzer_triggered INTEGER NOT NULL DEFAULT 0,
                received_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS face_detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,

                yolo_confidence REAL NOT NULL,
                x1 REAL NOT NULL,
                y1 REAL NOT NULL,
                x2 REAL NOT NULL,
                y2 REAL NOT NULL,

                recognition_status TEXT NOT NULL,
                student_id INTEGER,
                similarity REAL,
                second_similarity REAL,
                similarity_margin REAL,

                FOREIGN KEY(event_id)
                    REFERENCES events(id)
                    ON DELETE CASCADE,

                FOREIGN KEY(student_id)
                    REFERENCES students(id)
                    ON DELETE SET NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                attendance_date TEXT NOT NULL,
                first_seen_time TEXT NOT NULL,
                first_seen_timestamp TEXT NOT NULL,
                event_id INTEGER NOT NULL,
                pir_sensor TEXT NOT NULL,
                similarity REAL NOT NULL,
                captured_image TEXT NOT NULL,
                created_at TEXT NOT NULL,

                UNIQUE(student_id, attendance_date),

                FOREIGN KEY(student_id)
                    REFERENCES students(id)
                    ON DELETE CASCADE,

                FOREIGN KEY(event_id)
                    REFERENCES events(id)
                    ON DELETE CASCADE
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS unknown_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                face_detection_id INTEGER NOT NULL,
                event_timestamp TEXT NOT NULL,
                pir_sensor TEXT NOT NULL,
                best_similarity REAL,
                closest_student_id INTEGER,
                captured_image TEXT NOT NULL,
                buzzer_triggered INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,

                FOREIGN KEY(event_id)
                    REFERENCES events(id)
                    ON DELETE CASCADE,

                FOREIGN KEY(face_detection_id)
                    REFERENCES face_detections(id)
                    ON DELETE CASCADE,

                FOREIGN KEY(closest_student_id)
                    REFERENCES students(id)
                    ON DELETE SET NULL
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_attendance_date
            ON attendance(attendance_date)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_events_timestamp
            ON events(event_timestamp)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_unknown_timestamp
            ON unknown_events(event_timestamp)
        """)


# ============================================================
# EMBEDDING CACHE
# ============================================================

embedding_cache_lock = threading.Lock()
student_embedding_cache: list[dict] = []


def reload_embedding_cache() -> None:
    global student_embedding_cache

    with db() as connection:
        rows = connection.execute("""
            SELECT
                s.id AS student_id,
                s.student_code,
                s.name,
                s.roll_number,
                s.class_name,
                e.embedding,
                e.dimensions
            FROM students s
            JOIN student_embeddings e
              ON e.student_id = s.id
            WHERE s.active = 1
            ORDER BY s.id, e.id
        """).fetchall()

    grouped: dict[int, dict] = {}

    for row in rows:
        student_id = int(
            row["student_id"]
        )

        vector = np.frombuffer(
            row["embedding"],
            dtype=np.float32
        ).copy()

        if vector.size != int(
            row["dimensions"]
        ):
            continue

        norm = np.linalg.norm(vector)

        if norm <= 0:
            continue

        vector = (
            vector / norm
        ).astype(np.float32)

        if student_id not in grouped:
            grouped[student_id] = {
                "student_id": student_id,
                "student_code": row["student_code"],
                "name": row["name"],
                "roll_number": row["roll_number"],
                "class_name": row["class_name"],
                "embeddings": []
            }

        grouped[student_id][
            "embeddings"
        ].append(vector)

    with embedding_cache_lock:
        student_embedding_cache = list(
            grouped.values()
        )

    print(
        f"Recognition cache: "
        f"{len(student_embedding_cache)} students"
    )


# ============================================================
# UTILITIES
# ============================================================

def now_iso() -> str:
    return datetime.now().isoformat(
        timespec="seconds"
    )


def normalize_timestamp(
    raw: str
) -> tuple[str, str | None, str | None]:

    value = (raw or "").strip()

    if "," in value:
        date_part, time_part = (
            value.split(",", 1)
        )

        date_part = date_part.strip()
        time_part = time_part.strip()

        return (
            f"{date_part} {time_part}",
            date_part,
            time_part
        )

    # Try already-normalized form.
    try:
        parsed = datetime.fromisoformat(
            value
        )

        return (
            parsed.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            parsed.strftime(
                "%Y-%m-%d"
            ),
            parsed.strftime(
                "%H:%M:%S"
            )
        )

    except Exception:
        return (
            value or "Unknown",
            None,
            None
        )


def unique_jpeg_name(
    prefix: str
) -> str:
    stamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    token = uuid.uuid4().hex[:8]
    return (
        f"{prefix}_{stamp}_{token}.jpg"
    )


def decode_jpeg(
    jpeg_bytes: bytes
) -> np.ndarray | None:
    array = np.frombuffer(
        jpeg_bytes,
        dtype=np.uint8
    )
    return cv2.imdecode(
        array,
        cv2.IMREAD_COLOR
    )


def clamp_box(
    box,
    width: int,
    height: int
):
    x1, y1, x2, y2 = [
        int(round(float(v)))
        for v in box
    ]

    x1 = max(
        0,
        min(x1, width - 1)
    )
    y1 = max(
        0,
        min(y1, height - 1)
    )
    x2 = max(
        x1 + 1,
        min(x2, width)
    )
    y2 = max(
        y1 + 1,
        min(y2, height)
    )

    return (
        x1, y1, x2, y2
    )


def box_iou(
    a,
    b
) -> float:

    ax1, ay1, ax2, ay2 = [
        float(v) for v in a
    ]
    bx1, by1, bx2, by2 = [
        float(v) for v in b
    ]

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    iw = max(
        0.0,
        ix2 - ix1
    )
    ih = max(
        0.0,
        iy2 - iy1
    )

    intersection = (
        iw * ih
    )

    area_a = max(
        0.0,
        ax2 - ax1
    ) * max(
        0.0,
        ay2 - ay1
    )

    area_b = max(
        0.0,
        bx2 - bx1
    ) * max(
        0.0,
        by2 - by1
    )

    union = (
        area_a +
        area_b -
        intersection
    )

    if union <= 0:
        return 0.0

    return (
        intersection / union
    )


def center_inside(
    inner_bbox,
    outer_bbox
) -> bool:

    ix1, iy1, ix2, iy2 = [
        float(v)
        for v in inner_bbox
    ]

    ox1, oy1, ox2, oy2 = [
        float(v)
        for v in outer_bbox
    ]

    cx = (
        ix1 + ix2
    ) / 2.0

    cy = (
        iy1 + iy2
    ) / 2.0

    return (
        ox1 <= cx <= ox2 and
        oy1 <= cy <= oy2
    )


# ============================================================
# YOLO FACE DETECTION
# ============================================================

def detect_yolo_faces(
    image: np.ndarray
) -> list[dict]:

    with yolo_lock:
        results = yolo_model.predict(
            source=image,
            imgsz=YOLO_IMAGE_SIZE,
            conf=YOLO_CONFIDENCE,
            verbose=False
        )

    result = results[0]

    detections = []

    if (
        result.boxes is None or
        len(result.boxes) == 0
    ):
        return detections

    boxes = (
        result.boxes.xyxy
        .detach()
        .cpu()
        .numpy()
    )

    confs = (
        result.boxes.conf
        .detach()
        .cpu()
        .numpy()
    )

    classes = (
        result.boxes.cls
        .detach()
        .cpu()
        .numpy()
        .astype(int)
    )

    for (
        box,
        confidence,
        class_id
    ) in zip(
        boxes,
        confs,
        classes
    ):
        class_name = str(
            result.names[
                int(class_id)
            ]
        )

        # Your uploaded YOLO model is trained for "face".
        # Ignore any future non-face class if the model changes.
        if class_name.lower() != "face":
            continue

        detections.append({
            "bbox": [
                float(v)
                for v in box
            ],
            "confidence": float(
                confidence
            ),
            "class_id": int(
                class_id
            ),
            "class_name": class_name
        })

    return detections


# ============================================================
# INSIGHTFACE EMBEDDINGS
# ============================================================

def insight_faces(
    image: np.ndarray
):
    with face_lock:
        faces = face_app.get(image)

    return faces


def match_insight_face_to_yolo(
    yolo_bbox,
    faces,
    already_used: set[int]
):
    best_index = None
    best_score = -1.0

    for index, face in enumerate(
        faces
    ):
        if index in already_used:
            continue

        face_box = (
            face.bbox.astype(float)
        )

        iou = box_iou(
            yolo_bbox,
            face_box
        )

        # Prefer overlap, but allow center matching
        # because different detectors can draw different boxes.
        score = iou

        if center_inside(
            face_box,
            yolo_bbox
        ):
            score += 1.0

        if score > best_score:
            best_score = score
            best_index = index

    # Require at least overlap or center containment.
    if best_index is None:
        return None

    chosen = faces[
        best_index
    ]

    chosen_box = chosen.bbox.astype(
        float
    )

    if (
        box_iou(
            yolo_bbox,
            chosen_box
        ) <= 0 and
        not center_inside(
            chosen_box,
            yolo_bbox
        )
    ):
        return None

    already_used.add(
        best_index
    )

    return chosen


def normalized_embedding_from_face(
    face
) -> np.ndarray | None:

    embedding = getattr(
        face,
        "normed_embedding",
        None
    )

    if embedding is None:
        embedding = getattr(
            face,
            "embedding",
            None
        )

    if embedding is None:
        return None

    vector = np.asarray(
        embedding,
        dtype=np.float32
    ).reshape(-1)

    norm = np.linalg.norm(vector)

    if norm <= 0:
        return None

    return (
        vector / norm
    ).astype(np.float32)


# ============================================================
# IDENTITY MATCHING
# ============================================================

def identify_student(
    embedding: np.ndarray
) -> dict:

    with embedding_cache_lock:
        cache_snapshot = list(
            student_embedding_cache
        )

    if not cache_snapshot:
        return {
            "status": "UNKNOWN",
            "student": None,
            "best_similarity": None,
            "second_similarity": None,
            "margin": None,
            "reason": "NO_STUDENTS_ENROLLED"
        }

    scores = []

    for student in cache_snapshot:

        similarities = [
            float(
                np.dot(
                    embedding,
                    reference
                )
            )
            for reference
            in student["embeddings"]
        ]

        if not similarities:
            continue

        scores.append((
            max(similarities),
            student
        ))

    if not scores:
        return {
            "status": "UNKNOWN",
            "student": None,
            "best_similarity": None,
            "second_similarity": None,
            "margin": None,
            "reason": "NO_VALID_EMBEDDINGS"
        }

    scores.sort(
        key=lambda item: item[0],
        reverse=True
    )

    best_similarity = float(
        scores[0][0]
    )

    best_student = scores[0][1]

    second_similarity = (
        float(scores[1][0])
        if len(scores) > 1
        else None
    )

    margin = (
        best_similarity -
        second_similarity
        if second_similarity is not None
        else None
    )

    threshold_ok = (
        best_similarity >=
        RECOGNITION_THRESHOLD
    )

    margin_ok = (
        margin is None or
        margin >= AMBIGUITY_MARGIN
    )

    if (
        threshold_ok and
        margin_ok
    ):
        return {
            "status": "KNOWN",
            "student": best_student,
            "best_similarity": best_similarity,
            "second_similarity": second_similarity,
            "margin": margin,
            "reason": "MATCH"
        }

    return {
        "status": "UNKNOWN",
        "student": best_student,
        "best_similarity": best_similarity,
        "second_similarity": second_similarity,
        "margin": margin,
        "reason": (
            "LOW_SIMILARITY"
            if not threshold_ok
            else "AMBIGUOUS_MATCH"
        )
    }


# ============================================================
# ATTENDANCE
# ============================================================

def mark_attendance(
    connection: sqlite3.Connection,
    *,
    student_id: int,
    event_date: str | None,
    event_time: str | None,
    event_timestamp: str,
    event_id: int,
    pir_sensor: str,
    similarity: float,
    captured_image: str
) -> str:

    if (
        not event_date or
        not event_time
    ):
        return "INVALID_RTC_TIME"

    cursor = connection.execute("""
        INSERT OR IGNORE INTO attendance (
            student_id,
            attendance_date,
            first_seen_time,
            first_seen_timestamp,
            event_id,
            pir_sensor,
            similarity,
            captured_image,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        student_id,
        event_date,
        event_time,
        event_timestamp,
        event_id,
        pir_sensor,
        similarity,
        captured_image,
        now_iso()
    ))

    if cursor.rowcount == 1:
        return "MARKED_PRESENT"

    return "ALREADY_MARKED_TODAY"


# ============================================================
# ANNOTATION
# ============================================================

def annotate_event(
    image: np.ndarray,
    results: list[dict]
) -> np.ndarray:

    output = image.copy()

    for item in results:

        x1, y1, x2, y2 = [
            int(v)
            for v in item["bbox"]
        ]

        status = item[
            "recognition_status"
        ]

        if status == "KNOWN":
            name = item.get(
                "student_name",
                "Known"
            )
            similarity = item.get(
                "similarity"
            )
            label = (
                f"{name} "
                f"{similarity:.2f}"
            )
            color = (
                0, 200, 0
            )

        elif status == "UNKNOWN":
            similarity = item.get(
                "similarity"
            )

            if similarity is None:
                label = "UNKNOWN"
            else:
                label = (
                    f"UNKNOWN {similarity:.2f}"
                )

            color = (
                0, 0, 255
            )

        else:
            label = "UNVERIFIED"
            color = (
                0, 165, 255
            )

        cv2.rectangle(
            output,
            (x1, y1),
            (x2, y2),
            color,
            2
        )

        cv2.putText(
            output,
            label,
            (
                x1,
                max(18, y1 - 8)
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA
        )

    return output


# ============================================================
# STUDENT ENROLLMENT
# ============================================================

def embedding_for_enrollment_image(
    image: np.ndarray
) -> tuple[np.ndarray | None, str]:

    yolo_faces = detect_yolo_faces(
        image
    )

    if len(yolo_faces) == 0:
        return (
            None,
            "YOLO did not detect a face"
        )

    if len(yolo_faces) > 1:
        return (
            None,
            "Enrollment photo must contain exactly one face"
        )

    faces = insight_faces(
        image
    )

    if len(faces) == 0:
        return (
            None,
            "Recognition model could not extract a face"
        )

    chosen = match_insight_face_to_yolo(
        yolo_faces[0]["bbox"],
        faces,
        set()
    )

    if chosen is None:
        return (
            None,
            "YOLO face and recognition face did not align"
        )

    embedding = (
        normalized_embedding_from_face(
            chosen
        )
    )

    if embedding is None:
        return (
            None,
            "Could not create face embedding"
        )

    return (
        embedding,
        "OK"
    )


@app.route(
    "/students/enroll",
    methods=["GET", "POST"]
)
def enroll_student():

    if request.method == "GET":
        return enrollment_page(
            message=""
        )

    student_code = (
        request.form.get(
            "student_code",
            ""
        ).strip()
    )

    name = (
        request.form.get(
            "name",
            ""
        ).strip()
    )

    roll_number = (
        request.form.get(
            "roll_number",
            ""
        ).strip()
    )

    class_name = (
        request.form.get(
            "class_name",
            ""
        ).strip()
    )

    files = request.files.getlist(
        "photos"
    )

    if not student_code or not name:
        return enrollment_page(
            message=(
                "Student ID and name are required."
            )
        ), 400

    if not files:
        return enrollment_page(
            message=(
                "Add at least one enrollment photo."
            )
        ), 400

    valid_embeddings = []
    rejected = []

    student_folder = (
        ENROLLMENT_DIR /
        student_code
    )

    student_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    for file in files:

        if not file or not file.filename:
            continue

        data = file.read()

        image = decode_jpeg(
            data
        )

        if image is None:
            rejected.append(
                f"{file.filename}: invalid image"
            )
            continue

        embedding, reason = (
            embedding_for_enrollment_image(
                image
            )
        )

        if embedding is None:
            rejected.append(
                f"{file.filename}: {reason}"
            )
            continue

        filename = unique_jpeg_name(
            "reference"
        )

        path = (
            student_folder /
            filename
        )

        # Save a normalized JPEG copy.
        if not cv2.imwrite(
            str(path),
            image
        ):
            rejected.append(
                f"{file.filename}: could not save"
            )
            continue

        valid_embeddings.append((
            embedding,
            str(
                path.relative_to(
                    DATA_DIR
                )
            )
        ))

    if not valid_embeddings:
        return enrollment_page(
            message=(
                "No usable enrollment photos. "
                + " | ".join(rejected)
            )
        ), 400

    with db() as connection:

        existing = connection.execute("""
            SELECT id
            FROM students
            WHERE student_code = ?
        """, (
            student_code,
        )).fetchone()

        if existing is None:

            cursor = connection.execute("""
                INSERT INTO students (
                    student_code,
                    name,
                    roll_number,
                    class_name,
                    active,
                    created_at
                )
                VALUES (?, ?, ?, ?, 1, ?)
            """, (
                student_code,
                name,
                roll_number,
                class_name,
                now_iso()
            ))

            student_id = cursor.lastrowid

        else:
            student_id = int(
                existing["id"]
            )

            connection.execute("""
                UPDATE students
                SET
                    name = ?,
                    roll_number = ?,
                    class_name = ?,
                    active = 1
                WHERE id = ?
            """, (
                name,
                roll_number,
                class_name,
                student_id
            ))

        for (
            embedding,
            reference_image
        ) in valid_embeddings:

            connection.execute("""
                INSERT INTO student_embeddings (
                    student_id,
                    embedding,
                    dimensions,
                    reference_image,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                student_id,
                embedding.astype(
                    np.float32
                ).tobytes(),
                int(
                    embedding.size
                ),
                reference_image,
                now_iso()
            ))

    reload_embedding_cache()

    message = (
        f"Enrolled {name}: "
        f"{len(valid_embeddings)} usable photo(s)."
    )

    if rejected:
        message += (
            " Rejected: "
            + " | ".join(rejected)
        )

    return enrollment_page(
        message=message
    )


def enrollment_page(
    message: str
):
    students = get_students()

    rows = "".join(
        f"""
        <tr>
          <td>{student["student_code"]}</td>
          <td>{student["name"]}</td>
          <td>{student["roll_number"] or ""}</td>
          <td>{student["class_name"] or ""}</td>
          <td>{student["reference_count"]}</td>
        </tr>
        """
        for student in students
    )

    return f"""
<!doctype html>
<html>
<head>
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>Enroll Student - Surakshya Cam</title>
<style>
body {{
  font-family: Arial, sans-serif;
  background:#f4f4f4;
  margin:24px;
}}
.card {{
  max-width:760px;
  background:white;
  padding:20px;
  margin-bottom:20px;
}}
label {{
  display:block;
  margin-top:12px;
  font-weight:bold;
}}
input {{
  width:100%;
  max-width:520px;
  padding:9px;
  box-sizing:border-box;
}}
button {{
  margin-top:18px;
  padding:11px 18px;
}}
.message {{
  background:#eef;
  padding:10px;
}}
table {{
  width:100%;
  border-collapse:collapse;
  background:white;
}}
th,td {{
  padding:8px;
  border:1px solid #ddd;
}}
</style>
</head>
<body>

<p>
<a href="/">Back to dashboard</a>
</p>

<div class="card">
<h1>Enroll Student</h1>

<p>
Use several clear reference photos of the same student.
Each photo must contain exactly one face.
</p>

{"<div class='message'>" + message + "</div>" if message else ""}

<form
  action="/students/enroll"
  method="post"
  enctype="multipart/form-data">

<label>Student ID</label>
<input
  name="student_code"
  required
  placeholder="STU001">

<label>Name</label>
<input
  name="name"
  required
  placeholder="Student name">

<label>Roll number</label>
<input
  name="roll_number"
  placeholder="21">

<label>Class</label>
<input
  name="class_name"
  placeholder="Grade 10 / Section A">

<label>Reference photos</label>
<input
  name="photos"
  type="file"
  accept="image/jpeg,image/png"
  multiple
  required>

<button type="submit">
Enroll / Add Photos
</button>

</form>
</div>

<h2>Enrolled Students</h2>

<table>
<thead>
<tr>
<th>ID</th>
<th>Name</th>
<th>Roll</th>
<th>Class</th>
<th>Reference Photos</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>

</body>
</html>
"""


def get_students() -> list[dict]:

    with db() as connection:

        rows = connection.execute("""
            SELECT
                s.*,
                COUNT(e.id) AS reference_count
            FROM students s
            LEFT JOIN student_embeddings e
              ON e.student_id = s.id
            WHERE s.active = 1
            GROUP BY s.id
            ORDER BY s.name
        """).fetchall()

    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# ESP32 EVENT UPLOAD
# ============================================================

@app.post("/upload")
def upload_event():

    jpeg_bytes = request.get_data(
        cache=False
    )

    if not jpeg_bytes:
        return jsonify({
            "status": "error",
            "message": "No JPEG received",
            "buzzer": False
        }), 400

    image = decode_jpeg(
        jpeg_bytes
    )

    if image is None:
        return jsonify({
            "status": "error",
            "message": "Invalid JPEG",
            "buzzer": False
        }), 400

    pir_sensor = request.headers.get(
        "X-Sensor",
        "Unknown"
    ).strip()

    (
        event_timestamp,
        event_date,
        event_time
    ) = normalize_timestamp(
        request.headers.get(
            "X-Timestamp",
            ""
        )
    )

    esp_event_raw = request.headers.get(
        "X-ESP-Event",
        ""
    ).strip()

    try:
        esp_event_number = (
            int(esp_event_raw)
            if esp_event_raw
            else None
        )
    except ValueError:
        esp_event_number = None

    filename = unique_jpeg_name(
        "event"
    )

    original_path = (
        ORIGINAL_DIR /
        filename
    )

    annotated_path = (
        ANNOTATED_DIR /
        filename
    )

    original_path.write_bytes(
        jpeg_bytes
    )

    # --------------------------------------------------------
    # Stage 1: YOLO detects faces.
    # --------------------------------------------------------

    yolo_faces = detect_yolo_faces(
        image
    )

    # --------------------------------------------------------
    # Stage 2: InsightFace produces identity embeddings.
    # --------------------------------------------------------

    recognition_faces = (
        insight_faces(image)
        if yolo_faces
        else []
    )

    used_recognition_faces: set[int] = set()

    recognition_results = []

    image_height, image_width = (
        image.shape[:2]
    )

    for yolo_face in yolo_faces:

        bbox = yolo_face[
            "bbox"
        ]

        x1, y1, x2, y2 = (
            clamp_box(
                bbox,
                image_width,
                image_height
            )
        )

        width = x2 - x1
        height = y2 - y1

        result_item = {
            "bbox": [
                x1, y1, x2, y2
            ],
            "yolo_confidence": (
                yolo_face[
                    "confidence"
                ]
            ),
            "recognition_status": (
                "UNVERIFIED"
            ),
            "student_id": None,
            "student_code": None,
            "student_name": None,
            "similarity": None,
            "second_similarity": None,
            "margin": None,
            "attendance": None,
            "reason": None
        }

        if (
            width < MIN_FACE_WIDTH or
            height < MIN_FACE_HEIGHT
        ):
            result_item["reason"] = (
                "FACE_TOO_SMALL"
            )

            recognition_results.append(
                result_item
            )
            continue

        matched_face = (
            match_insight_face_to_yolo(
                bbox,
                recognition_faces,
                used_recognition_faces
            )
        )

        if matched_face is None:
            result_item["reason"] = (
                "NO_RECOGNITION_FACE"
            )

            recognition_results.append(
                result_item
            )
            continue

        embedding = (
            normalized_embedding_from_face(
                matched_face
            )
        )

        if embedding is None:
            result_item["reason"] = (
                "NO_EMBEDDING"
            )

            recognition_results.append(
                result_item
            )
            continue

        identity = identify_student(
            embedding
        )

        result_item[
            "recognition_status"
        ] = identity["status"]

        result_item[
            "similarity"
        ] = identity[
            "best_similarity"
        ]

        result_item[
            "second_similarity"
        ] = identity[
            "second_similarity"
        ]

        result_item[
            "margin"
        ] = identity["margin"]

        result_item[
            "reason"
        ] = identity["reason"]

        candidate = identity[
            "student"
        ]

        if candidate is not None:
            result_item[
                "student_id"
            ] = candidate[
                "student_id"
            ]

            result_item[
                "student_code"
            ] = candidate[
                "student_code"
            ]

            result_item[
                "student_name"
            ] = candidate[
                "name"
            ]

        recognition_results.append(
            result_item
        )

    known_count = sum(
        item["recognition_status"]
        == "KNOWN"
        for item in recognition_results
    )

    unknown_count = sum(
        item["recognition_status"]
        == "UNKNOWN"
        for item in recognition_results
    )

    unverified_count = sum(
        item["recognition_status"]
        == "UNVERIFIED"
        for item in recognition_results
    )

    # Buzzer only for a valid embedding that does NOT meet
    # the known-person match rules. A blurry/unverified face
    # is logged but does not automatically alarm.
    buzzer = (
        unknown_count > 0
    )

    annotated = annotate_event(
        image,
        recognition_results
    )

    if not cv2.imwrite(
        str(annotated_path),
        annotated
    ):
        original_path.unlink(
            missing_ok=True
        )

        return jsonify({
            "status": "error",
            "message": (
                "Could not save annotated image"
            ),
            "buzzer": False
        }), 500

    attendance_actions = []

    with db() as connection:

        cursor = connection.execute("""
            INSERT INTO events (
                esp_event_number,
                event_timestamp,
                event_date,
                event_time,
                pir_sensor,
                original_image,
                annotated_image,
                yolo_face_count,
                known_face_count,
                unknown_face_count,
                unverified_face_count,
                buzzer_triggered,
                received_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
        """, (
            esp_event_number,
            event_timestamp,
            event_date,
            event_time,
            pir_sensor,
            filename,
            filename,
            len(yolo_faces),
            known_count,
            unknown_count,
            unverified_count,
            1 if buzzer else 0,
            now_iso()
        ))

        event_id = cursor.lastrowid

        for item in recognition_results:

            detection_cursor = (
                connection.execute("""
                    INSERT INTO face_detections (
                        event_id,
                        yolo_confidence,
                        x1,
                        y1,
                        x2,
                        y2,
                        recognition_status,
                        student_id,
                        similarity,
                        second_similarity,
                        similarity_margin
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                """, (
                    event_id,
                    item[
                        "yolo_confidence"
                    ],
                    item["bbox"][0],
                    item["bbox"][1],
                    item["bbox"][2],
                    item["bbox"][3],
                    item[
                        "recognition_status"
                    ],
                    (
                        item["student_id"]
                        if item[
                            "recognition_status"
                        ] == "KNOWN"
                        else None
                    ),
                    item["similarity"],
                    item[
                        "second_similarity"
                    ],
                    item["margin"]
                ))
            )

            face_detection_id = (
                detection_cursor.lastrowid
            )

            if (
                item[
                    "recognition_status"
                ] == "KNOWN" and
                item["student_id"] is not None and
                item["similarity"] is not None
            ):
                action = mark_attendance(
                    connection,
                    student_id=int(
                        item["student_id"]
                    ),
                    event_date=event_date,
                    event_time=event_time,
                    event_timestamp=event_timestamp,
                    event_id=event_id,
                    pir_sensor=pir_sensor,
                    similarity=float(
                        item["similarity"]
                    ),
                    captured_image=filename
                )

                item["attendance"] = action

                attendance_actions.append({
                    "student_id": item[
                        "student_code"
                    ],
                    "name": item[
                        "student_name"
                    ],
                    "action": action
                })

            elif (
                item[
                    "recognition_status"
                ] == "UNKNOWN"
            ):
                connection.execute("""
                    INSERT INTO unknown_events (
                        event_id,
                        face_detection_id,
                        event_timestamp,
                        pir_sensor,
                        best_similarity,
                        closest_student_id,
                        captured_image,
                        buzzer_triggered,
                        created_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, 1, ?
                    )
                """, (
                    event_id,
                    face_detection_id,
                    event_timestamp,
                    pir_sensor,
                    item["similarity"],
                    item["student_id"],
                    filename,
                    now_iso()
                ))

    print()
    print("=" * 72)
    print(
        f"SURAKSHYA EVENT #{event_id}"
    )
    print(
        f"RTC:       {event_timestamp}"
    )
    print(
        f"PIR:       {pir_sensor}"
    )
    print(
        f"YOLO face: {len(yolo_faces)}"
    )
    print(
        f"Known:     {known_count}"
    )
    print(
        f"Unknown:   {unknown_count}"
    )
    print(
        f"Unverified:{unverified_count}"
    )
    print(
        f"Buzzer:    {'YES' if buzzer else 'NO'}"
    )

    for item in recognition_results:

        if (
            item[
                "recognition_status"
            ] == "KNOWN"
        ):
            print(
                "KNOWN -> "
                f"{item['student_name']} "
                f"({item['student_code']}) "
                f"sim={item['similarity']:.3f} "
                f"attendance={item['attendance']}"
            )

        elif (
            item[
                "recognition_status"
            ] == "UNKNOWN"
        ):
            print(
                "UNKNOWN -> "
                f"best_similarity="
                f"{item['similarity']}"
            )

        else:
            print(
                "UNVERIFIED -> "
                f"{item['reason']}"
            )

    print("=" * 72)

    # Compact JSON so ESP32 can reliably find:
    # "buzzer":true
    return jsonify({
        "status": "success",
        "event_id": event_id,
        "timestamp": event_timestamp,
        "pir_sensor": pir_sensor,
        "buzzer": buzzer,
        "known_count": known_count,
        "unknown_count": unknown_count,
        "unverified_count": unverified_count,
        "attendance": attendance_actions,
        "faces": recognition_results
    })


# ============================================================
# API
# ============================================================

@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "yolo_model": YOLO_MODEL_PATH.name,
        "recognition_model": (
            INSIGHTFACE_MODEL_PACK
        ),
        "students_cached": len(
            student_embedding_cache
        ),
        "recognition_threshold": (
            RECOGNITION_THRESHOLD
        ),
        "ambiguity_margin": (
            AMBIGUITY_MARGIN
        )
    })


@app.get("/api/attendance/today")
def api_attendance_today():

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    return jsonify(
        get_attendance(today)
    )


@app.get("/api/unknown")
def api_unknown():

    with db() as connection:

        rows = connection.execute("""
            SELECT
                u.*,
                s.student_code
                    AS closest_student_code,
                s.name
                    AS closest_student_name
            FROM unknown_events u
            LEFT JOIN students s
              ON s.id =
                 u.closest_student_id
            ORDER BY u.id DESC
            LIMIT 200
        """).fetchall()

    return jsonify([
        dict(row)
        for row in rows
    ])


# ============================================================
# ATTENDANCE EXPORT
# ============================================================

def get_attendance(
    date_value: str
) -> list[dict]:

    with db() as connection:

        rows = connection.execute("""
            SELECT
                a.id,
                a.attendance_date,
                a.first_seen_time,
                a.first_seen_timestamp,
                a.pir_sensor,
                a.similarity,
                a.captured_image,

                s.student_code,
                s.name,
                s.roll_number,
                s.class_name

            FROM attendance a

            JOIN students s
              ON s.id = a.student_id

            WHERE
                a.attendance_date = ?

            ORDER BY
                a.first_seen_time ASC
        """, (
            date_value,
        )).fetchall()

    return [
        dict(row)
        for row in rows
    ]


@app.get("/attendance.csv")
def attendance_csv():

    date_value = request.args.get(
        "date",
        datetime.now().strftime(
            "%Y-%m-%d"
        )
    )

    rows = get_attendance(
        date_value
    )

    output = io.StringIO()

    writer = csv.writer(
        output
    )

    writer.writerow([
        "Student ID",
        "Name",
        "Roll Number",
        "Class",
        "Date",
        "First Seen Time",
        "PIR",
        "Similarity"
    ])

    for row in rows:
        writer.writerow([
            row["student_code"],
            row["name"],
            row["roll_number"],
            row["class_name"],
            row["attendance_date"],
            row["first_seen_time"],
            row["pir_sensor"],
            row["similarity"]
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
            (
                "attachment; "
                f"filename=attendance_"
                f"{date_value}.csv"
            )
        }
    )


# ============================================================
# IMAGES
# ============================================================

@app.get(
    "/images/original/<path:filename>"
)
def event_original(filename):
    return send_from_directory(
        ORIGINAL_DIR,
        filename
    )


@app.get(
    "/images/annotated/<path:filename>"
)
def event_annotated(filename):
    return send_from_directory(
        ANNOTATED_DIR,
        filename
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/")
def dashboard():

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    attendance_rows = get_attendance(
        today
    )

    with db() as connection:

        unknown_rows = connection.execute("""
            SELECT
                u.*,
                s.student_code
                    AS closest_student_code,
                s.name
                    AS closest_student_name
            FROM unknown_events u
            LEFT JOIN students s
              ON s.id =
                 u.closest_student_id
            ORDER BY u.id DESC
            LIMIT 30
        """).fetchall()

        event_rows = connection.execute("""
            SELECT *
            FROM events
            ORDER BY id DESC
            LIMIT 50
        """).fetchall()

    attendance_html = "".join(
        f"""
        <tr>
          <td>{row["student_code"]}</td>
          <td>{row["name"]}</td>
          <td>{row["roll_number"] or ""}</td>
          <td>{row["class_name"] or ""}</td>
          <td>{row["first_seen_time"]}</td>
          <td>{row["pir_sensor"]}</td>
          <td>{row["similarity"]:.3f}</td>
          <td>
            <a
              href="/images/original/{row["captured_image"]}"
              target="_blank">
              View
            </a>
          </td>
        </tr>
        """
        for row in attendance_rows
    )

    unknown_html = "".join(
        f"""
        <tr>
          <td>{row["event_timestamp"]}</td>
          <td>{row["pir_sensor"]}</td>
          <td>{
            "-"
            if row["best_similarity"] is None
            else f'{row["best_similarity"]:.3f}'
          }</td>
          <td>{
            row["closest_student_name"]
            or "-"
          }</td>
          <td>
            <a
              href="/images/annotated/{row["captured_image"]}"
              target="_blank">
              View alert
            </a>
          </td>
        </tr>
        """
        for row in unknown_rows
    )

    events_html = "".join(
        f"""
        <tr>
          <td>{row["id"]}</td>
          <td>{row["event_timestamp"]}</td>
          <td>{row["pir_sensor"]}</td>
          <td>{row["known_face_count"]}</td>
          <td>{row["unknown_face_count"]}</td>
          <td>{row["unverified_face_count"]}</td>
          <td>{
            "ALERT"
            if row["buzzer_triggered"]
            else "-"
          }</td>
          <td>
            <a
              href="/images/annotated/{row["annotated_image"]}"
              target="_blank">
              Result
            </a>
          </td>
        </tr>
        """
        for row in event_rows
    )

    return f"""
<!doctype html>
<html>
<head>
<meta
  name="viewport"
  content="width=device-width,initial-scale=1">

<title>Surakshya Cam Attendance</title>

<style>
body {{
  margin:0;
  background:#f3f5f7;
  font-family:Arial,sans-serif;
  color:#111;
}}

header {{
  background:#111;
  color:white;
  padding:20px 28px;
}}

header h1 {{
  margin:0 0 4px 0;
}}

nav a {{
  color:white;
  margin-right:18px;
}}

main {{
  padding:24px;
}}

.cards {{
  display:grid;
  grid-template-columns:
    repeat(auto-fit,minmax(200px,1fr));
  gap:14px;
  margin-bottom:22px;
}}

.card {{
  background:white;
  padding:18px;
  border-radius:8px;
}}

.big {{
  font-size:30px;
  font-weight:bold;
}}

section {{
  background:white;
  padding:18px;
  margin-bottom:22px;
  overflow:auto;
}}

table {{
  width:100%;
  border-collapse:collapse;
}}

th,td {{
  padding:9px;
  border-bottom:1px solid #ddd;
  text-align:left;
  white-space:nowrap;
}}

th {{
  background:#f7f7f7;
}}

.alert {{
  color:#b00020;
  font-weight:bold;
}}

small {{
  color:#666;
}}
</style>
</head>

<body>

<header>
<h1>Surakshya Cam</h1>
<div>
Attendance + Known/Unknown Face Recognition
</div>
<nav>
<a href="/">Dashboard</a>
<a href="/students/enroll">Enroll Student</a>
<a href="/attendance.csv?date={today}">
Download Today's CSV
</a>
</nav>
</header>

<main>

<div class="cards">

<div class="card">
<div>Present Today</div>
<div class="big">
{len(attendance_rows)}
</div>
</div>

<div class="card">
<div>Recent Unknown Alerts</div>
<div class="big">
{len(unknown_rows)}
</div>
</div>

<div class="card">
<div>Recognition Threshold</div>
<div class="big">
{RECOGNITION_THRESHOLD:.2f}
</div>
<small>
Tune after validation.
</small>
</div>

</div>


<section>

<h2>Today's Attendance - {today}</h2>

<table>
<thead>
<tr>
<th>Student ID</th>
<th>Name</th>
<th>Roll</th>
<th>Class</th>
<th>First Seen</th>
<th>PIR</th>
<th>Similarity</th>
<th>Image</th>
</tr>
</thead>

<tbody>
{attendance_html}
</tbody>
</table>

</section>


<section>

<h2 class="alert">
Unknown Person Alerts
</h2>

<table>
<thead>
<tr>
<th>Time</th>
<th>PIR</th>
<th>Best Similarity</th>
<th>Closest Enrolled Student</th>
<th>Image</th>
</tr>
</thead>

<tbody>
{unknown_html}
</tbody>
</table>

</section>


<section>

<h2>Recent Detection Events</h2>

<table>
<thead>
<tr>
<th>Event</th>
<th>RTC Time</th>
<th>PIR</th>
<th>Known</th>
<th>Unknown</th>
<th>Unverified</th>
<th>Buzzer</th>
<th>Annotated</th>
</tr>
</thead>

<tbody>
{events_html}
</tbody>
</table>

</section>

</main>

</body>
</html>
"""


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    init_database()
    reload_embedding_cache()

    print()
    print("=" * 72)
    print("SURAKSHYA CAM ATTENDANCE SERVER V2")
    print("=" * 72)
    print(
        f"YOLO model: {YOLO_MODEL_PATH}"
    )
    print(
        f"Recognition: {INSIGHTFACE_MODEL_PACK}"
    )
    print(
        f"Database: {DATABASE_PATH}"
    )
    print(
        f"Recognition threshold: "
        f"{RECOGNITION_THRESHOLD}"
    )
    print(
        f"Ambiguity margin: "
        f"{AMBIGUITY_MARGIN}"
    )
    print()
    print(
        "Dashboard: "
        "http://127.0.0.1:5000"
    )
    print(
        "Enrollment: "
        "http://127.0.0.1:5000/students/enroll"
    )
    print(
        "Health: "
        "http://127.0.0.1:5000/health"
    )
    print("=" * 72)
    print()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True
    )
