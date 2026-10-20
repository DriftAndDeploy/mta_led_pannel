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
#define FW_VERSION "6.5.0"
#define FW_BUILD   650

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
  uint8_t  brightness     = 80;
  uint8_t  latch_blanking = LATCH_BLANKING;
  uint8_t  service_status = 0;      // 0 normal, 1 delays/changes, 2 suspended
  uint16_t test_s         = 0;      // seconds of test pattern requested
  bool     wx_show        = false;  // weather strip on and data available
  int16_t  wx_temp        = 0;      // degrees F
  uint8_t  wx_icon        = 0;      // WX_* below (0 = none)
  bool     wx_umbrella    = false;  // rain likely in the next 12 h
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
  n.brightness     = constrain((int)(doc["brightness"] | 80), 0, 100);
  n.latch_blanking = constrain((int)(doc["latch_blanking"] | (int)LATCH_BLANKING), 1, 8);
  n.service_status = doc["service_status"] | 0;
  n.test_s         = doc["test_pattern"] | 0;
  n.wx_show        = (doc["wx"] | 0) != 0;
  n.wx_temp        = doc["wx_temp"] | 0;
  n.wx_icon        = doc["wx_icon"] | 0;
  n.wx_umbrella    = (doc["wx_umbrella"] | 0) != 0;
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
    snprintf(text, sizeof(text), "%s%s%s", wx_part, leave_part, alert_part);
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

  const int WX_GAP = 12;          // space after the weather in the ticker
  bool weather   = s.wx_show && wx_part[0];
  bool scrolling = (leave_part[0] || alert_part[0]);

  dma_display->setTextSize(1);
  if (scrolling) {
    int cx = scroll_x;
    if (weather) cx = drawRun(wx_part, cx, TEXT_Y, WX_TEXT_COLOR) + WX_GAP;
    cx = drawRun(leave_part, cx, TEXT_Y, dma_display->color565(255, 255, 255));  // leave by: white
    cx = drawRun(alert_part, cx, TEXT_Y, dma_display->color565(255, 220, 0));    // alert: yellow
    // Clip: blank the logo column so glyphs slide smoothly under its edge.
    dma_display->fillRect(0, 16, SCROLL_MIN, 16, 0);

    scroll_x--;
    if (cx < SCROLL_MIN) scroll_x = PANEL_RES_X;   // whole ticker has passed
  } else if (weather) {
    // Nothing to scroll: weather sits still beside the logo.
    int w = runWidth(wx_part);
    drawRun(wx_part, SCROLL_MIN + (PANEL_RES_X - SCROLL_MIN - w) / 2, TEXT_Y, WX_TEXT_COLOR);
  }

  drawLogoAt(scrolling || weather ? 8 : 32, 24, s);
}

// Minimalist layout: [logo][5px][time] centred as one unit + status dot.
// With weather on, that row moves up 3 px and the weather sits centred below.
void drawMinimalist(const PanelState& s) {
  const int GAP = 5;
  bool weather   = s.wx_show && wx_part[0];
  int cy         = weather ? 13 : 16;   // logo centre row
  int timeWidth  = strlen(s.train_time) * 6;
  int totalWidth = 14 + GAP + timeWidth;
  int startX     = (PANEL_RES_X - totalWidth) / 2;

  drawLogoAt(startX + 7, cy, s);

  dma_display->setTextSize(1);
  dma_display->setTextColor(dma_display->color565(s.time_r, s.time_g, s.time_b));
  dma_display->setCursor(startX + 14 + GAP, cy - 4);
  dma_display->print(s.train_time);

  if (weather) drawRun(wx_part, (PANEL_RES_X - runWidth(wx_part)) / 2, 24, WX_TEXT_COLOR);

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
