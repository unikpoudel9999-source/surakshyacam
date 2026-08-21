 #include <Arduino.h>
#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include "esp_http_server.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"

// =====================================================
// WIFI - USE YOUR VERIFIED WORKING WIFI
// =====================================================

const char* WIFI_SSID = "xxx";
const char* WIFI_PASSWORD = "xxxxxxxx";

// =====================================================
// LAPTOP DATABASE SERVER
// =====================================================
// CHANGE THIS to the IPv4 address of the laptop running
// surakshya_server.py.
//
// Example:
// const char* DATABASE_SERVER_URL =
//   "http://192.168.1.20:5000/upload";

const char* DATABASE_SERVER_URL =
  "http://192.168.137.133/upload";


// =====================================================
// AI THINKER ESP32-CAM PINS
// =====================================================

#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27

#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5

#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// =====================================================
// ARDUINO UNO SERIAL
// =====================================================

// UNO D1/TX -> voltage divider -> GPIO13

#define UNO_RX_PIN 13

HardwareSerial UnoSerial(2);

// =====================================================
// FLASH
// =====================================================

#define FLASH_PIN 4

// Leave OFF while testing
#define USE_FLASH false

// =====================================================
// UNKNOWN-PERSON BUZZER
// =====================================================
// GPIO14 is free only if you are NOT using the microSD interface.
// Use an ACTIVE buzzer through a transistor/driver stage.
#define BUZZER_PIN 14
#define BUZZER_ALERT_MS 5000
#define BUZZER_ACTIVE_HIGH true

struct UploadJob;

struct UploadJob
{
  uint8_t* jpeg;
  size_t length;
  char sensor[20];
  char timestamp[50];
  unsigned long espEventNumber;
};

volatile unsigned long buzzerOffAt = 0;

void setBuzzer(bool on)
{
  bool level = BUZZER_ACTIVE_HIGH ? on : !on;
  digitalWrite(BUZZER_PIN, level ? HIGH : LOW);
}

void triggerUnknownBuzzer()
{
  setBuzzer(true);
  buzzerOffAt = millis() + BUZZER_ALERT_MS;

  Serial.println(
    "!!! UNKNOWN PERSON - BUZZER ON !!!"
  );
}

void updateBuzzer()
{
  if (
    buzzerOffAt != 0 &&
    (long)(millis() - buzzerOffAt) >= 0
  )
  {
    setBuzzer(false);
    buzzerOffAt = 0;

    Serial.println(
      "BUZZER OFF"
    );
  }
}

// =====================================================
// SERVERS
// =====================================================

httpd_handle_t webServer = NULL;
httpd_handle_t streamServer = NULL;

// =====================================================
// MUTEX
// =====================================================

SemaphoreHandle_t cameraMutex;
SemaphoreHandle_t photoMutex;
SemaphoreHandle_t triggerMutex;

// =====================================================
// CAPTURED PHOTO
// =====================================================

uint8_t* latestPhoto = NULL;
size_t latestPhotoLength = 0;

unsigned long photoCount = 0;

char lastSensor[20] = "None";
char lastTime[50] = "No detection yet";

// =====================================================
// CAPTURE REQUEST
// =====================================================

bool captureRequested = false;

char requestedSensor[20] = "";
char requestedTime[50] = "";

volatile bool streamClientActive = false;

// =====================================================
// DATABASE UPLOAD QUEUE
// =====================================================
// Every saved PIR JPEG is copied into its own upload job.
// The background task sends that copy to the laptop server,
// so the camera can continue streaming/capturing.

QueueHandle_t uploadQueue = NULL;


bool allocateUploadJpeg(
  UploadJob& job,
  const uint8_t* source,
  size_t length
)
{
  job.jpeg = NULL;

  if (psramFound())
  {
    job.jpeg =
      (uint8_t*)heap_caps_malloc(
        length,
        MALLOC_CAP_SPIRAM |
        MALLOC_CAP_8BIT
      );
  }

  if (job.jpeg == NULL)
  {
    job.jpeg =
      (uint8_t*)malloc(length);
  }

  if (job.jpeg == NULL)
  {
    return false;
  }

  memcpy(
    job.jpeg,
    source,
    length
  );

  job.length = length;

  return true;
}


bool queueDatabaseUpload(
  const uint8_t* jpeg,
  size_t jpegLength,
  const char* sensor,
  const char* timestamp,
  unsigned long espEventNumber
)
{
  if (uploadQueue == NULL)
  {
    Serial.println(
      "DB ERROR: UPLOAD QUEUE NOT READY"
    );

    return false;
  }

  UploadJob job = {};

  if (
    !allocateUploadJpeg(
      job,
      jpeg,
      jpegLength
    )
  )
  {
    Serial.println(
      "DB ERROR: COULD NOT COPY JPEG"
    );

    return false;
  }

  strncpy(
    job.sensor,
    sensor,
    sizeof(job.sensor) - 1
  );

  job.sensor[
    sizeof(job.sensor) - 1
  ] = '\0';

  strncpy(
    job.timestamp,
    timestamp,
    sizeof(job.timestamp) - 1
  );

  job.timestamp[
    sizeof(job.timestamp) - 1
  ] = '\0';

  job.espEventNumber =
    espEventNumber;

  if (
    xQueueSend(
      uploadQueue,
      &job,
      0
    ) != pdTRUE
  )
  {
    Serial.println(
      "DB ERROR: UPLOAD QUEUE FULL"
    );

    free(job.jpeg);

    return false;
  }

  Serial.print(
    "DATABASE UPLOAD QUEUED #"
  );

  Serial.println(
    espEventNumber
  );

  return true;
}


void databaseUploaderTask(
  void* parameter
)
{
  UploadJob job;

  while (true)
  {
    if (
      xQueueReceive(
        uploadQueue,
        &job,
        portMAX_DELAY
      ) != pdTRUE
    )
    {
      continue;
    }

    if (
      WiFi.status() !=
      WL_CONNECTED
    )
    {
      Serial.println(
        "DB UPLOAD FAILED: WIFI DISCONNECTED"
      );

      free(job.jpeg);

      continue;
    }

    WiFiClient client;
    HTTPClient http;

    http.setConnectTimeout(5000);
    http.setTimeout(20000);

    Serial.print(
      "Uploading database event #"
    );

    Serial.println(
      job.espEventNumber
    );

    if (
      !http.begin(
        client,
        DATABASE_SERVER_URL
      )
    )
    {
      Serial.println(
        "DB UPLOAD FAILED: HTTP BEGIN"
      );

      free(job.jpeg);

      continue;
    }

    http.addHeader(
      "Content-Type",
      "image/jpeg"
    );

    http.addHeader(
      "X-Sensor",
      String(job.sensor)
    );

    http.addHeader(
      "X-Timestamp",
      String(job.timestamp)
    );

    http.addHeader(
      "X-ESP-Event",
      String(job.espEventNumber)
    );

    int httpCode =
      http.POST(
        job.jpeg,
        job.length
      );

    if (
      httpCode >= 200 &&
      httpCode < 300
    )
    {
      Serial.print(
        "DATABASE UPLOAD OK. HTTP "
      );

      Serial.println(
        httpCode
      );

      String serverResponse =
        http.getString();

      Serial.println(
        serverResponse
      );

      // The Python recognition server returns:
      // "buzzer":true
      // whenever at least one valid face is classified UNKNOWN.
      bool unknownAlert =
        (
          serverResponse.indexOf(
            "\"buzzer\":true"
          ) >= 0
        ) ||
        (
          serverResponse.indexOf(
            "\"buzzer\": true"
          ) >= 0
        );

      if (unknownAlert)
      {
        triggerUnknownBuzzer();
      }
    }
    else
    {
      Serial.print(
        "DATABASE UPLOAD FAILED. HTTP "
      );

      Serial.println(
        httpCode
      );

      if (httpCode > 0)
      {
        Serial.println(
          http.getString()
        );
      }
    }

    http.end();

    free(job.jpeg);

    vTaskDelay(
      pdMS_TO_TICKS(20)
    );
  }
}


// =====================================================
// STREAM
// =====================================================

#define PART_BOUNDARY "123456789000000000000987654321"

static const char* STREAM_CONTENT_TYPE =
  "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;

static const char* STREAM_BOUNDARY =
  "\r\n--" PART_BOUNDARY "\r\n";

static const char* STREAM_PART =
  "Content-Type: image/jpeg\r\n"
  "Content-Length: %u\r\n\r\n";

// =====================================================
// WEB PAGE
// =====================================================

static const char WEBPAGE[] PROGMEM = R"HTML(
<!DOCTYPE html>

<html>

<head>

<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>Surakshya Cam</title>

<style>

body {
  background:#111;
  color:white;
  font-family:Arial,sans-serif;
  text-align:center;
  margin:0;
  padding:15px;
}

.container {
  max-width:800px;
  margin:auto;
}

.status {
  background:#222;
  padding:15px;
  border-radius:10px;
  margin-bottom:20px;
}

img {
  width:100%;
  max-width:640px;
  border-radius:8px;
}

.section {
  margin-top:25px;
}

button {
  padding:12px 20px;
  font-size:16px;
  margin:10px;
}

</style>

</head>

<body>

<div class="container">

<h1>Surakshya Cam</h1>

<div class="status">

<p>
Last Detection:
<strong id="sensor">None</strong>
</p>

<p>
Detection Time:
<strong id="time">No detection yet</strong>
</p>

<p>
Captured Photos:
<strong id="count">0</strong>
</p>

</div>


<div class="section">

<h2>Live Video</h2>

<img id="stream">

</div>


<div class="section">

<h2>Latest Captured Photo</h2>

<img
id="capture"
style="display:none">

<br>

<button onclick="manualCapture()">
TEST CAPTURE
</button>

<button onclick="refreshCapture()">
REFRESH PHOTO
</button>

</div>

</div>


<script>

const host =
  window.location.hostname;

document.getElementById("stream").src =
  "http://" + host + ":81/stream";

let previousCount = -1;


const refreshCapture = () =>
{
  const img =
    document.getElementById("capture");

  img.style.display =
    "block";

  img.src =
    "/capture.jpg?t=" +
    Date.now();
}


const manualCapture = () =>
{
  fetch("/testcapture?t=" + Date.now())
  .then(() => {

    setTimeout(
      refreshCapture,
      800
    );

  });
}


const updateStatus = () =>
{
  fetch(
    "/status?t=" + Date.now(),
    {cache:"no-store"}
  )

  .then((response) => {
    return response.json();
  })

  .then((data) => {

    document.getElementById(
      "sensor"
    ).innerText =
      data.sensor;

    document.getElementById(
      "time"
    ).innerText =
      data.time;

    document.getElementById(
      "count"
    ).innerText =
      data.count;


    if (
      data.count > 0 &&
      data.count != previousCount
    )
    {
      refreshCapture();
    }

    previousCount =
      data.count;
  });
}


setInterval(
  updateStatus,
  1000
);

updateStatus();

</script>

</body>

</html>
)HTML";

// =====================================================
// CAMERA INITIALIZATION
// =====================================================

bool initializeCamera()
{
  camera_config_t config = {};

  config.ledc_channel =
    LEDC_CHANNEL_0;

  config.ledc_timer =
    LEDC_TIMER_0;

  config.pin_d0 =
    Y2_GPIO_NUM;

  config.pin_d1 =
    Y3_GPIO_NUM;

  config.pin_d2 =
    Y4_GPIO_NUM;

  config.pin_d3 =
    Y5_GPIO_NUM;

  config.pin_d4 =
    Y6_GPIO_NUM;

  config.pin_d5 =
    Y7_GPIO_NUM;

  config.pin_d6 =
    Y8_GPIO_NUM;

  config.pin_d7 =
    Y9_GPIO_NUM;

  config.pin_xclk =
    XCLK_GPIO_NUM;

  config.pin_pclk =
    PCLK_GPIO_NUM;

  config.pin_vsync =
    VSYNC_GPIO_NUM;

  config.pin_href =
    HREF_GPIO_NUM;

  config.pin_sccb_sda =
    SIOD_GPIO_NUM;

  config.pin_sccb_scl =
    SIOC_GPIO_NUM;

  config.pin_pwdn =
    PWDN_GPIO_NUM;

  config.pin_reset =
    RESET_GPIO_NUM;

  config.xclk_freq_hz =
    20000000;

  config.pixel_format =
    PIXFORMAT_JPEG;

  // Stable live stream
  config.frame_size =
    FRAMESIZE_QVGA;

  config.jpeg_quality =
    12;

  if (psramFound())
  {
    Serial.println("PSRAM FOUND");

    config.fb_count = 2;

    config.fb_location =
      CAMERA_FB_IN_PSRAM;

    config.grab_mode =
      CAMERA_GRAB_LATEST;
  }
  else
  {
    Serial.println("NO PSRAM");

    config.fb_count = 1;

    config.fb_location =
      CAMERA_FB_IN_DRAM;

    config.grab_mode =
      CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err =
    esp_camera_init(&config);

  if (err != ESP_OK)
  {
    Serial.printf(
      "CAMERA ERROR: 0x%x\n",
      err
    );

    return false;
  }

  Serial.println(
    "CAMERA READY"
  );

  return true;
}

// =====================================================
// REQUEST A PHOTO
// =====================================================

void requestCapture(
  const char* sensor,
  const char* timestamp
)
{
  xSemaphoreTake(
    triggerMutex,
    portMAX_DELAY
  );

  strncpy(
    requestedSensor,
    sensor,
    sizeof(requestedSensor) - 1
  );

  requestedSensor[
    sizeof(requestedSensor) - 1
  ] = '\0';

  strncpy(
    requestedTime,
    timestamp,
    sizeof(requestedTime) - 1
  );

  requestedTime[
    sizeof(requestedTime) - 1
  ] = '\0';

  captureRequested =
    true;

  xSemaphoreGive(
    triggerMutex
  );

  Serial.println();
  Serial.println(
    ">>> PHOTO REQUESTED <<<"
  );
}

// =====================================================
// CHECK REQUEST
// =====================================================

bool hasCaptureRequest()
{
  bool requested;

  xSemaphoreTake(
    triggerMutex,
    portMAX_DELAY
  );

  requested =
    captureRequested;

  xSemaphoreGive(
    triggerMutex
  );

  return requested;
}

// =====================================================
// SAVE CAMERA FRAME
// =====================================================

bool saveFrame(camera_fb_t* fb)
{
  if (fb == NULL)
  {
    return false;
  }

  char sensor[20];
  char timestamp[50];

  // Get trigger information

  xSemaphoreTake(
    triggerMutex,
    portMAX_DELAY
  );

  if (!captureRequested)
  {
    xSemaphoreGive(
      triggerMutex
    );

    return false;
  }

  strncpy(
    sensor,
    requestedSensor,
    sizeof(sensor) - 1
  );

  sensor[
    sizeof(sensor) - 1
  ] = '\0';

  strncpy(
    timestamp,
    requestedTime,
    sizeof(timestamp) - 1
  );

  timestamp[
    sizeof(timestamp) - 1
  ] = '\0';

  captureRequested =
    false;

  xSemaphoreGive(
    triggerMutex
  );

  // Allocate image memory

  uint8_t* buffer =
    NULL;

  if (psramFound())
  {
    buffer =
      (uint8_t*)heap_caps_malloc(
        fb->len,
        MALLOC_CAP_SPIRAM |
        MALLOC_CAP_8BIT
      );
  }

  if (buffer == NULL)
  {
    buffer =
      (uint8_t*)malloc(
        fb->len
      );
  }

  if (buffer == NULL)
  {
    Serial.println(
      "PHOTO MEMORY ERROR"
    );

    return false;
  }

  memcpy(
    buffer,
    fb->buf,
    fb->len
  );

  // Replace previous image

  xSemaphoreTake(
    photoMutex,
    portMAX_DELAY
  );

  if (latestPhoto != NULL)
  {
    free(latestPhoto);
  }

  latestPhoto =
    buffer;

  latestPhotoLength =
    fb->len;

  strncpy(
    lastSensor,
    sensor,
    sizeof(lastSensor) - 1
  );

  lastSensor[
    sizeof(lastSensor) - 1
  ] = '\0';

  strncpy(
    lastTime,
    timestamp,
    sizeof(lastTime) - 1
  );

  lastTime[
    sizeof(lastTime) - 1
  ] = '\0';

  photoCount++;

  unsigned long number =
    photoCount;

  // Make an independent JPEG copy for the database uploader
  // while latestPhoto is protected by photoMutex.
  queueDatabaseUpload(
    latestPhoto,
    latestPhotoLength,
    sensor,
    timestamp,
    number
  );

  xSemaphoreGive(
    photoMutex
  );

  Serial.println();
  Serial.println(
    "=============================="
  );

  Serial.print(
    "PHOTO SAVED #"
  );

  Serial.println(number);

  Serial.print(
    "Sensor: "
  );

  Serial.println(sensor);

  Serial.print(
    "Time: "
  );

  Serial.println(timestamp);

  Serial.print(
    "JPEG bytes: "
  );

  Serial.println(
    latestPhotoLength
  );

  Serial.println(
    "=============================="
  );

  return true;
}

// =====================================================
// STANDALONE CAPTURE
// =====================================================

void performStandaloneCapture()
{
  if (!hasCaptureRequest())
  {
    return;
  }

  if (streamClientActive)
  {
    // Let the live stream save its next frame.
    return;
  }

  xSemaphoreTake(
    cameraMutex,
    portMAX_DELAY
  );

  if (USE_FLASH)
  {
    digitalWrite(
      FLASH_PIN,
      HIGH
    );

    delay(100);
  }

  camera_fb_t* fb =
    esp_camera_fb_get();

  if (USE_FLASH)
  {
    digitalWrite(
      FLASH_PIN,
      LOW
    );
  }

  if (fb != NULL)
  {
    saveFrame(fb);

    esp_camera_fb_return(fb);
  }
  else
  {
    Serial.println(
      "STANDALONE CAPTURE FAILED"
    );
  }

  xSemaphoreGive(
    cameraMutex
  );
}

// =====================================================
// ROOT PAGE
// =====================================================

esp_err_t rootHandler(
  httpd_req_t* req
)
{
  httpd_resp_set_type(
    req,
    "text/html"
  );

  return httpd_resp_send(
    req,
    WEBPAGE,
    HTTPD_RESP_USE_STRLEN
  );
}

// =====================================================
// STATUS
// =====================================================

esp_err_t statusHandler(
  httpd_req_t* req
)
{
  char sensor[20];
  char timestamp[50];

  unsigned long count;

  xSemaphoreTake(
    photoMutex,
    portMAX_DELAY
  );

  strncpy(
    sensor,
    lastSensor,
    sizeof(sensor) - 1
  );

  sensor[
    sizeof(sensor) - 1
  ] = '\0';

  strncpy(
    timestamp,
    lastTime,
    sizeof(timestamp) - 1
  );

  timestamp[
    sizeof(timestamp) - 1
  ] = '\0';

  count =
    photoCount;

  xSemaphoreGive(
    photoMutex
  );

  char json[200];

  snprintf(
    json,
    sizeof(json),
    "{\"sensor\":\"%s\","
    "\"time\":\"%s\","
    "\"count\":%lu}",
    sensor,
    timestamp,
    count
  );

  httpd_resp_set_type(
    req,
    "application/json"
  );

  httpd_resp_set_hdr(
    req,
    "Cache-Control",
    "no-store"
  );

  return httpd_resp_send(
    req,
    json,
    HTTPD_RESP_USE_STRLEN
  );
}

// =====================================================
// CAPTURED JPEG
// =====================================================

esp_err_t photoHandler(
  httpd_req_t* req
)
{
  xSemaphoreTake(
    photoMutex,
    portMAX_DELAY
  );

  if (
    latestPhoto == NULL ||
    latestPhotoLength == 0
  )
  {
    xSemaphoreGive(
      photoMutex
    );

    httpd_resp_set_status(
      req,
      "404 Not Found"
    );

    return httpd_resp_send(
      req,
      "No captured photo yet",
      HTTPD_RESP_USE_STRLEN
    );
  }

  httpd_resp_set_type(
    req,
    "image/jpeg"
  );

  httpd_resp_set_hdr(
    req,
    "Cache-Control",
    "no-store, no-cache"
  );

  esp_err_t result =
    httpd_resp_send(
      req,
      (const char*)latestPhoto,
      latestPhotoLength
    );

  xSemaphoreGive(
    photoMutex
  );

  return result;
}

// =====================================================
// MANUAL TEST CAPTURE
// =====================================================

esp_err_t testCaptureHandler(
  httpd_req_t* req
)
{
  requestCapture(
    "MANUAL TEST",
    "WEB BUTTON"
  );

  httpd_resp_set_type(
    req,
    "text/plain"
  );

  return httpd_resp_send(
    req,
    "Capture requested",
    HTTPD_RESP_USE_STRLEN
  );
}

// =====================================================
// LIVE STREAM
// =====================================================

esp_err_t streamHandler(
  httpd_req_t* req
)
{
  streamClientActive =
    true;

  esp_err_t result =
    httpd_resp_set_type(
      req,
      STREAM_CONTENT_TYPE
    );

  if (result != ESP_OK)
  {
    streamClientActive =
      false;

    return result;
  }

  char partBuffer[64];

  while (true)
  {
    xSemaphoreTake(
      cameraMutex,
      portMAX_DELAY
    );

    camera_fb_t* fb =
      esp_camera_fb_get();

    if (fb == NULL)
    {
      xSemaphoreGive(
        cameraMutex
      );

      break;
    }

    // =================================================
    // PIR TRIGGER:
    // SAVE THIS EXACT LIVE FRAME
    // =================================================

    if (hasCaptureRequest())
    {
      saveFrame(fb);
    }

    result =
      httpd_resp_send_chunk(
        req,
        STREAM_BOUNDARY,
        strlen(STREAM_BOUNDARY)
      );

    if (result == ESP_OK)
    {
      int length =
        snprintf(
          partBuffer,
          sizeof(partBuffer),
          STREAM_PART,
          fb->len
        );

      result =
        httpd_resp_send_chunk(
          req,
          partBuffer,
          length
        );
    }

    if (result == ESP_OK)
    {
      result =
        httpd_resp_send_chunk(
          req,
          (const char*)fb->buf,
          fb->len
        );
    }

    esp_camera_fb_return(
      fb
    );

    xSemaphoreGive(
      cameraMutex
    );

    if (result != ESP_OK)
    {
      break;
    }

    delay(15);
  }

  streamClientActive =
    false;

  return result;
}

// =====================================================
// START SERVERS
// =====================================================

void startServers()
{
  // PORT 80

  httpd_config_t webConfig =
    HTTPD_DEFAULT_CONFIG();

  webConfig.server_port =
    80;

  webConfig.max_uri_handlers =
    10;

  if (
    httpd_start(
      &webServer,
      &webConfig
    ) == ESP_OK
  )
  {
    httpd_uri_t root = {};

    root.uri = "/";
    root.method = HTTP_GET;
    root.handler = rootHandler;

    httpd_register_uri_handler(
      webServer,
      &root
    );


    httpd_uri_t status = {};

    status.uri = "/status";
    status.method = HTTP_GET;
    status.handler = statusHandler;

    httpd_register_uri_handler(
      webServer,
      &status
    );


    httpd_uri_t photo = {};

    photo.uri = "/capture.jpg";
    photo.method = HTTP_GET;
    photo.handler = photoHandler;

    httpd_register_uri_handler(
      webServer,
      &photo
    );


    httpd_uri_t test = {};

    test.uri = "/testcapture";
    test.method = HTTP_GET;
    test.handler = testCaptureHandler;

    httpd_register_uri_handler(
      webServer,
      &test
    );

    Serial.println(
      "WEB SERVER READY"
    );
  }


  // PORT 81

  httpd_config_t streamConfig =
    HTTPD_DEFAULT_CONFIG();

  streamConfig.server_port =
    81;

  streamConfig.ctrl_port =
    webConfig.ctrl_port + 1;

  if (
    httpd_start(
      &streamServer,
      &streamConfig
    ) == ESP_OK
  )
  {
    httpd_uri_t stream = {};

    stream.uri = "/stream";
    stream.method = HTTP_GET;
    stream.handler = streamHandler;

    httpd_register_uri_handler(
      streamServer,
      &stream
    );

    Serial.println(
      "STREAM SERVER READY"
    );
  }
}

// =====================================================
// UNO MESSAGE
// =====================================================

void processUnoMessage(
  String message
)
{
  message.trim();

  if (message.length() == 0)
  {
    return;
  }

  Serial.print(
    "UNO -> ESP32: "
  );

  Serial.println(message);

  if (
    message.startsWith(
      "PIR1_DETECTED"
    )
  )
  {
    int comma =
      message.indexOf(',');

    String timestamp =
      "Unknown";

    if (comma >= 0)
    {
      timestamp =
        message.substring(
          comma + 1
        );
    }

    requestCapture(
      "PIR 1",
      timestamp.c_str()
    );
  }

  else if (
    message.startsWith(
      "PIR2_DETECTED"
    )
  )
  {
    int comma =
      message.indexOf(',');

    String timestamp =
      "Unknown";

    if (comma >= 0)
    {
      timestamp =
        message.substring(
          comma + 1
        );
    }

    requestCapture(
      "PIR 2",
      timestamp.c_str()
    );
  }
}

// =====================================================
// WIFI
// =====================================================

void connectWiFi()
{
  WiFi.mode(
    WIFI_STA
  );

  WiFi.setSleep(false);

  WiFi.begin(
    WIFI_SSID,
    WIFI_PASSWORD
  );

  Serial.print(
    "Connecting WiFi"
  );

  while (
    WiFi.status() != WL_CONNECTED
  )
  {
    delay(500);
    Serial.print(".");
  }

  Serial.println();

  Serial.println(
    "WIFI CONNECTED"
  );

  Serial.print(
    "ESP IP: "
  );

  Serial.println(
    WiFi.localIP()
  );
}

// =====================================================
// SETUP
// =====================================================

void setup()
{
  Serial.begin(
    115200
  );

  delay(1000);

  pinMode(
    FLASH_PIN,
    OUTPUT
  );

  digitalWrite(
    FLASH_PIN,
    LOW
  );

  pinMode(
    BUZZER_PIN,
    OUTPUT
  );

  setBuzzer(false);

  cameraMutex =
    xSemaphoreCreateMutex();

  photoMutex =
    xSemaphoreCreateMutex();

  triggerMutex =
    xSemaphoreCreateMutex();

  // Database uploads are processed in the background.
  uploadQueue =
    xQueueCreate(
      6,
      sizeof(UploadJob)
    );

  if (uploadQueue == NULL)
  {
    Serial.println(
      "DB ERROR: COULD NOT CREATE UPLOAD QUEUE"
    );

    while (true)
    {
      delay(1000);
    }
  }

  BaseType_t uploadTaskResult =
    xTaskCreatePinnedToCore(
      databaseUploaderTask,
      "DatabaseUploader",
      8192,
      NULL,
      1,
      NULL,
      0
    );

  if (uploadTaskResult != pdPASS)
  {
    Serial.println(
      "DB ERROR: COULD NOT START UPLOAD TASK"
    );

    while (true)
    {
      delay(1000);
    }
  }

  // Arduino Uno RX
  UnoSerial.begin(
    9600,
    SERIAL_8N1,
    UNO_RX_PIN,
    -1
  );

  Serial.println(
    "UNO RX GPIO13 READY"
  );

  if (!initializeCamera())
  {
    while (true)
    {
      delay(1000);
    }
  }

  connectWiFi();

  startServers();

  Serial.println();

  Serial.print(
    "MAIN PAGE: http://"
  );

  Serial.println(
    WiFi.localIP()
  );

  Serial.print(
    "LIVE: http://"
  );

  Serial.print(
    WiFi.localIP()
  );

  Serial.println(
    ":81/stream"
  );

  Serial.print(
    "PHOTO: http://"
  );

  Serial.print(
    WiFi.localIP()
  );

  Serial.println(
    "/capture.jpg"
  );
}

// =====================================================
// LOOP
// =====================================================

void loop()
{
  static String incoming = "";

  updateBuzzer();

  // Receive Arduino messages

  while (
    UnoSerial.available()
  )
  {
    char c =
      UnoSerial.read();

    if (c == '\n')
    {
      if (
        incoming.length() > 0
      )
      {
        processUnoMessage(
          incoming
        );

        incoming = "";
      }
    }

    else if (
      c != '\r'
    )
    {
      incoming += c;

      if (
        incoming.length() > 100
      )
      {
        incoming = "";
      }
    }
  }

  // If no live browser is connected,
  // take the PIR photo here.

  if (
    hasCaptureRequest() &&
    !streamClientActive
  )
  {
    performStandaloneCapture();
  }

  delay(5);
}
