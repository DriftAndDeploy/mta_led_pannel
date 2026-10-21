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
#define FW_VERSION "7.4.0"
#define FW_BUILD   740

#define STR_(x) #x
#define STR(x)  STR_(x)
__attribute__((used)) static const char FW_TAG[] = "##MTAPANEL_BUILD=" STR(FW_BUILD) "##";

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <ArduinoJson.h>
#include <WiFiManager.h>          // "WiFiManager" by tzapu
#include <Preferences.h>
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
const uint32_t ARRIVAL_ANIM_MS   = 4300;    // arriving-train animation length
const uint32_t OCCASION_MS       = 10000;   // holiday/season animation length...
const uint32_t OCCASION_EVERY_MS = 600000;  // ...played every 10 minutes
const uint32_t HTTP_TIMEOUT_MS   = 8000;

// Panel driving. latch_blanking hides the "ghost" glow / faint lines that
// appear next to lit pixels. This is only the boot default: the web app's
// "Ghosting Fix" setting (1-8) overrides it at runtime.
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
  bool     color_all      = false;  // time colour for all text, not just the time
  uint8_t  brightness     = 80;
  uint8_t  latch_blanking = LATCH_BLANKING;
  uint8_t  service_status = 0;      // 0 normal, 1 delays/changes, 2 suspended
  uint16_t test_s         = 0;      // seconds of test pattern requested
  bool     wx_show        = false;  // weather strip on and data available
  int16_t  wx_temp        = 0;      // degrees F
  uint8_t  wx_icon        = 0;      // WX_* below (0 = none)
  bool     wx_umbrella    = false;  // rain likely in the next 12 h
  // Two-row modes: second row
  bool     dual           = false;
  bool     dirs           = false;  // rows are the same line, N then S: draw arrows
  char     train_time2[8] = "--:--";
  char     line2[4]       = "L";
  uint8_t  logo2_r = 167, logo2_g = 169, logo2_b = 172;
  uint8_t  service_status2 = 0;
  uint32_t arriving       = 0;      // nonzero: train 1 pulling in (play animation once per id)
  uint32_t arriving2      = 0;      // same for train 2
  uint8_t  occasion       = 0;      // OCC_* holiday/season animation (0 = none)
  char     occasion_label[24] = "";
  uint32_t occasion_seq   = 0;      // changes -> play now (previews)
  // Scores-only mode (Live Scores on): one game, chosen by the server
  bool     scores_only    = false;  // show games instead of trains
  bool     score_on       = false;  // false: show score_msg instead of a game
  bool     score_final    = false;
  uint8_t  score_win      = 0;      // 1 away / 2 home won -> celebrate
  char     score_msg[16]  = "";
  char     away_abbr[5]   = "", home_abbr[5] = "";
  int16_t  away_score     = 0, home_score = 0;
  uint8_t  away_r = 200, away_g = 200, away_b = 200, home_r = 200, home_g = 200, home_b = 200;
  char     score_status[12] = "";
  uint32_t score_seq      = 0;      // changes when a different game is shown
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
void drawBullet(int cx, int cy, int r, const char* l, uint8_t cr, uint8_t cg, uint8_t cb);
void drawDual(const PanelState& s);
void drawArrival(uint32_t elapsed, const char* line, uint8_t r, uint8_t g, uint8_t b, uint16_t textColor);
uint16_t textColor(const PanelState& s, uint16_t normal);
void drawOccasion(uint32_t elapsed, uint8_t kind, const char* label);
void drawScores(const PanelState& s, bool stale);
void drawStatusDot(uint8_t status);
int  drawRun(const char* text, int x, int y, uint16_t color);
int  runWidth(const char* text);
void buildWeatherText(const PanelState& s);

// Weather icon numbers (match WX_ICONS in server/proxy_server.py) and the
// control characters used for icons inside ticker text.
#define WX_SUN 1
#define WX_MOON 2
#define WX_PARTLY 3
#define WX_CLOUD 4
#define WX_RAIN 5
#define WX_SNOW 6
#define WX_STORM 7
#define CH_UMBRELLA '\x10'
#define CH_DEGREE   '\x11'
#define CH_GAP      '\x12'   // 3 px spacer
#define WX_TEXT_COLOR dma_display->color565(150, 200, 255)

void drawTestPattern();
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

// `show` draws on the panel — only allowed from setup(), never from the
// network task (the render loop owns the display once it is running).
static bool joinNetwork(const String& ssid, const String& pass, bool show,
                        uint32_t timeout_ms = 15000) {
  Serial.printf("[wifi] Joining \"%s\"...\n", ssid.c_str());
  if (show) showSplash("Joining", ssid.c_str(), panel_id);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), pass.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeout_ms) delay(250);
  return WiFi.status() == WL_CONNECTED;
}

// ---------------------------------------------------------------------------
// Known networks
//
// The panel remembers up to MAX_NETS networks in flash (newest/most recently
// used first). That includes the one it is on now, plus any "upcoming"
// network sent from the web app. When the current network disappears (new
// router), the panel scans and joins whichever known network is in range.
// ---------------------------------------------------------------------------
const int MAX_NETS = 4;
struct KnownNet { String ssid, pass; };
static KnownNet known_nets[MAX_NETS];
static int      known_count = 0;

static void loadKnownNets() {
  Preferences p;
  p.begin("nets", true);
  String raw = p.getString("list", "");
  p.end();
  known_count = 0;
  JsonDocument doc;
  if (raw.isEmpty() || deserializeJson(doc, raw)) return;
  for (JsonObject o : doc.as<JsonArray>()) {
    if (known_count >= MAX_NETS) break;
    known_nets[known_count].ssid = o["s"] | "";
    known_nets[known_count].pass = o["p"] | "";
    if (known_nets[known_count].ssid.length()) known_count++;
  }
}

static void saveKnownNets() {
  JsonDocument doc;
  JsonArray arr = doc.to<JsonArray>();
  for (int i = 0; i < known_count; i++) {
    JsonObject o = arr.add<JsonObject>();
    o["s"] = known_nets[i].ssid;
    o["p"] = known_nets[i].pass;
  }
  String raw;
  serializeJson(doc, raw);
  Preferences p;
  p.begin("nets", false);
  p.putString("list", raw);
  p.end();
}

// Adds (or moves) a network to position `pos` (0 = most preferred).
// The same SSID may be stored twice with different passwords, so entering a
// router's new password ahead of time never loses the one that works today.
static void rememberNetwork(const String& ssid, const String& pass, int pos) {
  if (ssid.isEmpty()) return;
  int found = -1;
  for (int i = 0; i < known_count; i++)
    if (known_nets[i].ssid == ssid && known_nets[i].pass == pass) { found = i; break; }
  if (found == pos) return;
  KnownNet n = {ssid, pass};
  if (found >= 0) {                                  // remove existing copy
    for (int i = found; i < known_count - 1; i++) known_nets[i] = known_nets[i + 1];
    known_count--;
  } else if (known_count == MAX_NETS) {
    known_count--;                                   // drop the oldest
  }
  pos = constrain(pos, 0, known_count);
  for (int i = known_count; i > pos; i--) known_nets[i] = known_nets[i - 1];
  known_nets[pos] = n;
  known_count++;
  saveKnownNets();
  Serial.printf("[wifi] Remembered \"%s\" (%d known)\n", ssid.c_str(), known_count);
}

static void rememberCurrentNetwork() {
  if (WiFi.status() == WL_CONNECTED) rememberNetwork(WiFi.SSID(), WiFi.psk(), 0);
}

// Scan, then try every known network that is in range, preferred first.
static bool tryKnownNetworks(bool show) {
  if (known_count == 0) return false;
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  int found = WiFi.scanNetworks();
  if (found <= 0) { WiFi.scanDelete(); return false; }
  bool visible[MAX_NETS] = {false};
  for (int i = 0; i < known_count; i++)
    for (int j = 0; j < found; j++)
      if (WiFi.SSID(j) == known_nets[i].ssid) { visible[i] = true; break; }
  WiFi.scanDelete();

  for (int i = 0; i < known_count; i++) {
    if (!visible[i]) continue;
    KnownNet n = known_nets[i];                      // copy: list may reorder
    if (joinNetwork(n.ssid, n.pass, show)) {
      rememberNetwork(n.ssid, n.pass, 0);
      return true;
    }
  }
  return false;
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  loadKnownNets();
  if (tryKnownNetworks(true)) {
    WiFi.setAutoReconnect(true);
    Serial.printf("Connected to known network \"%s\"  %s  RSSI %d\n", WiFi.SSID().c_str(),
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
    return;
  }

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
        if (joinNetwork(ssid, pass, true, 20000)) break;
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
  rememberCurrentNetwork();           // portal, serial or saved creds — keep it
  Serial.printf("Connected to \"%s\"  %s  RSSI %d\n", WiFi.SSID().c_str(),
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

// ---------------------------------------------------------------------------
// Network task (core 0)
// ---------------------------------------------------------------------------
static int    latest_server_build = 0;
static int    failed_ota_build    = 0;   // don't retry a build that failed this boot
static String wifi_ack_id;               // ack for a network the web app sent us

void netTask(void*) {
  uint32_t offline_since = 0, last_switch_try = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      uint32_t now = millis();
      if (!offline_since) offline_since = now;
      // Current network gone for 30 s (e.g. router swapped)? Try the others.
      if (now - offline_since > 30000 && now - last_switch_try > 90000) {
        last_switch_try = now;
        Serial.println("[wifi] Offline — trying known networks");
        if (tryKnownNetworks(false)) {
          Serial.printf("[wifi] Switched to \"%s\"\n", WiFi.SSID().c_str());
          offline_since = 0;
          continue;
        }
      }
      if (now - offline_since > OFFLINE_REBOOT_MS) {
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

static String urlEncode(const String& s) {
  static const char* hex = "0123456789ABCDEF";
  String out;
  for (size_t i = 0; i < s.length(); i++) {
    uint8_t c = s[i];
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out += (char)c;
    else { out += '%'; out += hex[c >> 4]; out += hex[c & 15]; }
  }
  return out;
}

bool fetchTrainTimes() {
  // Report which network we're on (shown in the web app) and acknowledge
  // any network the web app sent, so the server can delete its password.
  String url = String("https://") + PROXY_HOST + "/next-trains?panel_id=" + panel_id +
               "&fw=" + FW_BUILD + "&ssid=" + urlEncode(WiFi.SSID()) +
               "&rssi=" + WiFi.RSSI();
  if (wifi_ack_id.length()) url += "&wifi_ok=" + urlEncode(wifi_ack_id);

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
  n.color_all      = (doc["color_all"] | 0) != 0;
  n.brightness     = constrain((int)(doc["brightness"] | 80), 0, 100);
  n.latch_blanking = constrain((int)(doc["latch_blanking"] | (int)LATCH_BLANKING), 1, 8);
  n.service_status = doc["service_status"] | 0;
  n.test_s         = doc["test_pattern"] | 0;
  n.wx_show        = (doc["wx"] | 0) != 0;
  n.wx_temp        = doc["wx_temp"] | 0;
  n.wx_icon        = doc["wx_icon"] | 0;
  n.wx_umbrella    = (doc["wx_umbrella"] | 0) != 0;
  n.dual           = (doc["dual"] | 0) != 0;
  n.dirs           = (doc["dirs"] | 0) != 0;
  copyStr(n.train_time2, sizeof(n.train_time2), doc["train_time2"] | "--:--");
  copyStr(n.line2,       sizeof(n.line2),       doc["line2"]       | "L");
  n.logo2_r        = doc["logo2_r"] | 167;
  n.logo2_g        = doc["logo2_g"] | 169;
  n.logo2_b        = doc["logo2_b"] | 172;
  n.service_status2 = doc["service_status2"] | 0;
  n.arriving       = doc["arriving"]  | 0u;
  n.arriving2      = doc["arriving2"] | 0u;
  n.occasion       = doc["occasion"] | 0;
  copyStr(n.occasion_label, sizeof(n.occasion_label), doc["occasion_label"] | "");
  n.occasion_seq   = doc["occasion_seq"] | 0u;
  n.scores_only    = (doc["scores_only"] | 0) != 0;
  n.score_on       = (doc["score_on"] | 0) != 0;
  n.score_final    = (doc["score_final"] | 0) != 0;
  n.score_win      = doc["score_win"] | 0;
  copyStr(n.score_msg, sizeof(n.score_msg), doc["score_msg"] | "");
  copyStr(n.away_abbr, sizeof(n.away_abbr), doc["away_abbr"] | "");
  copyStr(n.home_abbr, sizeof(n.home_abbr), doc["home_abbr"] | "");
  n.away_score     = doc["away_score"] | 0;
  n.home_score     = doc["home_score"] | 0;
  n.away_r = doc["away_r"] | 200; n.away_g = doc["away_g"] | 200; n.away_b = doc["away_b"] | 200;
  n.home_r = doc["home_r"] | 200; n.home_g = doc["home_g"] | 200; n.home_b = doc["home_b"] | 200;
  copyStr(n.score_status, sizeof(n.score_status), doc["score_status"] | "");
  n.score_seq      = doc["score_seq"] | 0u;
  n.show_leave_by  = doc["show_leave_by"] | true;
  n.show_alerts    = doc["show_alerts"]   | true;
  n.panel_off      = (doc["panel_off"]  | 0) != 0;
  n.minimalist     = (doc["minimalist"] | 0) != 0;
  n.have_data      = true;
  n.updated_ms     = millis();
  latest_server_build = doc["fw_build"] | 0;

  // Upcoming network from the web app: store it as the next choice after the
  // current one, then acknowledge on the next request.
  JsonObject net = doc["wifi_new"];
  if (!net.isNull()) {
    String id = net["id"] | "";
    String ssid = net["ssid"] | "";
    if (ssid.length() && id != wifi_ack_id) {
      rememberCurrentNetwork();
      rememberNetwork(ssid, String(net["pass"] | ""), 1);
      wifi_ack_id = id;
    }
  } else {
    wifi_ack_id = "";                 // server has our ack; stop sending it
  }

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
static char     wx_part[16]     = "";   // icon, temp, degree sign, umbrella
static int      last_brightness = -1;
static int      last_latch      = -1;

void render(const PanelState& in) {
  PanelState s = in;

  // Stale data: never show a time we can no longer vouch for.
  if (millis() - s.updated_ms > STALE_MS) {
    copyStr(s.train_time, sizeof(s.train_time), "--:--");
    copyStr(s.train_time2, sizeof(s.train_time2), "--:--");
    s.leave_by[0] = '\0';
    s.wx_show = false;
  }

  // Latch blanking is baked into the panel's timing data when brightness is
  // applied, so changing it forces brightness to be re-applied as well.
  if (s.latch_blanking != last_latch) {
    dma_display->setLatBlanking(s.latch_blanking);
    last_latch = s.latch_blanking;
    last_brightness = -1;
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

  // Diagnostic test pattern (Settings > Test Pattern), counted from when
  // the server told us about it.
  if (s.test_s && millis() - s.updated_ms < (uint32_t)s.test_s * 1000UL) {
    drawTestPattern();
    return;
  }

  // Scores-only mode: games instead of trains.
  if (s.scores_only) {
    drawScores(s, millis() - s.updated_ms > STALE_MS);
    return;
  }

  // Arriving-train animation: once per train id, only on fresh data.
  static uint32_t played1 = 0, played2 = 0, anim_start = 0;
  static uint8_t  anim_train = 0;   // 0 none, 1 = train 1, 2 = train 2
  static uint32_t occ_played = 0, occ_last = 0, occ_start = 0;
  static bool     occ_on = false;
  bool fresh = millis() - s.updated_ms < 30000;
  if (!anim_train && !occ_on && fresh) {
    if (s.arriving && s.arriving != played1) {
      played1 = s.arriving; anim_train = 1; anim_start = millis();
    } else if (s.dual && s.arriving2 && s.arriving2 != played2) {
      played2 = s.arriving2; anim_train = 2; anim_start = millis();
    }
  }
  if (anim_train) {
    uint32_t e = millis() - anim_start;
    if (e < ARRIVAL_ANIM_MS) {
      uint16_t tc = textColor(s, dma_display->color565(220, 220, 220));
      if (anim_train == 1) drawArrival(e, s.line,  s.logo_r,  s.logo_g,  s.logo_b,  tc);
      else                 drawArrival(e, s.line2, s.logo2_r, s.logo2_g, s.logo2_b, tc);
      return;
    }
    anim_train = 0;
  }

  // Holiday / season animation: right away when the sequence changes (new
  // day or a preview), then every 10 minutes while the server sends one.
  if (!anim_train && !occ_on && fresh && s.occasion) {
    if (s.occasion_seq != occ_played || millis() - occ_last >= OCCASION_EVERY_MS) {
      occ_on = true;
      occ_start = millis();
      occ_played = s.occasion_seq;
    }
  }
  if (occ_on) {
    uint32_t e = millis() - occ_start;
    if (e < OCCASION_MS) {
      drawOccasion(e, s.occasion, s.occasion_label);
      return;
    }
    occ_on = false;
    occ_last = millis();
  }

  // Rebuild the ticker text only when new data arrives.
  if (s.version != scroll_version) {
    bool hasLeave = s.show_leave_by && s.leave_by[0] && strcmp(s.leave_by, "--:--") != 0;
    bool hasAlert = s.show_alerts && s.alert_msg[0];
    if (hasLeave) snprintf(leave_part, sizeof(leave_part), "Leave by %s  ", s.leave_by);
    else          leave_part[0] = '\0';
    if (hasAlert) snprintf(alert_part, sizeof(alert_part), "> %s  ", s.alert_msg);
    else          alert_part[0] = '\0';
    buildWeatherText(s);
    // Only restart the scroll if the text itself changed.
    static char last_text[160] = "";
    char text[160];
    snprintf(text, sizeof(text), "%s%s%s", leave_part, wx_part, alert_part);
    if (strcmp(text, last_text) != 0) {
      copyStr(last_text, sizeof(last_text), text);
      scroll_x = PANEL_RES_X;
    }
    scroll_version = s.version;
  }

  dma_display->clearScreen();
  if (s.dual)            drawDual(s);
  else if (s.minimalist) drawMinimalist(s);
  else                   drawNormal(s);
  present();
}

// Colour for a piece of text: its usual colour, or the user's time colour
// when "Use for All Text" is on.
uint16_t textColor(const PanelState& s, uint16_t normal) {
  return s.color_all ? dma_display->color565(s.time_r, s.time_g, s.time_b) : normal;
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

  const int WX_GAP = 12;          // space after the weather in the ticker
  bool weather   = s.wx_show && wx_part[0];
  bool scrolling = (leave_part[0] || alert_part[0]);

  dma_display->setTextSize(1);
  if (scrolling) {
    // Order: leave by (white), weather, service alert (yellow).
    int cx = scroll_x;
    cx = drawRun(leave_part, cx, TEXT_Y, textColor(s, dma_display->color565(255, 255, 255)));
    if (weather) cx = drawRun(wx_part, cx, TEXT_Y, textColor(s, WX_TEXT_COLOR)) + WX_GAP;
    cx = drawRun(alert_part, cx, TEXT_Y, textColor(s, dma_display->color565(255, 220, 0)));
    // Clip: blank the logo column so glyphs slide smoothly under its edge.
    dma_display->fillRect(0, 16, SCROLL_MIN, 16, 0);

    scroll_x--;
    if (cx < SCROLL_MIN) scroll_x = PANEL_RES_X;   // whole ticker has passed
  } else if (weather) {
    // Nothing to scroll: weather sits still beside the logo.
    int w = runWidth(wx_part);
    drawRun(wx_part, SCROLL_MIN + (PANEL_RES_X - SCROLL_MIN - w) / 2, TEXT_Y, textColor(s, WX_TEXT_COLOR));
  }

  drawLogoAt(scrolling || weather ? 8 : 32, 24, s);
}

// Minimalist layout: [logo][5px][time] centred as one unit + status dot.
// With weather on, that row moves up 3 px and the weather sits centred below.
void drawMinimalist(const PanelState& s) {
  const int GAP = 5;
  bool weather   = s.wx_show && wx_part[0];
  bool ticker    = leave_part[0] || alert_part[0];   // leave-by / alerts, if turned on
  int cy         = (weather || ticker) ? 13 : 16;   // logo centre row
  int timeWidth  = strlen(s.train_time) * 6;
  int totalWidth = 14 + GAP + timeWidth;
  int startX     = (PANEL_RES_X - totalWidth) / 2;

  drawLogoAt(startX + 7, cy, s);

  dma_display->setTextSize(1);
  dma_display->setTextColor(dma_display->color565(s.time_r, s.time_g, s.time_b));
  dma_display->setCursor(startX + 14 + GAP, cy - 4);
  dma_display->print(s.train_time);

  if (ticker) {           // scrolls along the bottom: leave by, weather, alert
    int cx = scroll_x;
    cx = drawRun(leave_part, cx, 24, textColor(s, dma_display->color565(255, 255, 255)));
    if (weather) cx = drawRun(wx_part, cx, 24, textColor(s, WX_TEXT_COLOR)) + 12;
    cx = drawRun(alert_part, cx, 24, textColor(s, dma_display->color565(255, 220, 0)));
    scroll_x--;
    if (cx < 0) scroll_x = PANEL_RES_X;
  } else if (weather) {
    drawRun(wx_part, (PANEL_RES_X - runWidth(wx_part)) / 2, 24, textColor(s, WX_TEXT_COLOR));
  }

  drawStatusDot(s.service_status);
}

// Subway bullet. Dimming keeps bright colours from blooming on the panel;
// N/Q/R/W use unlit (black) letters like the real yellow bullets.
void drawLogoAt(int cx, int cy, const PanelState& s) {
  drawBullet(cx, cy, 7, s.line, s.logo_r, s.logo_g, s.logo_b);
}

void drawBullet(int cx, int cy, int r, const char* l, uint8_t cr, uint8_t cg, uint8_t cb) {
  float dim = 0.90f;
  if      (!strcmp(l,"L"))                                            dim = 0.50f;
  else if (!strcmp(l,"1") || !strcmp(l,"2") || !strcmp(l,"3"))       dim = 0.80f;
  else if (!strcmp(l,"7") || !strcmp(l,"G"))                          dim = 0.70f;
  else if (!strcmp(l,"B") || !strcmp(l,"D") || !strcmp(l,"F") || !strcmp(l,"M")) dim = 0.80f;

  bool darkLetter = !strcmp(l,"N") || !strcmp(l,"Q") || !strcmp(l,"R") || !strcmp(l,"W");

  dma_display->fillCircle(cx, cy, r, dma_display->color565(
      (uint8_t)(cr * dim), (uint8_t)(cg * dim), (uint8_t)(cb * dim)));
  dma_display->setTextSize(1);
  dma_display->setTextColor(darkLetter ? 0 : dma_display->color565(255, 255, 255));
  dma_display->setCursor(cx - (int)(strlen(l) * 6) / 2 + 1, cy - 3);
  dma_display->print(l);
}

// Two-train layout: one row per train, [bullet][big time][alert dot].
//   Row 1: y 1-14, row 2: y 17-30. Times use the 2x font with 1 px letter
//   spacing and a slim colon, so "12:45" (46 px) fits beside a 13 px bullet.
//   Times are right-aligned to x=59 so the colons line up like a departure
//   board, and the alert dots sit in a fixed column at x 61-62.
static int bigTimeWidth(const char* t) {
  int w = 0;
  for (const char* p = t; *p; p++) w += (*p == ':') ? 3 : 11;
  return w - 1;
}

static int drawBigTime(int x, int y, const char* t, uint16_t c) {
  dma_display->setTextSize(2);
  dma_display->setTextColor(c);
  for (const char* p = t; *p; p++) {
    if (*p == ':') {
      dma_display->fillRect(x, y + 3, 2, 2, c);
      dma_display->fillRect(x, y + 9, 2, 2, c);
      x += 3;
    } else {
      dma_display->setCursor(x, y);
      dma_display->write(*p);
      x += 11;
    }
  }
  dma_display->setTextSize(1);
  return x - 1;   // first column after the last glyph
}

// Direction arrow in the empty corner just outside the bullet: top-right
// for the N row (points up), bottom-right for the S row (points down).
// Any bullet pixel directly touching the arrow is cleared to keep a gap.
static void drawDirArrow(int y, bool up) {
  static const uint8_t ARROW[3] = {0x04, 0x0E, 0x1F};      // 5 px wide, bit 4 = left
  for (int pass = 0; pass < 2; pass++)
    for (int r = 0; r < 3; r++)
      for (int b = 0; b < 5; b++) {
        if (!(ARROW[r] & (0x10 >> b))) continue;
        int x = 10 + b, yy = up ? y - 1 + r : y + 14 - r;
        if (pass == 0) {
          dma_display->drawPixel(x - 1, yy, 0); dma_display->drawPixel(x, yy - 1, 0);
          dma_display->drawPixel(x, yy + 1, 0);
        } else {
          dma_display->drawPixel(x, yy, dma_display->color565(235, 235, 235));
        }
      }
}

static void drawTrainRow(int y, const char* line, uint8_t r, uint8_t g, uint8_t b,
                         const char* t, uint8_t status, uint16_t timeColor, int8_t arrow = 0) {
  drawBullet(6, y + 7, 6, line, r, g, b);
  if (arrow) drawDirArrow(y, arrow > 0);
  drawBigTime(max(14, 60 - bigTimeWidth(t)), y, t, timeColor);
  if (status) {   // yellow = delays/changes, red = suspended
    uint16_t c = status == 2 ? dma_display->color565(220, 40, 40) : dma_display->color565(255, 200, 0);
    dma_display->fillRect(61, y + 6, 2, 2, c);
  }
}

void drawDual(const PanelState& s) {
  uint16_t tc = dma_display->color565(s.time_r, s.time_g, s.time_b);
  drawTrainRow(1,  s.line,  s.logo_r,  s.logo_g,  s.logo_b,  s.train_time,  s.service_status,  tc,
               s.dirs ? 1 : 0);
  drawTrainRow(17, s.line2, s.logo2_r, s.logo2_g, s.logo2_b, s.train_time2, s.service_status2, tc,
               s.dirs ? -1 : 0);
}

// ---------------------------------------------------------------------------
// Arriving-train animation (~4.3 s): bullet + "ARRIVING" on top, and a
// two-car train in the line's colour that eases in from the right, stops,
// and pulls out to the left. Sprite legend:
//   r roof  s body  w lit window  c line-colour stripe  u underframe
//   k wheel  h headlight  t tail light
// ---------------------------------------------------------------------------
static const char* const TRAIN_SPRITE[] = {
  "..rrrrrrrrrrrrrrrrrrrr.....rrrrrrrrrrrrrrrrrrrr..",
  ".ssssssssssssssssssssss...ssssssssssssssssssssss.",
  ".sswwwwssswwwwssswwwwss...sswwwwssswwwwssswwwwss.",
  ".sswwwwssswwwwssswwwwss...sswwwwssswwwwssswwwwss.",
  ".sswwwwssswwwwssswwwwss...sswwwwssswwwwssswwwwss.",
  ".ssssssssssssssssssssss...ssssssssssssssssssssss.",
  ".cccccccccccccccccccccc...cccccccccccccccccccccc.",
  ".hsssssssssssssssssssss...ssssssssssssssssssssst.",
  ".hsssssssssssssssssssssuuussssssssssssssssssssss.",
  ".uuuuuuuuuuuuuuuuuuuuuu...uuuuuuuuuuuuuuuuuuuuuu.",
  "...kk..kk.......kk..kk.......kk..kk.......kk..kk.",
  "...kk..kk.......kk..kk.......kk..kk.......kk..kk.",
};
static const int TRAIN_W = 49, TRAIN_H = 12, TRAIN_Y = 18;
static const uint32_t ANIM_IN_MS = 1500, ANIM_STOP_MS = 1300, ANIM_OUT_MS = 1500;
static const int TRAIN_STOP_X = (PANEL_RES_X - TRAIN_W) / 2;

static void drawTrainSprite(int x0, uint16_t stripe) {
  uint16_t pal[128] = {0};
  pal['r'] = dma_display->color565( 80,  80,  92);
  pal['s'] = dma_display->color565(120, 120, 135);
  pal['w'] = dma_display->color565(190, 160,  80);
  pal['c'] = stripe;
  pal['u'] = dma_display->color565( 45,  45,  50);
  pal['k'] = dma_display->color565( 90,  90,  90);
  pal['h'] = dma_display->color565(255, 255, 200);
  pal['t'] = dma_display->color565(220,   0,   0);
  for (int row = 0; row < TRAIN_H; row++) {
    const char* line = TRAIN_SPRITE[row];
    for (int col = 0; col < TRAIN_W; col++) {
      int x = x0 + col;
      if (x < 0 || x >= PANEL_RES_X || line[col] == '.') continue;
      dma_display->drawPixel(x, TRAIN_Y + row, pal[(uint8_t)line[col] & 127]);
    }
  }
}

void drawArrival(uint32_t e, const char* line, uint8_t r, uint8_t g, uint8_t b, uint16_t textColor) {
  int x;
  if (e < ANIM_IN_MS) {                                  // ease in, slowing to a stop
    float t = 1.0f - (float)e / ANIM_IN_MS;
    x = TRAIN_STOP_X + (int)((PANEL_RES_X - TRAIN_STOP_X) * t * t);
  } else if (e < ANIM_IN_MS + ANIM_STOP_MS) {            // stopped at the platform
    x = TRAIN_STOP_X;
  } else {                                               // ease out, speeding up
    float t = (float)(e - ANIM_IN_MS - ANIM_STOP_MS) / ANIM_OUT_MS;
    x = TRAIN_STOP_X - (int)((TRAIN_STOP_X + TRAIN_W) * t * t);
  }

  dma_display->clearScreen();
  drawBullet(7, 7, 6, line, r, g, b);
  dma_display->setTextSize(1);
  dma_display->setTextColor(textColor);
  dma_display->setCursor(16, 4);
  dma_display->print("ARRIVING");

  dma_display->fillRect(0, 30, PANEL_RES_X, 1, dma_display->color565(55, 55, 60));     // rail
  for (int sx = 1; sx < PANEL_RES_X; sx += 4)                                           // sleepers
    dma_display->drawPixel(sx, 31, dma_display->color565(50, 35, 20));
  drawTrainSprite(x, dma_display->color565((uint8_t)(r * 0.9f), (uint8_t)(g * 0.9f), (uint8_t)(b * 0.9f)));
  present();
}

// ---------------------------------------------------------------------------
// Holiday / season animations (10 s, full panel). The server picks the
// occasion; numbers match OCC_KINDS in server/proxy_server.py. Everything is
// drawn as a pure function of elapsed time (no state), using a hash for
// "random" placement so each frame is consistent with the last.
// ---------------------------------------------------------------------------
enum {
  OCC_FIREWORKS = 1, OCC_FIREWORKS_USA, OCC_HEARTS, OCC_SHAMROCKS, OCC_EGGS,
  OCC_PUMPKIN, OCC_LEAVES, OCC_TREE, OCC_FLAG, OCC_STARS, OCC_FLOWERS,
  OCC_SUN, OCC_SNOW, OCC_TURTLE
};

static uint32_t hsh(uint32_t x) {
  x ^= x >> 16; x *= 0x7feb352dU; x ^= x >> 15; x *= 0x846ca68bU; x ^= x >> 16;
  return x;
}
static inline uint16_t rgb(int r, int g, int b) {
  return dma_display->color565(constrain(r, 0, 255), constrain(g, 0, 255), constrain(b, 0, 255));
}
static inline uint16_t rgbk(const uint8_t* c, float k) { return rgb(c[0] * k, c[1] * k, c[2] * k); }
static inline void px(int x, int y, uint16_t c) {
  if (x >= 0 && x < PANEL_RES_X && y >= 0 && y < PANEL_RES_Y) dma_display->drawPixel(x, y, c);
}
// 5-px-wide bitmaps, bit 4 = leftmost pixel
static void bits5(int x, int y, const uint8_t* rows, int n, uint16_t c) {
  for (int r = 0; r < n; r++)
    for (int b = 0; b < 5; b++)
      if (rows[r] & (0x10 >> b)) px(x + b, y + r, c);
}

// 7-px-wide bitmaps, bit 6 = leftmost pixel
static void bits7(int x, int y, const uint8_t* rows, int n, uint16_t c) {
  for (int r = 0; r < n; r++)
    for (int b = 0; b < 7; b++)
      if (rows[r] & (0x40 >> b)) px(x + b, y + r, c);
}

// Label along the bottom with a dark outline so it reads over the scene.
// Fits -> centred and still; too long -> scrolls across once in 10 s.
static void drawLabel(uint32_t e, const char* label, uint16_t c) {
  if (!label[0]) return;
  int w = strlen(label) * 6 - 1;
  int x = (w <= PANEL_RES_X - 2) ? (PANEL_RES_X - w) / 2
        : PANEL_RES_X - (int)((uint64_t)e * (PANEL_RES_X + w) / OCCASION_MS);
  const int y = 24;
  dma_display->setTextSize(1);
  dma_display->setTextColor(0);
  for (int d = 0; d < 4; d++) {
    dma_display->setCursor(x + (d == 0) - (d == 1), y + (d == 2) - (d == 3));
    dma_display->print(label);
  }
  dma_display->setTextColor(c);
  dma_display->setCursor(x, y);
  dma_display->print(label);
}

// Fireworks: a rocket every 0.65 s rises, bursts into 14 sparks that
// spread, sag with gravity and fade.
static void drawFireworks(uint32_t e, bool usa) {
  static const uint8_t NY[][3] = {{255, 200, 60}, {255, 255, 255}, {255, 80, 170}, {80, 200, 255}, {150, 255, 120}};
  static const uint8_t US[][3] = {{230, 30, 30}, {210, 210, 210}, {40, 90, 255}};
  const uint32_t GAP = 480, LIFE = 2000, RISE = 500;
  for (int k = (int)(e / GAP) - 3; k <= (int)(e / GAP); k++) {
    if (k < 0) continue;
    uint32_t t = e - k * GAP;
    if (t >= LIFE) continue;
    uint32_t h = hsh(k * 2654435761U + 17);
    int bx = 8 + h % 48, by = 4 + (h >> 8) % 11;
    const uint8_t* c = usa ? US[(h >> 16) % 3] : NY[(h >> 16) % 5];
    if (t < RISE) {
      float p = (float)t / RISE;
      int y = 31 - (int)((31 - by) * (1 - (1 - p) * (1 - p)));
      px(bx, y, rgb(220, 190, 120));
      px(bx, y + 1, rgb(90, 70, 40));
    } else {
      float p = (float)(t - RISE) / (LIFE - RISE);
      float r = (9 + h % 4) * (1 - (1 - p) * (1 - p));
      float fade = p < 0.5f ? 1.0f : 2 * (1 - p);          // full brightness, then fade
      if (p < 0.08f) dma_display->fillCircle(bx, by, 1, rgb(255, 255, 220));   // flash
      for (int i = 0; i < 18; i++) {
        float a = i * 0.349f + (h % 100) * 0.01f;
        float sag = 5.0f * p * p;
        px(bx + lroundf(cosf(a) * r), by + lroundf(sinf(a) * r + sag), rgbk(c, fade));
        px(bx + lroundf(cosf(a) * r * 0.7f), by + lroundf(sinf(a) * r * 0.7f + sag), rgbk(c, fade * 0.45f));
      }
    }
  }
}

// Drifting particles: snow (falls), hearts (rise), shamrocks, leaves.
enum { P_SNOW, P_HEART, P_SHAMROCK, P_LEAF };
static const uint8_t SPR_FLAKE[3]    = {0x04, 0x0E, 0x04};
static const uint8_t SPR_HEART[4]    = {0x0A, 0x1F, 0x0E, 0x04};
static const uint8_t SPR_SHAMROCK[5] = {0x04, 0x0E, 0x1F, 0x1B, 0x04};
static const uint8_t SPR_LEAF[4]     = {0x0C, 0x1E, 0x1C, 0x08};

static void drawParticles(uint32_t e, int type, int count) {
  static const uint8_t HEARTS[][3] = {{255, 40, 90}, {255, 120, 170}, {210, 0, 50}};
  static const uint8_t LEAVES[][3] = {{230, 90, 0}, {200, 40, 0}, {230, 170, 0}, {150, 75, 20}};
  for (int i = 0; i < count; i++) {
    uint32_t h = hsh(i * 0x9E3779B9U + type * 101 + 7);
    float speed = (type == P_SNOW ? 4 : 7) + (h % 8);          // px per second
    int y = (int)((h >> 8) % 42 + e * speed / 1000.0f) % 42 - 6;
    if (type == P_HEART) y = 30 - y;
    float sway = sinf(e / 700.0f + (h % 628) / 100.0f) * (type == P_SNOW ? 1.5f : 2.5f);
    int x = (int)((h >> 16) % 70) - 4 + lroundf(sway);
    switch (type) {
      case P_SNOW:
        if (h & 3) px(x + 2, y + 1, rgb(190, 190, 215));
        else       bits5(x, y, SPR_FLAKE, 3, rgb(225, 225, 255));
        break;
      case P_HEART:    bits5(x, y, SPR_HEART, 4, rgbk(HEARTS[(h >> 4) % 3], 1)); break;
      case P_SHAMROCK: bits5(x, y, SPR_SHAMROCK, 5, (h >> 4) & 1 ? rgb(0, 190, 60) : rgb(70, 230, 90)); break;
      case P_LEAF:     bits5(x, y, SPR_LEAF, 4, rgbk(LEAVES[(h >> 4) % 4], 1)); break;
    }
  }
}

static void drawGround(uint16_t c, int y = 21) {   // 2-row strip; label sits below on black
  dma_display->fillRect(0, y, PANEL_RES_X, 2, c);
}

// Easter: five striped eggs hopping on the grass.
static void drawEggs(uint32_t e) {
  static const uint8_t C[][3] = {{255, 120, 180}, {110, 200, 255}, {255, 215, 60}, {150, 255, 130}, {200, 140, 255}};
  for (int x = 0; x < PANEL_RES_X; x++) {
    px(x, 22, rgb(20, 120, 30));
    if (hsh(x + 5) % 3 == 0) px(x, 21, rgb(30, 150, 40));
  }
  for (int i = 0; i < 5; i++) {
    int hop = (int)(fabsf(sinf(e / 260.0f + i * 1.3f)) * 6);
    int x = 3 + i * 12, y = 15 - hop;
    const uint8_t* a = C[i];
    const uint8_t* b = C[(i + 2) % 5];
    for (int r = 0; r < 7; r++) {
      int x0 = (r == 0 || r == 6) ? 1 : 0, x1 = (r == 0 || r == 6) ? 3 : 4;
      for (int c = x0; c <= x1; c++) {
        bool stripe = (r == 2) || (r == 4 && (c % 2 == 0));
        px(x + c, y + r, rgbk(stripe ? b : a, 0.9f));
      }
    }
  }
}

// Halloween: jack-o'-lantern with a flickering face, bats flapping past.
static void drawPumpkin(uint32_t e) {
  const int cx = 32, cy = 12;
  dma_display->fillCircle(cx - 5, cy, 6, rgb(190, 70, 0));
  dma_display->fillCircle(cx + 5, cy, 6, rgb(190, 70, 0));
  dma_display->fillCircle(cx, cy, 7, rgb(235, 100, 0));
  dma_display->fillRect(cx - 1, cy - 10, 3, 3, rgb(40, 140, 30));
  float flick = 0.7f + 0.3f * ((hsh(e / 90) % 100) / 100.0f);
  uint16_t face = rgb(255 * flick, 210 * flick, 40 * flick);
  dma_display->fillTriangle(cx - 6, cy - 1, cx - 2, cy - 1, cx - 4, cy - 4, face);
  dma_display->fillTriangle(cx + 2, cy - 1, cx + 6, cy - 1, cx + 4, cy - 4, face);
  dma_display->fillRect(cx - 6, cy + 2, 13, 3, face);
  px(cx - 3, cy + 2, 0); px(cx + 3, cy + 2, 0); px(cx, cy + 4, 0);      // teeth
  static const uint8_t UP[3]   = {0x41, 0x3E, 0x08};                     // 7 px wide
  static const uint8_t DOWN[3] = {0x08, 0x3E, 0x41};
  for (int i = 0; i < 3; i++) {
    int x = (int)((e * (0.010f + i * 0.004f)) + i * 30) % 84 - 10;
    int y = 1 + i * 2 + lroundf(sinf(e / 300.0f + i) * 2);
    bits7(x, y, ((e / 150 + i) & 1) ? UP : DOWN, 3, rgb(150, 60, 210));
  }
}

// Christmas: tree with blinking lights and a twinkling star, in the snow.
static void drawTree(uint32_t e) {
  drawParticles(e, P_SNOW, 22);
  const int cx = 32;
  for (int r = 0; r < 18; r++) {
    int tier = r < 6 ? r : (r < 12 ? r - 3 : r - 6);   // three stacked tiers
    int hw = 1 + tier * 2 / 3 + (r >= 12 ? 1 : 0);
    dma_display->fillRect(cx - hw, 3 + r, hw * 2 + 1, 1, rgb(0, 120 + (r % 3) * 15, 40));
  }
  dma_display->fillRect(cx - 2, 21, 5, 2, rgb(110, 60, 20));
  uint16_t star = ((e / 300) & 1) ? rgb(255, 230, 60) : rgb(255, 180, 0);
  px(cx, 1, star); px(cx - 1, 2, star); px(cx, 2, star); px(cx + 1, 2, star); px(cx, 3, star);
  static const uint8_t L[][3] = {{255, 40, 40}, {255, 200, 40}, {60, 120, 255}, {230, 230, 230}};
  for (int i = 0; i < 16; i++) {
    uint32_t h = hsh(i + 300);
    int r = 3 + h % 16, tier = r < 6 ? r : (r < 12 ? r - 3 : r - 6);
    int hw = 1 + tier * 2 / 3 + (r >= 12 ? 1 : 0);
    int x = cx - hw + (int)((h >> 8) % (hw * 2 + 1));
    if ((e / 400 + i) % 3) px(x, 3 + r, rgbk(L[i % 4], 1));
  }
}

// US flag waving on a pole (Memorial, Labor, Veterans, Presidents Day).
static void drawFlag(uint32_t e) {
  const int FX = 21, FY = 4, FW = 26, FH = 14;
  dma_display->fillRect(FX - 2, 2, 1, 21, rgb(150, 150, 150));
  px(FX - 2, 1, rgb(220, 180, 60));
  for (int x = 0; x < FW; x++) {
    float ph = x * 0.35f - e / 180.0f;
    int off = lroundf(1.5f * sinf(ph) * x / FW);
    float shade = 0.75f + 0.25f * cosf(ph);
    for (int y = 0; y < FH; y++) {
      uint16_t c;
      if (x < 11 && y < 8)  c = (x % 2 == 1 && y % 2 == 1) ? rgb(200 * shade, 200 * shade, 200 * shade)
                                                           : rgb(20 * shade, 40 * shade, 150 * shade);
      else if (y % 2 == 0)  c = rgb(200 * shade, 20 * shade, 30 * shade);
      else                  c = rgb(160 * shade, 160 * shade, 160 * shade);
      px(FX + x, FY + y + off, c);
    }
  }
}

// Twinkling stars (MLK Day, Juneteenth).
static void drawStars(uint32_t e) {
  for (int i = 0; i < 45; i++) {
    uint32_t h = hsh(i * 7 + 3);
    float tw = 0.5f + 0.5f * sinf(e / 300.0f * (1 + (h >> 16) % 3) + i);
    int v = 30 + (int)(200 * tw * tw);
    px(h % 64, (h >> 8) % 22, ((h >> 20) & 1) ? rgb(v, v * 0.8f, v * 0.3f) : rgb(v, v, v));
  }
}

// Spring: flowers grow out of the grass and bloom one after another.
static void drawFlowers(uint32_t e) {
  static const uint8_t PET[][3] = {{255, 90, 170}, {180, 90, 255}, {255, 60, 60}, {255, 200, 40}, {90, 160, 255}};
  drawGround(rgb(20, 110, 30));
  for (int i = 0; i < 6; i++) {
    uint32_t h = hsh(i + 91);
    int x = 5 + i * 11 + h % 3, H = 7 + (h >> 4) % 8;
    uint32_t t0 = 1500 + i * 250;
    float grow = min(1.0f, (float)e / t0);
    int top = 21 - (int)(H * grow);
    dma_display->fillRect(x, top, 1, 21 - top, rgb(40, 170, 40));
    if (grow > 0.5f) { px(x - 1, 21 - H / 2, rgb(60, 200, 60)); px(x + 1, 20 - H / 2, rgb(60, 200, 60)); }
    if (e < t0) continue;
    float bloom = min(1.0f, (e - t0) / 600.0f);
    int hx = x + lroundf(sinf(e / 500.0f + i) * 0.8f);
    uint16_t pc = rgbk(PET[i % 5], 0.4f + 0.6f * bloom);
    if (bloom > 0.2f) { px(hx - 1, top, pc); px(hx + 1, top, pc); px(hx, top - 1, pc); px(hx, top + 1, pc); }
    if (bloom > 0.7f) { px(hx - 1, top - 1, pc); px(hx + 1, top - 1, pc); px(hx - 1, top + 1, pc); px(hx + 1, top + 1, pc); }
    px(hx, top, rgb(255, 220, 60));
  }
}

// Summer: a big sun with slowly turning rays.
static void drawSun(uint32_t e) {
  const int cx = 32, cy = 11;
  float rot = e / 1500.0f;
  for (int k = 0; k < 12; k++) {
    float a = rot + k * 0.5236f;
    int len = (k % 2) ? 11 : 13;
    uint16_t c = (k % 2) ? rgb(255, 140, 0) : rgb(255, 210, 40);
    for (int r = 8; r <= len; r++) px(cx + lroundf(cosf(a) * r), cy + lroundf(sinf(a) * r), c);
  }
  dma_display->fillCircle(cx, cy, 6, rgb(255, 180, 0));
  dma_display->fillCircle(cx - 1, cy - 1, 3, rgb(255, 225, 90));
}

// Winter: snowfall under a crescent moon, snow on the ground.
static void drawSnowScene(uint32_t e) {
  drawStars(e / 2);
  dma_display->fillCircle(52, 6, 4, rgb(230, 230, 190));
  dma_display->fillCircle(54, 5, 4, 0);
  drawParticles(e, P_SNOW, 30);
  drawGround(rgb(110, 110, 130));
}

// Easter egg: a dancing turtle (drawn at 2x) on a disco floor, with notes.
static const char* const TURTLE[] = {   // 20 x 7, facing right
  ".....SSSSSS....KKK..",
  "...SSPPSSPPSS.KKK.K.",
  "..SPPSSPPSSPPSKKKKK.",
  ".SSSSSSSSSSSSSSKK...",
  "KYYYYYYYYYYYYYY.....",
};
static void drawTurtle(uint32_t e) {
  const uint32_t BEAT = 450;
  uint32_t beat = e / BEAT;
  float ph = (float)(e % BEAT) / BEAT;
  static const uint8_t DISCO[][3] = {{255, 40, 120}, {40, 200, 255}, {255, 210, 40}, {120, 255, 90}};
  for (int t = 0; t < 16; t++)
    dma_display->fillRect(t * 4, 30, 4, 2, rgbk(DISCO[(t + beat) % 4], 0.55f));

  bool spin = e > 6000 && e < 7000;                     // a quick spin mid-dance
  bool left = spin ? ((e / 100) & 1) : ((beat / 4) & 1);
  int hop = lroundf(fabsf(sinf(ph * 3.14159f)) * 3);
  int cx = 32 + lroundf(9 * sinf(e / (BEAT * 8.0f) * 6.2832f));
  int ox = cx - 20, oy = 16 - hop;
  bool stepA = ph < 0.5f;
  auto put = [&](int x, int y, uint16_t c) {           // 1x sprite coords -> 2x panel pixels
    int sx = left ? 19 - x : x;
    dma_display->fillRect(ox + sx * 2, oy + y * 2, 2, 2, c);
  };
  for (int y = 0; y < 5; y++)
    for (int x = 0; x < 20; x++) {
      char ch = TURTLE[y][x];
      if (ch == '.') continue;
      bool head = x >= 13 && y <= 3 && ch == 'K';
      uint16_t c = ch == 'S' ? rgb(0, 130, 60) : ch == 'P' ? rgb(120, 200, 60)
                 : ch == 'Y' ? rgb(200, 170, 60) : rgb(110, 210, 90);
      put(x, y + ((head && (beat & 1)) ? 1 : 0), c);   // head bobs on every other beat
    }
  uint16_t skin = rgb(110, 210, 90);
  int back = stepA ? 2 : 4, front = stepA ? 12 : 10;   // legs swap every half beat
  for (int dx = 0; dx < 2; dx++) { put(back + dx, 5, skin); put(back + dx, 6, skin); put(front + dx, 5, skin); put(front + dx, 6, skin); }

  static const uint8_t NOTE[7] = {0x0C, 0x0A, 0x09, 0x08, 0x38, 0x78, 0x30};   // eighth note, 7 px wide
  for (int k = (int)(e / 700) - 3; k <= (int)(e / 700); k++) {
    if (k < 0) continue;
    uint32_t t = e - k * 700;
    if (t > 2100) continue;
    uint32_t h = hsh(k + 77);
    int nx = cx - 12 + h % 24 + lroundf(sinf(t / 250.0f + k) * 2);
    int ny = 10 - (int)(t * 0.012f);
    bits7(nx, ny, NOTE, 7, rgbk(DISCO[h % 4], 1.0f - t / 2600.0f));
  }
}

void drawOccasion(uint32_t e, uint8_t kind, const char* label) {
  dma_display->clearScreen();
  uint16_t lc = rgb(230, 230, 230);
  switch (kind) {
    case OCC_FIREWORKS:     drawFireworks(e, false); lc = rgb(255, 210, 80); break;
    case OCC_FIREWORKS_USA: drawFireworks(e, true);  break;
    case OCC_HEARTS:        drawParticles(e, P_HEART, 16); lc = rgb(255, 140, 180); break;
    case OCC_SHAMROCKS:     drawParticles(e, P_SHAMROCK, 16); lc = rgb(120, 255, 120); break;
    case OCC_EGGS:          drawEggs(e); lc = rgb(255, 230, 120); break;
    case OCC_PUMPKIN:       drawPumpkin(e); lc = rgb(255, 140, 0); break;
    case OCC_LEAVES:        drawParticles(e, P_LEAF, 18); lc = rgb(255, 170, 60); break;
    case OCC_TREE:          drawTree(e); lc = rgb(255, 80, 80); break;
    case OCC_FLAG:          drawFlag(e); break;
    case OCC_STARS:         drawStars(e); lc = rgb(255, 220, 120); break;
    case OCC_FLOWERS:       drawFlowers(e); lc = rgb(255, 150, 210); break;
    case OCC_SUN:           drawSun(e); lc = rgb(255, 220, 60); break;
    case OCC_SNOW:          drawSnowScene(e); lc = rgb(180, 220, 255); break;
    case OCC_TURTLE:        drawTurtle(e); break;
  }
  drawLabel(e, label, lc);
  present();
}

// Live score card:   NYY        TOR      <- team colours
//                    3          2       <- big digits, centred in each half
//                       TOP 5TH          <- game status
static void centerText(const char* t, int cx, int y, uint16_t c) {
  dma_display->setTextSize(1);
  dma_display->setTextColor(c);
  dma_display->setCursor(cx - ((int)strlen(t) * 6 - 1) / 2, y);
  dma_display->print(t);
}

// Big score centred on cx. Three-digit scores (basketball) drop the letter
// spacing so the two halves keep a gap between them.
static void drawScoreDigits(const char* t, int cx, uint16_t c) {
  int n = strlen(t), adv = n >= 3 ? 10 : 11;
  int w = n * adv - (adv - 10);
  int x = cx - w / 2;
  dma_display->setTextSize(2);
  dma_display->setTextColor(c);
  for (int i = 0; i < n; i++) { dma_display->setCursor(x + i * adv, 9); dma_display->write(t[i]); }
  dma_display->setTextSize(1);
}

// Scores-only screen. A game: the card above; a just-won game: confetti in
// the winner's colours, the winner's abbreviation flashing and "NYY WIN!";
// no game: "SCORES" and the server's message (e.g. NEXT / NYK 7:30P).
void drawScores(const PanelState& s, bool stale) {
  dma_display->clearScreen();
  uint32_t now = millis();
  if (stale || !s.score_on) {
    centerText("SCORES", 32, 2, dma_display->color565(142, 203, 255));
    const char* msg = stale ? "NO DATA" : s.score_msg;
    uint16_t c = textColor(s, dma_display->color565(230, 190, 60));
    if (!strncmp(msg, "NEXT ", 5)) {
      centerText("NEXT GAME", 32, 13, dma_display->color565(120, 120, 120));
      centerText(msg + 5, 32, 23, c);
    } else if (strlen(msg) > 10 && strchr(msg, ' ')) {    // too wide: two lines
      char line[16];
      copyStr(line, sizeof(line), msg);
      char* sp = strrchr(line, ' ');
      *sp = '\0';
      centerText(line, 32, 13, c);
      centerText(sp + 1, 32, 23, c);
    } else {
      centerText(msg, 32, 17, c);
    }
    present();
    return;
  }

  bool win = s.score_final && s.score_win;
  uint8_t wr = s.score_win == 1 ? s.away_r : s.home_r;
  uint8_t wg = s.score_win == 1 ? s.away_g : s.home_g;
  uint8_t wb = s.score_win == 1 ? s.away_b : s.home_b;
  if (win) {                                       // confetti behind everything
    for (int i = 0; i < 44; i++) {
      uint32_t h = hsh(i * 0x9E3779B9U + 4242);
      float speed = 9 + h % 10;
      int y = (int)((h >> 8) % 36 + now * speed / 1000.0f) % 36 - 2;
      int x = (int)((h >> 16) % 66) - 1 + lroundf(sinf(now / 500.0f + i) * 1.5f);
      uint16_t c = (h & 3) == 0 ? rgb(255, 210, 60) : (h & 3) == 1 ? rgb(230, 230, 230) : rgb(wr, wg, wb);
      px(x, y, c);
      if (h & 4) px(x + 1, y, c);
    }
  }
  bool flash = win && ((now / 300) & 1);
  char a[6], h[6];
  snprintf(a, sizeof(a), "%d", s.away_score);
  snprintf(h, sizeof(h), "%d", s.home_score);
  auto teamColor = [&](uint8_t r, uint8_t g, uint8_t b, bool winner) {
    return (winner && flash) ? dma_display->color565(255, 255, 255) : dma_display->color565(r, g, b);
  };
  centerText(s.away_abbr, 16, 1, teamColor(s.away_r, s.away_g, s.away_b, s.score_win == 1));
  centerText(s.home_abbr, 48, 1, teamColor(s.home_r, s.home_g, s.home_b, s.score_win == 2));
  uint16_t digits = textColor(s, dma_display->color565(255, 255, 255));
  drawScoreDigits(a, 16, digits);
  drawScoreDigits(h, 48, digits);
  if (strlen(a) < 3 && strlen(h) < 3)                       // dash between short scores
    dma_display->fillRect(30, 15, 4, 2, dma_display->color565(90, 90, 90));
  if (win) {
    char msg[12];
    snprintf(msg, sizeof(msg), "%s WIN!", s.score_win == 1 ? s.away_abbr : s.home_abbr);
    centerText(msg, 32, 24, flash ? dma_display->color565(255, 230, 120) : dma_display->color565(wr, wg, wb));
  } else {
    centerText(s.score_status, 32, 24, textColor(s, dma_display->color565(230, 190, 60)));
  }
  present();
}

void drawStatusDot(uint8_t status) {
  uint16_t c = status == 2 ? dma_display->color565(220,  40,  40)   // suspended
             : status == 1 ? dma_display->color565(255, 200,   0)   // delays
             :               dma_display->color565(  0, 200,  80);  // good
  // 2x2 block in the top-left corner: 1 px down, 2 px in. The panel has no
  // working LEDs at (0,2) and (1,2) (same spot mirrored in every corner),
  // so the dot starts at x=2 to stay clear of them.
  dma_display->fillRect(2, 1, 2, 2, c);
}

// ---------------------------------------------------------------------------
// Weather strip. Icons are 7x7 bitmaps (bit 6 = leftmost pixel) that ride in
// the ticker text as control characters, so they scroll with it:
//   '\x01'-'\x07' weather icon (same numbers as the server's WX_ICONS)
//   '\x10' umbrella, '\x11' degree sign, '\x12' 3 px gap
// ---------------------------------------------------------------------------
// (WX_* / CH_* constants are defined near the top of the file.)

static const uint8_t BMP_SUN[7]      = {0x08, 0x22, 0x1C, 0x5D, 0x1C, 0x22, 0x08};
static const uint8_t BMP_MOON[7]     = {0x1C, 0x38, 0x70, 0x70, 0x70, 0x38, 0x1C};
static const uint8_t BMP_CLOUD[7]    = {0x00, 0x0C, 0x3E, 0x7F, 0x7F, 0x3E, 0x00};
static const uint8_t BMP_SMALLSUN[4] = {0x38, 0x7C, 0x7C, 0x38};               // rows 0-3
static const uint8_t BMP_SIDECLOUD[4]= {0x06, 0x1F, 0x3F, 0x1F};               // rows 3-6
static const uint8_t BMP_TOPCLOUD[4] = {0x0C, 0x3E, 0x7F, 0x3E};               // rows 0-3
static const uint8_t BMP_DROPS[2]    = {0x2A, 0x54};                           // rows 5-6
static const uint8_t BMP_FLAKES[2]   = {0x55, 0x2A};                           // rows 5-6
static const uint8_t BMP_BOLT[3]     = {0x18, 0x0C, 0x18};                     // rows 4-6
static const uint8_t BMP_CANOPY[3]   = {0x1C, 0x3E, 0x7F};                     // rows 0-2
static const uint8_t BMP_HANDLE[4]   = {0x08, 0x08, 0x28, 0x10};               // rows 3-6

static void drawBits(int x, int y, const uint8_t* rows, uint8_t n, uint16_t c) {
  for (uint8_t r = 0; r < n; r++)
    for (uint8_t b = 0; b < 7; b++)
      if (rows[r] & (0x40 >> b)) dma_display->drawPixel(x + b, y + r, c);
}

static void drawWxIcon(int x, int y, uint8_t icon) {
  uint16_t grey   = dma_display->color565(150, 150, 165);
  uint16_t yellow = dma_display->color565(255, 190, 0);
  switch (icon) {
    case WX_SUN:    drawBits(x, y, BMP_SUN, 7, yellow); break;
    case WX_MOON:   drawBits(x, y, BMP_MOON, 7, dma_display->color565(210, 210, 160)); break;
    case WX_PARTLY: drawBits(x, y, BMP_SMALLSUN, 4, yellow);
                    drawBits(x, y + 3, BMP_SIDECLOUD, 4, grey); break;
    case WX_RAIN:   drawBits(x, y, BMP_TOPCLOUD, 4, grey);
                    drawBits(x, y + 5, BMP_DROPS, 2, dma_display->color565(60, 130, 255)); break;
    case WX_SNOW:   drawBits(x, y, BMP_TOPCLOUD, 4, grey);
                    drawBits(x, y + 5, BMP_FLAKES, 2, dma_display->color565(230, 230, 255)); break;
    case WX_STORM:  drawBits(x, y, BMP_TOPCLOUD, 4, grey);
                    drawBits(x, y + 4, BMP_BOLT, 3, yellow); break;
    default:        drawBits(x, y, BMP_CLOUD, 7, grey); break;
  }
}

static void drawUmbrella(int x, int y) {
  drawBits(x, y, BMP_CANOPY, 3, dma_display->color565(80, 170, 255));
  drawBits(x, y + 3, BMP_HANDLE, 4, dma_display->color565(180, 180, 180));
}

static int glyphAdvance(char c) {
  if (c >= 1 && c <= CH_UMBRELLA) return 8;   // icons: 7 px + 1 px gap
  if (c == CH_DEGREE) return 4;
  if (c == CH_GAP)    return 3;
  return 6;
}

int runWidth(const char* text) {
  int w = 0;
  for (const char* p = text; *p; p++) w += glyphAdvance(*p);
  return w;
}

// Draws text with icons at (x, y), skipping anything off-panel.
// Returns the x just past the end.
int drawRun(const char* text, int x, int y, uint16_t color) {
  dma_display->setTextColor(color);
  for (const char* p = text; *p; p++) {
    char c = *p;
    int adv = glyphAdvance(c);
    if (x > -adv && x < PANEL_RES_X) {
      if (c >= WX_SUN && c <= WX_STORM) drawWxIcon(x, y, c);
      else if (c == CH_UMBRELLA)        drawUmbrella(x, y);
      else if (c == CH_DEGREE)          dma_display->drawRect(x, y, 3, 3, color);
      else if (c == CH_GAP)             { /* spacer */ }
      else { dma_display->setCursor(x, y); dma_display->write(c); }
    }
    x += adv;
  }
  return x;
}

// e.g. [cloud]52° [umbrella]
void buildWeatherText(const PanelState& s) {
  wx_part[0] = '\0';
  if (!s.wx_show) return;
  int n = 0;
  if (s.wx_icon >= WX_SUN && s.wx_icon <= WX_STORM) wx_part[n++] = (char)s.wx_icon;
  n += snprintf(wx_part + n, sizeof(wx_part) - n, "%d%c", s.wx_temp, CH_DEGREE);
  if (s.wx_umbrella && n + 3 < (int)sizeof(wx_part)) {
    wx_part[n++] = CH_GAP;
    wx_part[n++] = CH_UMBRELLA;
    wx_part[n] = '\0';
  }
}

// Cycles every 4 s: green 2x2 dots -> yellow 2x2 dots -> dim full white.
// Dots sit in all four corners (1 px in from the edges, like the status dot)
// and the centre. A pixel missing from every dot means a signal problem;
// one missing in only one spot (or a dark LED on white) means a bad LED.
void drawTestPattern() {
  static const int16_t spots[][2] = {{1, 1}, {61, 1}, {1, 29}, {61, 29}, {31, 15}};
  uint8_t phase = (millis() / 4000) % 3;
  dma_display->clearScreen();
  if (phase == 2) {
    dma_display->fillRect(0, 0, PANEL_RES_X, PANEL_RES_Y, dma_display->color565(80, 80, 80));
  } else {
    uint16_t c = phase == 0 ? dma_display->color565(0, 200, 80) : dma_display->color565(255, 200, 0);
    for (auto& p : spots) dma_display->fillRect(p[0], p[1], 2, 2, c);
  }
  present();
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
