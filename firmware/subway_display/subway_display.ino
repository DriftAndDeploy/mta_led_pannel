/*
 * MTA Subway Panel — ESP32 firmware
 * =================================
 * Hardware : ESP32 WROOM-32 + Waveshare RGB-Matrix-P4-64x32 (HUB75)
 * Libraries: ESP32-HUB75-MatrixPanel-I2S-DMA, Adafruit GFX, ArduinoJson 7,
 *            WiFiManager (tzapu)
 *
 * Arduino IDE board settings (must match for OTA to work):
 *   Board            : ESP32 Dev Module
 *   Partition Scheme : Minimal SPIFFS (1.9MB APP with OTA/190KB SPIFFS)
 *
 * Over-the-air updates
 *   Every refresh the panel tells the server which build it runs. When the
 *   server has a newer build in server/firmware/firmware.bin, the panel
 *   downloads it, verifies the MD5, flashes the spare app slot and reboots.
 *   Release steps are in README.md. Always bump FW_BUILD before exporting.
 *
 * Wiring (HUB75 -> ESP32):
 *   R1=25  G1=26  B1=27  R2=14  G2=12  B2=13
 *   A=23   B=22   C=21   D=19   E=32
 *   CLK=18 LAT=4  OE=15
 */

// ---------------------------------------------------------------------------
// Version — bump FW_BUILD for every release you publish over the air.
// The server reads FW_TAG out of the .bin to learn which build it holds.
// ---------------------------------------------------------------------------
#define FW_VERSION "6.0.1"
#define FW_BUILD   601

#define STR_(x) #x
#define STR(x)  STR_(x)
__attribute__((used)) static const char FW_TAG[] = "##MTAPANEL_BUILD=" STR(FW_BUILD) "##";

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <ArduinoJson.h>
#include <WiFiManager.h>          // "WiFiManager" by tzapu
#include <esp_ota_ops.h>
#if __has_include(<esp_mac.h>)
  #include <esp_mac.h>
#endif

#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>
#include <Adafruit_GFX.h>

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
const char* PROXY_HOST = "whenisthetraincoming.com";  // no https://, no slash
const char* AP_NAME    = "PanelSetup";                // WiFi setup hotspot

const uint32_t REFRESH_MS        = 15000;   // how often to ask the server
const uint32_t STALE_MS          = 180000;  // show --:-- if data older than this
const uint32_t OFFLINE_REBOOT_MS = 300000;  // reboot if WiFi is gone this long
const uint32_t FRAME_MS          = 40;      // render rate; one scroll px per frame
const uint32_t ID_SPLASH_MS      = 15000;   // Panel ID screen on power-up
const uint32_t HTTP_TIMEOUT_MS   = 8000;

// Panel driving. latch_blanking hides the "ghost" glow that appears beside
// lit pixels (most visible left of the logo and inside yellow logo letters).
// Raise to 6-8 if ghosting persists; lower if the panel looks too dim.
const uint8_t  LATCH_BLANKING    = 4;

// ---------------------------------------------------------------------------
// Shared state
//
// The network task (core 0) writes PanelState; the render loop (core 1)
// takes a copy every frame. Network calls never block the scroll ticker.
// ---------------------------------------------------------------------------
struct PanelState {
  char     train_time[8]  = "--:--";
  char     leave_by[8]    = "";
  char     alert_msg[100] = "";
  char     line[4]        = "L";
  uint8_t  logo_r = 167, logo_g = 169, logo_b = 172;
  uint8_t  time_r = 255, time_g = 255, time_b = 255;
  uint8_t  brightness     = 80;
  uint8_t  service_status = 0;      // 0 normal, 1 delays/changes, 2 suspended
  bool     show_leave_by  = true;
  bool     show_alerts    = true;
  bool     panel_off      = false;
  bool     minimalist     = false;
  bool     have_data      = false;
  uint32_t updated_ms     = 0;      // millis() of last good response
  uint32_t version        = 0;      // bumps whenever new data arrives
};

PanelState        g_state;
SemaphoreHandle_t g_state_mutex = nullptr;

volatile bool g_ota_active   = false;
volatile int  g_ota_progress = 0;     // 0-100
volatile int  g_ota_status   = 0;     // 0 running, 1 success, -1 failed

char panel_id[8] = "";

// ---------------------------------------------------------------------------
// Matrix panel
// ---------------------------------------------------------------------------
#define PANEL_RES_X  64
#define PANEL_RES_Y  32
#define PANEL_CHAIN   1

HUB75_I2S_CFG mxconfig(PANEL_RES_X, PANEL_RES_Y, PANEL_CHAIN);
MatrixPanel_I2S_DMA* dma_display = nullptr;

static void configurePanel() {
  mxconfig.gpio.r1 = 25; mxconfig.gpio.g1 = 26; mxconfig.gpio.b1 = 27;
  mxconfig.gpio.r2 = 14; mxconfig.gpio.g2 = 12; mxconfig.gpio.b2 = 13;
  mxconfig.gpio.a  = 23; mxconfig.gpio.b  = 22; mxconfig.gpio.c  = 21;
  mxconfig.gpio.d  = 19; mxconfig.gpio.e  = 32;
  mxconfig.gpio.clk = 18; mxconfig.gpio.lat = 4; mxconfig.gpio.oe = 15;
  mxconfig.clkphase       = false;
  mxconfig.latch_blanking = LATCH_BLANKING;
  // Double buffering: every frame is drawn off-screen and swapped in whole,
  // so the panel never shows a half-erased frame (the old flicker/stray
  // pixel problems came from erasing and redrawing on the visible buffer).
  mxconfig.double_buff    = true;
}

// Draw the back buffer, then call present() to show it.
static inline void present() { dma_display->flipDMABuffer(); }


// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------
void generatePanelId();
void connectWifi();
void netTask(void*);
bool fetchTrainTimes();
void runOta(int build);
void markFirmwareGood();
void render(const PanelState& s);
void drawNormal(const PanelState& s);
void drawMinimalist(const PanelState& s);
void drawLogoAt(int cx, int cy, const PanelState& s);
void drawStatusDot(uint8_t status);
void drawOtaScreen();
void showPanelIdSplash();
void showSplash(const char* line1, const char* line2 = nullptr, const char* line3 = nullptr);

// ---------------------------------------------------------------------------
// Rollback safety
//
// When the precompiled core has app rollback enabled, a freshly flashed OTA
// image stays "on probation" until markFirmwareGood() runs after the first
// successful server response. If the new build crashes or reboots before
// that, the bootloader automatically returns to the previous build.
// ---------------------------------------------------------------------------
#ifdef CONFIG_APP_ROLLBACK_ENABLE
extern "C" bool verifyRollbackLater() { return true; }
#endif

static bool firmwareOnProbation() {
  esp_ota_img_states_t st;
  const esp_partition_t* running = esp_ota_get_running_partition();
  return running && esp_ota_get_state_partition(running, &st) == ESP_OK &&
         st == ESP_OTA_IMG_PENDING_VERIFY;
}

void markFirmwareGood() {
  if (firmwareOnProbation()) {
    esp_ota_mark_app_valid_cancel_rollback();
    Serial.println("Firmware confirmed good — rollback cancelled");
  }
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\n=== MTA panel firmware %s (build %d) %s ===\n",
                FW_VERSION, FW_BUILD, FW_TAG);

  g_state_mutex = xSemaphoreCreateMutex();

  configurePanel();
  dma_display = new MatrixPanel_I2S_DMA(mxconfig);
  if (!dma_display->begin()) Serial.println("Panel init failed");
  dma_display->setBrightness8(map(g_state.brightness, 0, 100, 0, 255));
  dma_display->setTextWrap(false);
  dma_display->clearScreen(); present();
  dma_display->clearScreen(); present();

  // Panel ID first: it comes from the chip's factory MAC, so it is available
  // before WiFi starts. On a plain power-up it stays up for 15 s; after a
  // software restart (e.g. an OTA update) we keep it short.
  generatePanelId();
  showPanelIdSplash();
  esp_reset_reason_t reason = esp_reset_reason();
  bool coldBoot = (reason == ESP_RST_POWERON || reason == ESP_RST_EXT);
  delay(coldBoot ? ID_SPLASH_MS : 3000);

  showSplash("Connecting", "WiFi...", panel_id);
  connectWifi();

  showSplash("Fetching", "trains...", panel_id);   // ID stays visible until data arrives
  // Network work runs on core 0 alongside the WiFi stack; rendering stays
  // on core 1 (Arduino loop) so the ticker never freezes during requests.
  xTaskCreatePinnedToCore(netTask, "net", 16384, nullptr, 1, nullptr, 0);
  Serial.println("Setup complete.");
}

// ---------------------------------------------------------------------------
// Render loop (core 1)
// ---------------------------------------------------------------------------
void loop() {
  static uint32_t last_frame = 0;
  uint32_t now = millis();
  if (now - last_frame < FRAME_MS) { delay(1); return; }
  last_frame = now;

  if (g_ota_active) { drawOtaScreen(); return; }

  PanelState s;
  xSemaphoreTake(g_state_mutex, portMAX_DELAY);
  s = g_state;
  xSemaphoreGive(g_state_mutex);

  if (!s.have_data) return;      // keep the "Fetching trains" splash up
  render(s);
}

// ---------------------------------------------------------------------------
// Panel ID — last 3 bytes of the factory MAC (same IDs as v5.22, so existing
// pairings keep working). Read from eFuse, so WiFi does not need to be up.
// ---------------------------------------------------------------------------
void generatePanelId() {
  uint8_t mac[6] = {0};
  esp_efuse_mac_get_default(mac);
  snprintf(panel_id, sizeof(panel_id), "%02X%02X%02X", mac[3], mac[4], mac[5]);
  Serial.printf("Panel ID: %s\n", panel_id);
}

// ---------------------------------------------------------------------------
// WiFi setup. Two ways to give the panel a new network:
//
//   1. Phone/laptop: join the "PanelSetup" hotspot and open http://192.168.4.1
//   2. USB cable: Arduino IDE > Tools > Serial Monitor (115200 baud,
//      "Newline"), then type   NetworkName|password   and press Enter.
//
// Credentials are saved in flash either way, so this is only needed when the
// network changes. The Panel ID stays on screen during setup.
// ---------------------------------------------------------------------------
const uint32_t PORTAL_TIMEOUT_MS = 600000;   // reboot + retry after 10 minutes

// Reads "SSID|password" lines from the Serial Monitor.
static bool readSerialCredentials(String& ssid, String& pass) {
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (line.length() < 160) line += c;
      continue;
    }
    String in = line;
    line = "";
    in.trim();
    if (in.isEmpty()) continue;
    int bar = in.indexOf('|');
    if (bar <= 0) {
      Serial.println("[setup] Type it as   NetworkName|password   (e.g. Test Test123|hunter2)");
      continue;
    }
    ssid = in.substring(0, bar);
    pass = in.substring(bar + 1);
    return true;
  }
  return false;
}

static bool joinNetwork(const String& ssid, const String& pass) {
  Serial.printf("[setup] Joining \"%s\"...\n", ssid.c_str());
  showSplash("Joining", ssid.c_str(), panel_id);
  WiFi.persistent(true);              // save to flash for next boot
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), pass.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(250);
  return WiFi.status() == WL_CONNECTED;
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFiManager wm;
  wm.setAPCallback([](WiFiManager*) {
    char idLine[16];
    snprintf(idLine, sizeof(idLine), "ID %s", panel_id);
    showSplash("CONNECT TO:", AP_NAME, idLine);
    Serial.printf("[setup] Hotspot \"%s\" is up at http://%s\n", AP_NAME,
                  WiFi.softAPIP().toString().c_str());
    Serial.println("[setup] Or type   NetworkName|password   here and press Enter.");
  });
  wm.setConnectTimeout(20);
  wm.setConfigPortalTimeout(0);       // we handle the timeout below
  wm.setConfigPortalBlocking(false);  // keep looping so Serial input works

  if (!wm.autoConnect(AP_NAME)) {
    uint32_t started = millis(), last_log = 0;
    String ssid, pass;
    while (WiFi.status() != WL_CONNECTED) {
      wm.process();                   // serves the 192.168.4.1 setup page

      if (readSerialCredentials(ssid, pass)) {
        wm.stopConfigPortal();
        if (joinNetwork(ssid, pass)) break;
        Serial.println("[setup] Could not join — check the name and password. Hotspot restarting.");
        wm.startConfigPortal(AP_NAME);
      }

      if (millis() - last_log > 5000) {
        last_log = millis();
        Serial.printf("[setup] Waiting for WiFi details — %d device(s) on PanelSetup, free heap %lu\n",
                      WiFi.softAPgetStationNum(), (unsigned long)ESP.getFreeHeap());
      }
      if (millis() - started > PORTAL_TIMEOUT_MS) {
        Serial.println("[setup] No WiFi after 10 minutes — rebooting to retry the saved network");
        delay(500);
        ESP.restart();
      }
      delay(2);
    }
    wm.stopConfigPortal();            // no-op if WiFiManager already closed it
    WiFi.mode(WIFI_STA);
  }
  WiFi.setAutoReconnect(true);
  Serial.printf("Connected: %s  RSSI %d\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

// ---------------------------------------------------------------------------
// Network task (core 0)
// ---------------------------------------------------------------------------
static int latest_server_build = 0;
static int failed_ota_build    = 0;   // don't retry a build that failed this boot

void netTask(void*) {
  uint32_t offline_since = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      if (!offline_since) offline_since = millis();
      if (millis() - offline_since > OFFLINE_REBOOT_MS) {
        Serial.println("Offline too long — rebooting (opens PanelSetup if needed)");
        ESP.restart();
      }
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }
    offline_since = 0;

    if (fetchTrainTimes()) {
      markFirmwareGood();
      if (latest_server_build > FW_BUILD && latest_server_build != failed_ota_build) {
        runOta(latest_server_build);
      }
    }
    vTaskDelay(pdMS_TO_TICKS(REFRESH_MS));
  }
}

static void copyStr(char* dst, size_t size, const char* src) {
  strncpy(dst, src ? src : "", size - 1);
  dst[size - 1] = '\0';
}

bool fetchTrainTimes() {
  char url[200];
  snprintf(url, sizeof(url), "https://%s/next-trains?panel_id=%s&fw=%d",
           PROXY_HOST, panel_id, FW_BUILD);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, url)) return false;

  int code = http.GET();
  if (code != 200) {
    Serial.printf("Server error: HTTP %d\n", code);
    http.end();
    return false;
  }
  String payload = http.getString();
  http.end();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    Serial.printf("JSON error: %s\n", err.c_str());
    return false;
  }

  PanelState n;
  copyStr(n.train_time, sizeof(n.train_time), doc["train_time"] | "--:--");
  copyStr(n.leave_by,   sizeof(n.leave_by),   doc["leave_by"]   | "");
  copyStr(n.alert_msg,  sizeof(n.alert_msg),  doc["alert_msg"]  | "");
  copyStr(n.line,       sizeof(n.line),       doc["line"]       | "L");
  n.logo_r         = doc["logo_r"]  | 167;
  n.logo_g         = doc["logo_g"]  | 169;
  n.logo_b         = doc["logo_b"]  | 172;
  n.time_r         = doc["time_r"]  | 255;
  n.time_g         = doc["time_g"]  | 255;
  n.time_b         = doc["time_b"]  | 255;
  n.brightness     = constrain((int)(doc["brightness"] | 80), 0, 100);
  n.service_status = doc["service_status"] | 0;
  n.show_leave_by  = doc["show_leave_by"] | true;
  n.show_alerts    = doc["show_alerts"]   | true;
  n.panel_off      = (doc["panel_off"]  | 0) != 0;
  n.minimalist     = (doc["minimalist"] | 0) != 0;
  n.have_data      = true;
  n.updated_ms     = millis();
  latest_server_build = doc["fw_build"] | 0;

  xSemaphoreTake(g_state_mutex, portMAX_DELAY);
  n.version = g_state.version + 1;
  g_state = n;
  xSemaphoreGive(g_state_mutex);

  Serial.printf("Train %s  leave %s  line %s  status %d  alert '%s'  heap %lu\n",
                n.train_time, n.leave_by, n.line, n.service_status,
                n.alert_msg, (unsigned long)ESP.getFreeHeap());
  return true;
}

// ---------------------------------------------------------------------------
// Over-the-air update (runs in the network task)
// ---------------------------------------------------------------------------
void runOta(int build) {
  Serial.printf("OTA: build %d available (running %d) — updating\n", build, FW_BUILD);
  g_ota_progress = 0;
  g_ota_status   = 0;
  g_ota_active   = true;

  char url[200];
  snprintf(url, sizeof(url), "https://%s/firmware/download?build=%d&panel_id=%s",
           PROXY_HOST, build, panel_id);

  WiFiClientSecure client;
  client.setInsecure();
  httpUpdate.rebootOnUpdate(false);
  httpUpdate.onProgress([](int cur, int total) {
    if (total > 0) g_ota_progress = (int)((int64_t)cur * 100 / total);
  });

  t_httpUpdate_return ret = httpUpdate.update(client, url, FW_VERSION);
  if (ret == HTTP_UPDATE_OK) {
    Serial.println("OTA: success — rebooting");
    g_ota_status = 1;
    vTaskDelay(pdMS_TO_TICKS(1500));
    ESP.restart();
  }

  Serial.printf("OTA: failed (%d) %s\n", httpUpdate.getLastError(),
                httpUpdate.getLastErrorString().c_str());
  failed_ota_build = build;
  g_ota_status = -1;
  vTaskDelay(pdMS_TO_TICKS(3000));
  g_ota_active = false;
}

// ---------------------------------------------------------------------------
// Rendering — the whole frame is redrawn into the back buffer every tick.
// ---------------------------------------------------------------------------
static int      scroll_x       = PANEL_RES_X;
static uint32_t scroll_version = 0;
static char     leave_part[24]  = "";
static char     alert_part[110] = "";
static int      last_brightness = -1;

void render(const PanelState& in) {
  PanelState s = in;

  // Stale data: never show a time we can no longer vouch for.
  if (millis() - s.updated_ms > STALE_MS) {
    copyStr(s.train_time, sizeof(s.train_time), "--:--");
    s.leave_by[0] = '\0';
  }

  int target = s.panel_off ? 0 : map(s.brightness, 0, 100, 0, 255);
  if (target != last_brightness) {
    dma_display->setBrightness8(target);
    last_brightness = target;
  }
  if (s.panel_off) {
    dma_display->clearScreen();
    present();
    return;
  }

  // Rebuild the ticker text only when new data arrives.
  if (s.version != scroll_version) {
    bool hasLeave = s.show_leave_by && s.leave_by[0] && strcmp(s.leave_by, "--:--") != 0;
    bool hasAlert = s.show_alerts && s.alert_msg[0];
    if (hasLeave) snprintf(leave_part, sizeof(leave_part), "Leave by %s  ", s.leave_by);
    else          leave_part[0] = '\0';
    if (hasAlert) snprintf(alert_part, sizeof(alert_part), "> %s  ", s.alert_msg);
    else          alert_part[0] = '\0';
    // Only restart the scroll if the text itself changed.
    static char last_text[140] = "";
    char text[140];
    snprintf(text, sizeof(text), "%s%s", leave_part, alert_part);
    if (strcmp(text, last_text) != 0) {
      copyStr(last_text, sizeof(last_text), text);
      scroll_x = PANEL_RES_X;
    }
    scroll_version = s.version;
  }

  dma_display->clearScreen();
  if (s.minimalist) drawMinimalist(s);
  else              drawNormal(s);
  present();
}

// Normal layout
//   Top half    : train time, textSize 2, centred.
//   Bottom half : logo (left while scrolling, centred otherwise) + ticker.
void drawNormal(const PanelState& s) {
  const int SCROLL_MIN = 17;     // ticker is clipped to x >= 17
  const int TEXT_Y     = 20;

  dma_display->setTextSize(2);
  dma_display->setTextColor(dma_display->color565(s.time_r, s.time_g, s.time_b));
  int textWidth = strlen(s.train_time) * 12;
  dma_display->setCursor(max(0, (PANEL_RES_X - textWidth) / 2), 0);
  dma_display->print(s.train_time);

  int leave_len = strlen(leave_part);
  int alert_len = strlen(alert_part);
  bool scrolling = (leave_len + alert_len) > 0;

  if (scrolling) {
    dma_display->setTextSize(1);
    int cx = scroll_x;
    dma_display->setTextColor(dma_display->color565(255, 255, 255));   // leave by: white
    for (int i = 0; i < leave_len; i++, cx += 6) {
      if (cx > -6 && cx < PANEL_RES_X) { dma_display->setCursor(cx, TEXT_Y); dma_display->write(leave_part[i]); }
    }
    dma_display->setTextColor(dma_display->color565(255, 220, 0));     // alert: yellow
    for (int i = 0; i < alert_len; i++, cx += 6) {
      if (cx > -6 && cx < PANEL_RES_X) { dma_display->setCursor(cx, TEXT_Y); dma_display->write(alert_part[i]); }
    }
    // Clip: blank the logo column so glyphs slide smoothly under its edge.
    dma_display->fillRect(0, 16, SCROLL_MIN, 16, 0);

    scroll_x--;
    if (scroll_x < SCROLL_MIN - (leave_len + alert_len) * 6) scroll_x = PANEL_RES_X;
  }

  drawLogoAt(scrolling ? 8 : 32, 24, s);
}

// Minimalist layout: [logo][5px][time] centred as one unit + status dot.
void drawMinimalist(const PanelState& s) {
  const int GAP = 5;
  int timeWidth  = strlen(s.train_time) * 6;
  int totalWidth = 14 + GAP + timeWidth;
  int startX     = (PANEL_RES_X - totalWidth) / 2;

  drawLogoAt(startX + 7, 16, s);

  dma_display->setTextSize(1);
  dma_display->setTextColor(dma_display->color565(s.time_r, s.time_g, s.time_b));
  dma_display->setCursor(startX + 14 + GAP, 12);
  dma_display->print(s.train_time);

  drawStatusDot(s.service_status);
}

// Subway bullet. Dimming keeps bright colours from blooming on the panel;
// N/Q/R/W use unlit (black) letters like the real yellow bullets.
void drawLogoAt(int cx, int cy, const PanelState& s) {
  const char* l = s.line;
  float dim = 0.90f;
  if      (!strcmp(l,"L"))                                            dim = 0.50f;
  else if (!strcmp(l,"1") || !strcmp(l,"2") || !strcmp(l,"3"))       dim = 0.80f;
  else if (!strcmp(l,"7") || !strcmp(l,"G"))                          dim = 0.70f;
  else if (!strcmp(l,"B") || !strcmp(l,"D") || !strcmp(l,"F") || !strcmp(l,"M")) dim = 0.80f;

  bool darkLetter = !strcmp(l,"N") || !strcmp(l,"Q") || !strcmp(l,"R") || !strcmp(l,"W");

  dma_display->fillCircle(cx, cy, 7, dma_display->color565(
      (uint8_t)(s.logo_r * dim), (uint8_t)(s.logo_g * dim), (uint8_t)(s.logo_b * dim)));
  dma_display->setTextSize(1);
  dma_display->setTextColor(darkLetter ? 0 : dma_display->color565(255, 255, 255));
  dma_display->setCursor(cx - (int)(strlen(l) * 6) / 2 + 1, cy - 3);
  dma_display->print(l);
}

void drawStatusDot(uint8_t status) {
  uint16_t c = status == 2 ? dma_display->color565(220,  40,  40)   // suspended
             : status == 1 ? dma_display->color565(255, 200,   0)   // delays
             :               dma_display->color565(  0, 200,  80);  // good
  dma_display->fillCircle(61, 3, 2, c);
}

void drawOtaScreen() {
  dma_display->clearScreen();
  dma_display->setTextSize(1);
  dma_display->setTextColor(dma_display->color565(90, 90, 90));
  dma_display->setCursor(2, 2);
  if (g_ota_status == 1)       dma_display->print("UPDATED");
  else if (g_ota_status == -1) dma_display->print("UPDATE FAIL");
  else                         dma_display->print("UPDATING");
  dma_display->setCursor(2, 12);
  dma_display->printf("%d%%", (int)g_ota_progress);
  dma_display->drawRect(2, 24, 60, 5, dma_display->color565(60, 60, 60));
  dma_display->fillRect(3, 25, (58 * g_ota_progress) / 100, 3,
                        dma_display->color565(0, 160, 255));
  present();
}

void showPanelIdSplash() {
  dma_display->clearScreen();
  dma_display->setTextSize(1);
  dma_display->setTextColor(dma_display->color565(90, 90, 90));
  dma_display->setCursor(8, 1);
  dma_display->print("PANEL ID");
  dma_display->setTextColor(dma_display->color565(255, 255, 255));
  dma_display->setCursor((PANEL_RES_X - (int)strlen(panel_id) * 6) / 2, 12);
  dma_display->print(panel_id);
  dma_display->setTextColor(dma_display->color565(50, 50, 50));
  dma_display->setCursor(0, 24);
  dma_display->print("v" FW_VERSION);
  present();
}

void showSplash(const char* line1, const char* line2, const char* line3) {
  dma_display->clearScreen();
  dma_display->setTextSize(1);
  dma_display->setTextColor(dma_display->color565(90, 90, 90));
  dma_display->setCursor(2, 2);
  dma_display->print(line1);
  if (line2) { dma_display->setCursor(2, 12); dma_display->print(line2); }
  if (line3) {
    dma_display->setTextColor(dma_display->color565(200, 200, 200));
    dma_display->setCursor(2, 22);
    dma_display->print(line3);
  }
  present();
}
