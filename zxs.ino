#include <Wire.h>
#include <RTClib.h>

// ===============================
// PIN DEFINITIONS
// ===============================

#define PIR1_PIN 3
#define LED_PIN LED_BUILTIN

RTC_DS3231 rtc;

bool pir1Previous = LOW;

unsigned long lastStatusPrint = 0;


// ===============================
// PRINT DATE AND TIME
// ===============================

void printDateTime(DateTime now)
{
  Serial.print(now.year());
  Serial.print("-");
  if (now.month() < 10) Serial.print("0");
  Serial.print(now.month());
  Serial.print("-");
  if (now.day() < 10) Serial.print("0");
  Serial.print(now.day());
  Serial.print(",");
  if (now.hour() < 10) Serial.print("0");
  Serial.print(now.hour());
  Serial.print(":");
  if (now.minute() < 10) Serial.print("0");
  Serial.print(now.minute());
  Serial.print(":");
  if (now.second() < 10) Serial.print("0");
  Serial.print(now.second());
}


// ===============================
// SETUP
// ===============================

void setup()
{
  pinMode(PIR1_PIN, INPUT);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.begin(9600);
  Wire.begin();

  Serial.println("Starting system...");

  if (!rtc.begin())
  {
    Serial.println("RTC_NOT_FOUND");
    while (1)
    {
      digitalWrite(LED_PIN, HIGH);
      delay(200);
      digitalWrite(LED_PIN, LOW);
      delay(200);
    }
  }

  if (rtc.lostPower())
  {
    Serial.println("RTC lost power.");
    rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
    Serial.println("RTC time updated.");
  }

  Serial.println("RTC_OK");
  Serial.println("Waiting for PIR sensor...");

  delay(3000);

  pir1Previous = digitalRead(PIR1_PIN);

  Serial.print("Initial state -> PIR1: ");
  Serial.println(pir1Previous);

  Serial.println("READY. Watching for motion...");
}


// ===============================
// MAIN LOOP
// ===============================

void loop()
{
  bool pir1State = digitalRead(PIR1_PIN);

  // ===============================
  // PIR SENSOR 1 - DETECTION
  // ===============================
  if (pir1State == HIGH && pir1Previous == LOW)
  {
    digitalWrite(LED_PIN, HIGH);
    DateTime now = rtc.now();
    Serial.print("PIR1_DETECTED,");
    printDateTime(now);
    Serial.println();
    delay(100);
    digitalWrite(LED_PIN, LOW);
  }

  // ===============================
  // RAW STATUS PRINT (every 1s, for debugging)
  // ===============================
  if (millis() - lastStatusPrint >= 1000)
  {
    lastStatusPrint = millis();
    Serial.print("  [raw] t=");
    Serial.print(millis() / 1000);
    Serial.print("s  PIR1=");
    Serial.println(pir1State);
  }

  pir1Previous = pir1State;

  delay(20);
}
