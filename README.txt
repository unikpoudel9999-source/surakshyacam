SURAKSHYA CAM ATTENDANCE SYSTEM V2
===============================================

THIS VERSION IMPLEMENTS
-----------------------------------------------
PIR -> Arduino Uno -> DS3231 RTC timestamp
    -> ESP32-CAM JPEG
    -> Laptop server
    -> YOLO face detection using best (2).pt
    -> Face recognition using InsightFace
    -> Known student / Unknown person decision
    -> SQLite event history
    -> One attendance record per student per day
    -> Unknown-person event history
    -> ESP32 buzzer command for UNKNOWN persons
    -> Dashboard
    -> Student enrollment page
    -> Attendance CSV export

IMPORTANT MODEL ROLES
-----------------------------------------------
best (2).pt:
    Detects WHERE a face is.

InsightFace:
    Generates a face embedding used to determine WHO the face belongs to.

A YOLO face detector by itself cannot identify a student.

FOLDER CONTENTS
-----------------------------------------------
SurakshyaCam_ESP32_Attendance_V2.ino
surakshya_attendance_server.py
requirements.txt
run_server.bat
best (2).pt

FIRST SERVER SETUP - WINDOWS
-----------------------------------------------
1. Extract this folder.

2. Open Command Prompt inside the folder.

3. Install packages:

   py -m pip install -r requirements.txt

4. Start the server:

   py surakshya_attendance_server.py

   or double-click:

   run_server.bat

5. InsightFace 1.x is used. On first startup it may automatically
   download the buffalo_l recognition model pack.

6. Test:

   http://127.0.0.1:5000/health

7. Open dashboard:

   http://127.0.0.1:5000

STUDENT ENROLLMENT
-----------------------------------------------
Open:

   http://127.0.0.1:5000/students/enroll

Enter:
- Student ID
- Name
- Roll number
- Class
- several reference photos

Use multiple clear photos:
- front
- slight left/right
- slightly different lighting/expression

Each enrollment photo must contain exactly one face.

ESP32 SERVER ADDRESS
-----------------------------------------------
Find laptop IPv4:

   ipconfig

Example laptop IP:

   192.168.1.20

Then edit in the ESP32 sketch:

const char* DATABASE_SERVER_URL =
  "http://192.168.1.20:5000/upload";

Keep the laptop and ESP32-CAM on the same Wi-Fi.

BUZZER
-----------------------------------------------
V2 uses:

   ESP32-CAM GPIO14

for an ACTIVE buzzer alert.

GPIO14 conflicts with the microSD interface, so this configuration assumes
you are not using the ESP32-CAM microSD slot.

Recommended:
GPIO14 -> resistor/base driver -> transistor -> active buzzer

Do not power a higher-current buzzer directly from an ESP32 GPIO.

Default alert duration:

   5 seconds

The server only returns buzzer=true for a face that successfully generated
a recognition embedding but failed the KNOWN-student matching rules.

UNVERIFIED faces (too small/blurry/no embedding) are saved but do not
automatically sound the alarm.

ATTENDANCE RULE
-----------------------------------------------
A KNOWN student is marked once per day.

First detection:
   MARKED_PRESENT

Later detections on same date:
   ALREADY_MARKED_TODAY

The attendance time is taken from the Arduino/DS3231 timestamp, not the
server receive time.

DATABASE
-----------------------------------------------
SQLite file:

surakshya_data/surakshya.db

Main tables:

students
student_embeddings
events
face_detections
attendance
unknown_events

IMAGES
-----------------------------------------------
Original PIR images:

surakshya_data/events/original/

Annotated results:

surakshya_data/events/annotated/

Enrollment photos:

surakshya_data/students/

DASHBOARD
-----------------------------------------------
http://<LAPTOP-IP>:5000/

Shows:
- Today's attendance
- Unknown-person alerts
- Recent events
- Known / Unknown / Unverified counts
- RTC timestamps
- PIR source
- recognition similarity
- annotated images

ATTENDANCE CSV
-----------------------------------------------
Today's CSV:

http://<LAPTOP-IP>:5000/attendance.csv

RECOGNITION THRESHOLD
-----------------------------------------------
The server currently starts with:

RECOGNITION_THRESHOLD = 0.45
AMBIGUITY_MARGIN = 0.03

These are starting values only.

Do NOT claim final accuracy until you test:
- multiple photos of each enrolled student
- different lighting
- different distances
- different angles
- genuinely unknown people

After collecting that validation set, tune the threshold based on the
false-accept and false-reject behavior.

EXPECTED KNOWN EVENT
-----------------------------------------------
ESP32:
PHOTO SAVED #...
DATABASE UPLOAD OK. HTTP 200

Server response includes:
"buzzer": false

Dashboard:
Student -> Present

EXPECTED UNKNOWN EVENT
-----------------------------------------------
Server response includes:
"buzzer": true

ESP32 prints:
!!! UNKNOWN PERSON - BUZZER ON !!!

The unknown photo and RTC timestamp remain in the dashboard/database.

NEXT DEVELOPMENT STAGE
-----------------------------------------------
After V2 works reliably:
1. threshold calibration report
2. unknown-person two-frame confirmation
3. anti-spoof / liveness checks
4. admin authentication
5. attendance entry/exit modes
6. cloud backup
