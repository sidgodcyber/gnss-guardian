// GNSS Guardian safe-action output for a ground stand-in (DEMO ONLY; guidance stays advisory).
// Arduino Uno on the Pi's USB port (/dev/ttyACM0, 115200 baud). Relay on D7, servo flag on D9,
// on-board LED D13 = relay state. Protocol (NMEA-style checksum, XOR between '$' and '*'):
//   Pi  -> Uno   $GG,<seq>,<MODE>,<servo_deg>,<relay 0|1>,<watchdog_ms>*CS
//   Uno -> Pi    $GA,<seq>,<MODE>*CS        acknowledgement of every valid line
//                $GR,uno_safe_action,1*CS   at power-up
// FAIL-SAFE: at power-up and whenever no valid line has arrived for watchdog_ms (default 3000,
// clamped 1000..10000), the Uno goes SAFE: relay released (stand-in motor off), servo at SAFE_DEG.
// A hung Pi, a crashed process or a pulled USB cable all end in SAFE within watchdog_ms.
// Wiring: relay module IN -> D7 (active HIGH), VCC 5V, GND; servo signal -> D9, servo power from a
// separate 5 V supply with a common ground (the Uno's 5 V cannot drive a servo under load).
#include <Servo.h>

const uint8_t RELAY_PIN = 7, SERVO_PIN = 9, LED_PIN = 13;
const int SAFE_DEG = 180;
const unsigned long DEFAULT_WATCHDOG_MS = 3000;

Servo flag;
char buf[96];
uint8_t len = 0;
unsigned long lastValid = 0, watchdogMs = DEFAULT_WATCHDOG_MS;
bool safe = true;

uint8_t xorsum(const char *s, uint8_t n) { uint8_t x = 0; for (uint8_t i = 0; i < n; i++) x ^= s[i]; return x; }

void sendLine(const char *body) {
  char out[64];
  snprintf(out, sizeof(out), "$%s*%02X", body, xorsum(body, strlen(body)));
  Serial.println(out);
}

void goSafe() {
  digitalWrite(RELAY_PIN, LOW);
  digitalWrite(LED_PIN, LOW);
  flag.write(SAFE_DEG);
  safe = true;
}

void handle(char *line) {
  char *star = strrchr(line, '*');
  if (line[0] != '$' || !star) return;
  uint8_t n = star - line - 1;
  if (strtoul(star + 1, NULL, 16) != xorsum(line + 1, n)) return;
  *star = 0;
  char *body = line + 1;
  char ack[40];
  // GG,<seq>,<MODE>,<deg>,<relay>,<wd>
  char *f[6]; uint8_t k = 0;
  for (char *p = strtok(body, ","); p && k < 6; p = strtok(NULL, ",")) f[k++] = p;
  if (k < 6 || strcmp(f[0], "GG") != 0) return;
  int deg = constrain(atoi(f[3]), 0, 180);
  bool relay = atoi(f[4]) == 1 && strcmp(f[2], "SAFE") != 0;
  watchdogMs = constrain(strtoul(f[5], NULL, 10), 1000UL, 10000UL);
  if (strcmp(f[2], "SAFE") == 0) {
    goSafe();
  } else {
    flag.write(deg);
    digitalWrite(RELAY_PIN, relay ? HIGH : LOW);
    digitalWrite(LED_PIN, relay ? HIGH : LOW);
    safe = false;
  }
  lastValid = millis();
  snprintf(ack, sizeof(ack), "GA,%s,%s", f[1], f[2]);
  sendLine(ack);
}

void setup() {
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(LED_PIN, OUTPUT);
  flag.attach(SERVO_PIN);
  goSafe();
  Serial.begin(115200);
  sendLine("GR,uno_safe_action,1");
  lastValid = millis();
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (len) { buf[len] = 0; handle(buf); len = 0; }
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    } else {
      len = 0;                                     // overlong: garbage, drop it
    }
  }
  if (!safe && millis() - lastValid > watchdogMs) goSafe();
}
