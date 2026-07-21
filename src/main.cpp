#include <Arduino.h>
#include <DNSServer.h>
#include <M5Unified.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_sleep.h>
#include <time.h>

namespace {

constexpr uint32_t kBackground = 0x10131A;
constexpr uint32_t kPanel = 0x1B202B;
constexpr uint32_t kMuted = 0x7F8A9A;
constexpr uint32_t kWhite = 0xF4F7FB;
constexpr uint32_t kGreen = 0x20D98B;
constexpr uint32_t kAmber = 0xFFB84D;
constexpr uint32_t kRed = 0xFF5D6C;
constexpr uint32_t kBlue = 0x4EA1FF;
constexpr uint32_t kPurple = 0xB178FF;

constexpr uint16_t kUsagePort = 47322;
constexpr uint32_t kUsbPriorityMs = 30000;
constexpr uint32_t kWifiRetryMs = 30000;
constexpr uint32_t kRefreshTimeoutMs = 60000;
constexpr uint32_t kScreenTimeoutMs = 15000;
constexpr uint32_t kBatteryPollMs = 15000;
constexpr uint32_t kAutoRefreshDelayMs = 1500;
constexpr uint32_t kWifiControlRetryMs = 1000;
constexpr int64_t kTimezoneOffsetSeconds = 8 * 60 * 60;
constexpr uint8_t kDisplayBrightness = 80;
constexpr gpio_num_t kFrontButtonPin = GPIO_NUM_11;
constexpr gpio_num_t kSideButtonPin = GPIO_NUM_12;
constexpr uint64_t kButtonWakeMask = (1ULL << kFrontButtonPin) | (1ULL << kSideButtonPin);
constexpr char kPortalPassword[] = "codecmaster";
constexpr uint8_t kMaxRadarModels = 12;

enum class ScreenPage : uint8_t { Codex = 0, Vibe = 1, Radar = 2 };
enum class TimeRange : uint8_t { H24 = 0, D7 = 1, D30 = 2 };
enum class DataSource : uint8_t { Cache = 0, Usb = 1, Wifi = 2 };
enum class RefreshState : uint8_t { Idle = 0, Pending = 1, Success = 2, Failed = 3 };

struct PeriodUsage {
  int32_t costCents = -1;
  int64_t totalTokens = -1;
};

struct RadarModel {
  String name;
  int16_t scoreTenths = -1;
};

struct UsageSnapshot {
  String host;
  int64_t fetchedEpoch = 0;
  int16_t quotaRemaining = -1;
  int16_t quotaUsed = -1;
  int64_t quotaResetEpoch = 0;
  PeriodUsage periods[3];
  String radarUpdated;
  uint8_t radarCount = 0;
  RadarModel radarModels[kMaxRadarModels];
};

Preferences prefs;
UsageSnapshot usage;
ScreenPage currentPage = ScreenPage::Codex;
TimeRange currentRange = TimeRange::H24;
uint8_t currentRadarPage = 0;
DataSource dataSource = DataSource::Cache;
String preferredHost;
String serialLine;
uint32_t snapshotMillis = 0;
uint32_t lastUsbMillis = 0;
uint32_t lastWifiMillis = 0;
RefreshState refreshState = RefreshState::Idle;
uint32_t refreshStateMillis = 0;

DNSServer dnsServer;
WebServer webServer(80);
WiFiUDP usageUdp;
bool portalRunning = false;
bool udpRunning = false;
String portalSsid;
String savedSsid;
String savedPassword;
uint32_t lastWifiAttempt = 0;
bool provisioningScreen = false;
bool lastWifiConnected = false;
uint32_t sidePressedAt = 0;
bool sideLongHandled = false;
uint32_t frontPressedAt = 0;
bool frontLongHandled = false;
bool screenAwake = true;
bool ignoreButtonsUntilReleased = false;
bool autoRefreshPending = false;
uint32_t autoRefreshStartedMillis = 0;
uint32_t lastButtonActivityMillis = 0;
int cachedBatteryLevel = -1;
uint32_t batteryReadMillis = 0;
bool batteryLevelInitialized = false;
uint32_t lastWifiRefreshRequestMillis = 0;

uint32_t colorForRemaining(int value) {
  if (value >= 50) return kGreen;
  if (value >= 20) return kAmber;
  return kRed;
}

String htmlEscape(const String& value) {
  String result;
  result.reserve(value.length() + 16);
  for (size_t i = 0; i < value.length(); ++i) {
    const char ch = value[i];
    if (ch == '&') result += F("&amp;");
    else if (ch == '<') result += F("&lt;");
    else if (ch == '>') result += F("&gt;");
    else if (ch == '\"') result += F("&quot;");
    else result += ch;
  }
  return result;
}

String formatTokens(int64_t tokens) {
  if (tokens < 0) return "--";
  char buffer[24];
  if (tokens >= 1000000000LL) {
    snprintf(buffer, sizeof(buffer), "%.2fB", static_cast<double>(tokens) / 1000000000.0);
  } else if (tokens >= 1000000LL) {
    snprintf(buffer, sizeof(buffer), "%.1fM", static_cast<double>(tokens) / 1000000.0);
  } else if (tokens >= 1000LL) {
    snprintf(buffer, sizeof(buffer), "%.1fK", static_cast<double>(tokens) / 1000.0);
  } else {
    snprintf(buffer, sizeof(buffer), "%lld", static_cast<long long>(tokens));
  }
  return String(buffer);
}

String formatUsdAmount(int32_t cents) {
  if (cents < 0) return "--";
  char buffer[24];
  snprintf(buffer, sizeof(buffer), "%ld.%02ld", static_cast<long>(cents / 100),
           static_cast<long>(abs(cents % 100)));
  return String(buffer);
}

String formatRadarScore(int16_t tenths) {
  if (tenths < 0) return "--";
  char buffer[12];
  snprintf(buffer, sizeof(buffer), "%d.%d", tenths / 10, abs(tenths % 10));
  return String(buffer);
}

String formatUpdateTime(int64_t epoch) {
  if (epoch <= 0) return "--";
  const time_t localEpoch = static_cast<time_t>(epoch + kTimezoneOffsetSeconds);
  struct tm value;
  if (gmtime_r(&localEpoch, &value) == nullptr) return "--";
  char buffer[16];
  strftime(buffer, sizeof(buffer), "%m/%d %H:%M", &value);
  return String(buffer);
}

String countdownText() {
  if (usage.quotaResetEpoch <= 0 || usage.fetchedEpoch <= 0) return "--";
  const int64_t elapsed = static_cast<int64_t>((millis() - snapshotMillis) / 1000);
  int64_t seconds = usage.quotaResetEpoch - usage.fetchedEpoch - elapsed;
  if (seconds < 0) seconds = 0;
  const int64_t days = seconds / 86400;
  const int64_t hours = (seconds % 86400) / 3600;
  const int64_t minutes = (seconds % 3600) / 60;
  if (days > 0) return String(static_cast<long>(days)) + "d " + String(static_cast<long>(hours)) + "h";
  if (hours > 0) return String(static_cast<long>(hours)) + "h " + String(static_cast<long>(minutes)) + "m";
  return String(static_cast<long>(minutes)) + "m";
}

String sourceText() {
  if (dataSource == DataSource::Usb) return "USB";
  if (dataSource == DataSource::Wifi) return "WIFI";
  return "SAVED";
}

String shortHost() {
  if (usage.host.isEmpty()) return "NO DATA";
  if (usage.host.length() <= 18) return usage.host;
  return usage.host.substring(0, 17) + "~";
}

void drawProgressBar(int x, int y, int width, int height, int percent, uint32_t color) {
  M5.Display.fillRoundRect(x, y, width, height, height / 2, kPanel);
  const int innerWidth = max(0, (width - 4) * constrain(percent, 0, 100) / 100);
  if (innerWidth > 0) {
    M5.Display.fillRoundRect(x + 2, y + 2, innerWidth, height - 4, (height - 4) / 2, color);
  }
}

int batteryLevel() {
  const uint32_t now = millis();
  if (!batteryLevelInitialized || now - batteryReadMillis >= kBatteryPollMs) {
    const int level = M5.Power.getBatteryLevel();
    cachedBatteryLevel = level < 0 ? -1 : constrain(level, 0, 100);
    batteryReadMillis = now;
    batteryLevelInitialized = true;
  }
  return cachedBatteryLevel;
}

void drawHeader(const String& title, uint32_t titleColor) {
  const int level = batteryLevel();
  const uint32_t batteryColor = level < 0 ? kMuted : colorForRemaining(level);
  const String levelText = level < 0 ? "--%" : String(level) + "%";

  M5.Display.setTextSize(1.0f, 1.0f);
  M5.Display.setTextFont(1);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextColor(titleColor, kBackground);
  M5.Display.drawString(title, 8, 8);

  M5.Display.setTextColor(batteryColor, kBackground);
  M5.Display.setTextDatum(top_right);
  const int textRight = M5.Display.width() - 5;
  const int iconX = textRight - M5.Display.textWidth(levelText) - 19;
  M5.Display.drawRect(iconX, 8, 13, 8, batteryColor);
  M5.Display.fillRect(iconX + 13, 10, 2, 4, batteryColor);
  if (level > 0) {
    const int fillWidth = max(1, 9 * level / 100);
    M5.Display.fillRect(iconX + 2, 10, fillWidth, 4, batteryColor);
  }
  M5.Display.drawString(levelText, textRight, 7);
}

String refreshFooterText() {
  if (refreshState == RefreshState::Pending) return "REFRESHING...";
  if (refreshState == RefreshState::Success) return "REFRESH OK";
  if (refreshState == RefreshState::Failed) return "REFRESH FAILED";
  if (dataSource == DataSource::Cache) return "SAVED DATA";
  return sourceText() + " CONNECTED";
}

uint32_t refreshFooterColor() {
  if (refreshState == RefreshState::Pending) return kBlue;
  if (refreshState == RefreshState::Success) return kGreen;
  if (refreshState == RefreshState::Failed) return kRed;
  return kMuted;
}

void drawFooter(const String& radarUpdated = "") {
  M5.Display.setTextDatum(bottom_center);
  M5.Display.setTextFont(1);
  if (!radarUpdated.isEmpty()) {
    M5.Display.setTextColor(kMuted, kBackground);
    M5.Display.drawString("RADAR " + radarUpdated, M5.Display.width() / 2, 207);
  }
  M5.Display.setTextColor(kMuted, kBackground);
  M5.Display.drawString("UPDATED " + formatUpdateTime(usage.fetchedEpoch),
                        M5.Display.width() / 2, 222);
  M5.Display.setTextColor(refreshFooterColor(), kBackground);
  if (refreshState == RefreshState::Idle && dataSource != DataSource::Cache) {
    M5.Display.setTextColor(kGreen, kBackground);
  }
  M5.Display.drawString(refreshFooterText(), M5.Display.width() / 2, 237);
}

void drawCodexScreen() {
  drawHeader("CODEX", kBlue);
  M5.Display.setTextDatum(top_center);
  M5.Display.setTextColor(kMuted, kBackground);
  M5.Display.setTextFont(1);
  M5.Display.drawString("WEEK LEFT", M5.Display.width() / 2, 29);

  const uint32_t accent = usage.quotaRemaining < 0 ? kMuted : colorForRemaining(usage.quotaRemaining);
  M5.Display.setTextColor(accent, kBackground);
  M5.Display.setTextFont(7);
  const String percent = usage.quotaRemaining < 0 ? "--" : String(usage.quotaRemaining);
  M5.Display.drawString(percent, M5.Display.width() / 2 - 7, 44);
  M5.Display.setTextFont(2);
  M5.Display.setTextDatum(top_left);
  M5.Display.drawString("%", M5.Display.width() - 29, 72);
  drawProgressBar(14, 108, M5.Display.width() - 28, 14, max(0, static_cast<int>(usage.quotaRemaining)), accent);

  M5.Display.fillRoundRect(10, 135, M5.Display.width() - 20, 54, 9, kPanel);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextFont(1);
  M5.Display.setTextColor(kMuted, kPanel);
  M5.Display.drawString("RESETS IN", 20, 145);
  M5.Display.setTextColor(kWhite, kPanel);
  M5.Display.setTextFont(2);
  M5.Display.drawString(countdownText(), 20, 160);
  drawFooter();
}

void drawRangeTabs() {
  const char* labels[] = {"24H", "7D", "30D"};
  for (int i = 0; i < 3; ++i) {
    const int x = 8 + i * 42;
    const bool selected = static_cast<int>(currentRange) == i;
    M5.Display.fillRoundRect(x, 34, 36, 20, 7, selected ? kPurple : kPanel);
    M5.Display.setTextDatum(middle_center);
    M5.Display.setTextFont(1);
    M5.Display.setTextColor(selected ? kWhite : kMuted, selected ? kPurple : kPanel);
    M5.Display.drawString(labels[i], x + 18, 44);
  }
}

void drawVibeScreen() {
  const PeriodUsage& selected = usage.periods[static_cast<int>(currentRange)];
  drawHeader("VIBE USAGE", kPurple);
  drawRangeTabs();

  M5.Display.setTextDatum(top_left);
  M5.Display.setTextFont(1);
  M5.Display.setTextColor(kMuted, kBackground);
  M5.Display.drawString("USD EST. COST", 12, 66);
  M5.Display.setTextColor(kWhite, kBackground);
  M5.Display.setTextFont(4);
  // Draw the dollar sign ourselves instead of relying on the large bitmap
  // font's ambiguous currency glyph at this screen size.
  M5.Display.drawString("S", 12, 79);
  const int dollarLineX = 12 + M5.Display.textWidth("S") / 2;
  M5.Display.drawFastVLine(dollarLineX - 1, 77, 23, kWhite);
  M5.Display.drawFastVLine(dollarLineX, 77, 23, kWhite);
  M5.Display.drawString(formatUsdAmount(selected.costCents), 32, 79);

  M5.Display.fillRoundRect(10, 124, M5.Display.width() - 20, 65, 9, kPanel);
  M5.Display.setTextFont(1);
  M5.Display.setTextColor(kMuted, kPanel);
  M5.Display.drawString("TOTAL TOKENS", 20, 136);
  M5.Display.setTextColor(kGreen, kPanel);
  M5.Display.setTextFont(4);
  M5.Display.drawString(formatTokens(selected.totalTokens), 20, 151);
  drawFooter();
}

uint8_t radarPageCount() {
  return usage.radarCount == 0 ? 1 : (usage.radarCount + 2) / 3;
}

void drawRadarScreen() {
  const uint8_t pageCount = radarPageCount();
  if (currentRadarPage >= pageCount) currentRadarPage = 0;

  drawHeader("CODEX RADAR", kAmber);

  if (usage.radarCount == 0) {
    M5.Display.fillRoundRect(10, 56, M5.Display.width() - 20, 100, 9, kPanel);
    M5.Display.setTextDatum(middle_center);
    M5.Display.setTextFont(2);
    M5.Display.setTextColor(kMuted, kPanel);
    M5.Display.drawString("WAITING FOR", M5.Display.width() / 2, 96);
    M5.Display.drawString("RADAR DATA", M5.Display.width() / 2, 117);
  } else {
    const uint32_t rankColors[3] = {kAmber, kBlue, kPurple};
    const int first = currentRadarPage * 3;
    for (int row = 0; row < 3; ++row) {
      const int index = first + row;
      if (index >= usage.radarCount) break;
      const int y = 34 + row * 52;
      M5.Display.fillRoundRect(8, y, M5.Display.width() - 16, 48, 8, kPanel);
      M5.Display.setTextDatum(top_center);
      M5.Display.setTextFont(4);
      M5.Display.setTextSize(0.82f, 1.0f);
      M5.Display.setTextColor(kWhite, kPanel);
      M5.Display.drawString(usage.radarModels[index].name, M5.Display.width() / 2, y + 4);
      M5.Display.setTextSize(1.0f, 1.0f);
      M5.Display.setTextDatum(top_center);
      M5.Display.setTextFont(2);
      M5.Display.setTextColor(rankColors[row], kPanel);
      M5.Display.drawString("#" + String(index + 1) + "   " +
                            formatRadarScore(usage.radarModels[index].scoreTenths),
                            M5.Display.width() / 2, y + 30);
    }
  }
  drawFooter(usage.radarUpdated.isEmpty() ? "--" : usage.radarUpdated);
}

void drawProvisioningScreen() {
  const bool connected = WiFi.status() == WL_CONNECTED;
  drawHeader("WI-FI SETUP", kBlue);
  M5.Display.setTextDatum(top_center);
  M5.Display.setTextFont(2);

  if (connected) {
    M5.Display.fillRoundRect(10, 48, M5.Display.width() - 20, 105, 10, kPanel);
    M5.Display.setTextColor(kGreen, kPanel);
    M5.Display.setTextFont(4);
    M5.Display.drawString("WI-FI", M5.Display.width() / 2, 65);
    M5.Display.setTextFont(2);
    M5.Display.drawString("CONNECTED", M5.Display.width() / 2, 103);
    M5.Display.setTextFont(1);
    M5.Display.setTextColor(kMuted, kPanel);
    M5.Display.drawString(WiFi.SSID(), M5.Display.width() / 2, 130);
  } else if (portalRunning) {
    M5.Display.setTextFont(1);
    M5.Display.setTextColor(kMuted, kBackground);
    M5.Display.drawString("CONNECT TO HOTSPOT", M5.Display.width() / 2, 43);
    M5.Display.setTextFont(2);
    M5.Display.setTextColor(kAmber, kBackground);
    M5.Display.drawString(portalSsid, M5.Display.width() / 2, 63);
    M5.Display.setTextFont(1);
    M5.Display.setTextColor(kMuted, kBackground);
    M5.Display.drawString("PASSWORD", M5.Display.width() / 2, 102);
    M5.Display.setTextFont(2);
    M5.Display.setTextColor(kWhite, kBackground);
    M5.Display.drawString(kPortalPassword, M5.Display.width() / 2, 119);
    M5.Display.setTextFont(1);
    M5.Display.setTextColor(kMuted, kBackground);
    M5.Display.drawString("OPEN 192.168.4.1", M5.Display.width() / 2, 158);
  } else {
    M5.Display.setTextColor(kBlue, kBackground);
    M5.Display.setTextFont(2);
    M5.Display.drawString("CONNECTING...", M5.Display.width() / 2, 92);
  }

  M5.Display.setTextDatum(bottom_center);
  M5.Display.setTextFont(1);
  M5.Display.setTextColor(kMuted, kBackground);
  M5.Display.drawString("Side: back", M5.Display.width() / 2, 235);
}

void drawScreen() {
  if (!screenAwake) return;
  M5.Display.startWrite();
  M5.Display.fillScreen(kBackground);
  if (provisioningScreen) drawProvisioningScreen();
  else if (currentPage == ScreenPage::Codex) drawCodexScreen();
  else if (currentPage == ScreenPage::Vibe) drawVibeScreen();
  else drawRadarScreen();
  M5.Display.endWrite();
}

bool sendWifiControl(const String& event) {
  if (!udpRunning || WiFi.status() != WL_CONNECTED) return false;
  String message = event;
  if (!preferredHost.isEmpty()) message += "," + preferredHost;

  bool delivered = false;
  const IPAddress globalBroadcast(255, 255, 255, 255);
  if (usageUdp.beginPacket(globalBroadcast, kUsagePort)) {
    usageUdp.print(message);
    delivered = usageUdp.endPacket() == 1;
  }

  IPAddress subnetBroadcast = WiFi.localIP();
  const IPAddress subnetMask = WiFi.subnetMask();
  bool differsFromGlobal = false;
  for (int i = 0; i < 4; ++i) {
    subnetBroadcast[i] = subnetBroadcast[i] | static_cast<uint8_t>(~subnetMask[i]);
    differsFromGlobal = differsFromGlobal || subnetBroadcast[i] != 255;
  }
  if (differsFromGlobal && usageUdp.beginPacket(subnetBroadcast, kUsagePort)) {
    usageUdp.print(message);
    delivered = usageUdp.endPacket() == 1 || delivered;
  }
  return delivered;
}

void sleepScreen() {
  if (!screenAwake) return;
  screenAwake = false;
  Serial.println("STICKS3_SCREEN,OFF");
  Serial.flush();
  sendWifiControl("STICKS3_SCREEN_OFF");
  delay(20);
  WiFi.mode(WIFI_OFF);
  M5.Display.sleep();
  M5.Display.waitDisplay();
  esp_sleep_enable_ext1_wakeup(kButtonWakeMask, ESP_EXT1_WAKEUP_ANY_LOW);
  esp_deep_sleep_start();
}

void serviceScreenTimeout() {
  if (screenAwake && millis() - lastButtonActivityMillis >= kScreenTimeoutMs) sleepScreen();
}

void saveUiState() {
  prefs.begin("usage-ui", false);
  prefs.putUChar("page", static_cast<uint8_t>(currentPage));
  prefs.putUChar("range", static_cast<uint8_t>(currentRange));
  prefs.putUChar("radarPage", currentRadarPage);
  prefs.end();
}

void saveSnapshot() {
  prefs.begin("usage-data", false);
  prefs.putString("host", usage.host);
  prefs.putLong64("fetched", usage.fetchedEpoch);
  prefs.putInt("remain", usage.quotaRemaining);
  prefs.putInt("used", usage.quotaUsed);
  prefs.putLong64("reset", usage.quotaResetEpoch);
  for (int i = 0; i < 3; ++i) {
    prefs.putInt((String("cost") + i).c_str(), usage.periods[i].costCents);
    prefs.putLong64((String("tok") + i).c_str(), usage.periods[i].totalTokens);
  }
  prefs.putString("radarUpdate", usage.radarUpdated);
  prefs.putUChar("radarCount", usage.radarCount);
  for (int i = 0; i < kMaxRadarModels; ++i) {
    prefs.putString((String("rname") + i).c_str(), usage.radarModels[i].name);
    prefs.putInt((String("rscore") + i).c_str(), usage.radarModels[i].scoreTenths);
  }
  prefs.putString("preferred", preferredHost);
  prefs.end();
}

void loadState() {
  prefs.begin("usage-ui", false);
  currentPage = static_cast<ScreenPage>(constrain(static_cast<int>(prefs.getUChar("page", 0)), 0, 2));
  currentRange = static_cast<TimeRange>(constrain(static_cast<int>(prefs.getUChar("range", 0)), 0, 2));
  currentRadarPage = prefs.getUChar("radarPage", 0);
  prefs.end();

  prefs.begin("usage-data", false);
  usage.host = prefs.getString("host", "");
  usage.fetchedEpoch = prefs.getLong64("fetched", 0);
  usage.quotaRemaining = prefs.getInt("remain", -1);
  usage.quotaUsed = prefs.getInt("used", -1);
  usage.quotaResetEpoch = prefs.getLong64("reset", 0);
  for (int i = 0; i < 3; ++i) {
    usage.periods[i].costCents = prefs.getInt((String("cost") + i).c_str(), -1);
    usage.periods[i].totalTokens = prefs.getLong64((String("tok") + i).c_str(), -1);
  }
  usage.radarUpdated = prefs.getString("radarUpdate", "");
  usage.radarCount = min(static_cast<int>(prefs.getUChar("radarCount", 0)),
                         static_cast<int>(kMaxRadarModels));
  for (int i = 0; i < kMaxRadarModels; ++i) {
    usage.radarModels[i].name = prefs.getString((String("rname") + i).c_str(), "");
    usage.radarModels[i].scoreTenths = prefs.getInt((String("rscore") + i).c_str(), -1);
  }
  preferredHost = prefs.getString("preferred", "");
  prefs.end();
  snapshotMillis = millis();

  prefs.begin("usage-wifi", false);
  savedSsid = prefs.getString("ssid", "");
  savedPassword = prefs.getString("pass", "");
  prefs.end();
}

bool sameSnapshot(const UsageSnapshot& left, const UsageSnapshot& right) {
  if (left.host != right.host || left.fetchedEpoch != right.fetchedEpoch ||
      left.quotaRemaining != right.quotaRemaining || left.quotaUsed != right.quotaUsed ||
      left.quotaResetEpoch != right.quotaResetEpoch) return false;
  for (int i = 0; i < 3; ++i) {
    if (left.periods[i].costCents != right.periods[i].costCents ||
        left.periods[i].totalTokens != right.periods[i].totalTokens) return false;
  }
  if (left.radarUpdated != right.radarUpdated || left.radarCount != right.radarCount) return false;
  for (int i = 0; i < left.radarCount; ++i) {
    if (left.radarModels[i].name != right.radarModels[i].name ||
        left.radarModels[i].scoreTenths != right.radarModels[i].scoreTenths) return false;
  }
  return true;
}

bool parseUsageLine(const String& line, UsageSnapshot& parsed) {
  const bool isV2 = line.startsWith("USAGE2,");
  const bool isV3 = line.startsWith("USAGE3,");
  if (!isV2 && !isV3) return false;
  String fields[40];
  int fieldIndex = 0;
  int start = 0;
  for (int i = 0; i <= static_cast<int>(line.length()) && fieldIndex < 40; ++i) {
    if (i == static_cast<int>(line.length()) || line[i] == ',') {
      fields[fieldIndex++] = line.substring(start, i);
      start = i + 1;
    }
  }
  if (fieldIndex < 12 || fields[1].isEmpty()) return false;
  if (isV2 && (fieldIndex != 12 || fields[0] != "USAGE2")) return false;
  if (isV3 && (fieldIndex < 14 || fields[0] != "USAGE3")) return false;
  parsed.host = fields[1];
  parsed.fetchedEpoch = strtoll(fields[2].c_str(), nullptr, 10);
  parsed.quotaRemaining = constrain(fields[3].toInt(), -1L, 100L);
  parsed.quotaUsed = constrain(fields[4].toInt(), -1L, 100L);
  parsed.quotaResetEpoch = strtoll(fields[5].c_str(), nullptr, 10);
  for (int i = 0; i < 3; ++i) {
    parsed.periods[i].costCents = static_cast<int32_t>(strtol(fields[6 + i * 2].c_str(), nullptr, 10));
    parsed.periods[i].totalTokens = strtoll(fields[7 + i * 2].c_str(), nullptr, 10);
  }

  if (isV3) {
    const int radarCount = fields[13].toInt();
    if (radarCount < 0 || radarCount > kMaxRadarModels || fieldIndex != 14 + radarCount * 2) return false;
    parsed.radarUpdated = fields[12];
    parsed.radarCount = radarCount;
    for (int i = 0; i < radarCount; ++i) {
      parsed.radarModels[i].name = fields[14 + i * 2];
      parsed.radarModels[i].scoreTenths = static_cast<int16_t>(fields[15 + i * 2].toInt());
    }
  } else {
    // A Mac with the previous bridge can still update Codex/Vibe while the
    // last known radar list remains cached on the device.
    parsed.radarUpdated = usage.radarUpdated;
    parsed.radarCount = usage.radarCount;
    for (int i = 0; i < usage.radarCount; ++i) parsed.radarModels[i] = usage.radarModels[i];
  }
  return parsed.fetchedEpoch > 0;
}

void requestRefresh() {
  const uint32_t now = millis();
  refreshState = RefreshState::Pending;
  refreshStateMillis = now;
  lastButtonActivityMillis = now;
  lastWifiRefreshRequestMillis = 0;
  Serial.println("STICKS3_REFRESH");
  if (sendWifiControl("STICKS3_REFRESH")) lastWifiRefreshRequestMillis = now;
  drawScreen();
}

bool applyUsageLine(const String& line, DataSource incomingSource) {
  UsageSnapshot parsed;
  if (!parseUsageLine(line, parsed)) return false;
  const uint32_t now = millis();

  if (incomingSource == DataSource::Wifi) {
    if (lastUsbMillis != 0 && now - lastUsbMillis < kUsbPriorityMs) return false;
    if (!preferredHost.isEmpty() && parsed.host != preferredHost) return false;
    lastWifiMillis = now;
  } else if (incomingSource == DataSource::Usb) {
    lastUsbMillis = now;
    if (preferredHost != parsed.host) preferredHost = parsed.host;
  }

  const bool changed = !sameSnapshot(usage, parsed) || dataSource != incomingSource;
  usage = parsed;
  if (currentRadarPage >= radarPageCount()) currentRadarPage = 0;
  dataSource = incomingSource;
  snapshotMillis = now;
  const bool refreshCompleted = refreshState == RefreshState::Pending;
  if (refreshCompleted) {
    refreshState = RefreshState::Success;
    refreshStateMillis = now;
    lastButtonActivityMillis = now;
  }
  if (changed || refreshCompleted) {
    if (changed) saveSnapshot();
    drawScreen();
  }
  if (incomingSource == DataSource::Usb) Serial.println("USAGE_ACK");
  return true;
}

void readSerial() {
  while (Serial.available()) {
    const char ch = static_cast<char>(Serial.read());
    if (ch == '\n') {
      serialLine.trim();
      if (serialLine == "STICKS3_REFRESH_ACK") {
        if (refreshState == RefreshState::Pending) {
          refreshState = RefreshState::Success;
          refreshStateMillis = millis();
          lastButtonActivityMillis = refreshStateMillis;
          drawScreen();
          Serial.println("STICKS3_REFRESH_DONE");
        }
      } else if (!serialLine.isEmpty()) {
        applyUsageLine(serialLine, DataSource::Usb);
      }
      serialLine = "";
    } else if (ch != '\r' && serialLine.length() < 800) {
      serialLine += ch;
    }
  }
}

String portalPage(const String& message = "") {
  String page = F("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                  "<style>body{font:17px -apple-system;margin:36px;max-width:520px;background:#10131a;color:#f4f7fb}"
                  "input,button{box-sizing:border-box;width:100%;padding:13px;margin:8px 0;border-radius:9px;border:1px solid #454b58}"
                  "button{background:#4ea1ff;color:white;border:0;font-weight:700}</style>"
                  "<h2>CodeX Master Wi-Fi</h2>");
  if (!message.isEmpty()) page += "<p>" + htmlEscape(message) + "</p>";
  page += F("<form method=post action=/save><label>Wi-Fi name</label><input name=ssid required value='");
  page += htmlEscape(savedSsid);
  page += F("'><label>Password</label><input name=pass type=password><button>Save and connect</button></form>"
            "<p>Codex and Vibe credentials stay on the Mac. Only aggregate usage is sent here.</p>");
  return page;
}

void stopPortal() {
  if (!portalRunning) return;
  dnsServer.stop();
  webServer.stop();
  WiFi.softAPdisconnect(true);
  portalRunning = false;
}

void connectWifi() {
  if (savedSsid.isEmpty()) return;
  if (!portalRunning) WiFi.mode(WIFI_STA);
  else WiFi.mode(WIFI_AP_STA);
  WiFi.disconnect(false, false);
  WiFi.begin(savedSsid.c_str(), savedPassword.c_str());
  lastWifiAttempt = millis();
}

void startPortal() {
  if (portalRunning) return;
  uint64_t chipId = ESP.getEfuseMac();
  char suffix[5];
  snprintf(suffix, sizeof(suffix), "%04X", static_cast<unsigned>(chipId & 0xFFFF));
  portalSsid = String("StickS3-Usage-") + suffix;
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(portalSsid.c_str(), kPortalPassword);
  dnsServer.start(53, "*", WiFi.softAPIP());
  webServer.on("/", HTTP_GET, []() { webServer.send(200, "text/html", portalPage()); });
  webServer.on("/save", HTTP_POST, []() {
    const String newSsid = webServer.arg("ssid");
    const String newPass = webServer.arg("pass");
    if (newSsid.isEmpty()) {
      webServer.send(400, "text/html", portalPage("Wi-Fi name is required."));
      return;
    }
    savedSsid = newSsid;
    savedPassword = newPass;
    prefs.begin("usage-wifi", false);
    prefs.putString("ssid", savedSsid);
    prefs.putString("pass", savedPassword);
    prefs.end();
    webServer.send(200, "text/html", portalPage("Saved. The StickS3 is connecting now."));
    delay(250);
    stopPortal();
    connectWifi();
    drawScreen();
  });
  webServer.onNotFound([]() { webServer.sendHeader("Location", "http://192.168.4.1/"); webServer.send(302); });
  webServer.begin();
  portalRunning = true;
  drawScreen();
}

void serviceWifi() {
  if (portalRunning) {
    dnsServer.processNextRequest();
    webServer.handleClient();
  }
  const bool connected = WiFi.status() == WL_CONNECTED;
  if (connected) {
    if (!udpRunning) {
      usageUdp.begin(kUsagePort);
      udpRunning = true;
      sendWifiControl("STICKS3_SCREEN_ON");
    }
  } else {
    udpRunning = false;
    if (!savedSsid.isEmpty() && millis() - lastWifiAttempt >= kWifiRetryMs) connectWifi();
    if (provisioningScreen && !portalRunning && millis() - lastWifiAttempt >= 15000) startPortal();
  }

  if (connected != lastWifiConnected) {
    lastWifiConnected = connected;
    if (provisioningScreen) drawScreen();
  }

  if (!udpRunning) return;
  int packetSize = usageUdp.parsePacket();
  while (packetSize > 0) {
    char packet[800];
    const int count = usageUdp.read(packet, sizeof(packet) - 1);
    if (count > 0) {
      packet[count] = '\0';
      String line(packet);
      line.trim();
      applyUsageLine(line, DataSource::Wifi);
    }
    packetSize = usageUdp.parsePacket();
  }

  const uint32_t now = millis();
  if (refreshState == RefreshState::Pending &&
      now - lastWifiRefreshRequestMillis >= kWifiControlRetryMs &&
      sendWifiControl("STICKS3_REFRESH")) {
    lastWifiRefreshRequestMillis = now;
  }
}

void handleButtons() {
  const uint32_t now = millis();
  if (ignoreButtonsUntilReleased) {
    lastButtonActivityMillis = now;
    if (digitalRead(kFrontButtonPin) == HIGH && digitalRead(kSideButtonPin) == HIGH) {
      ignoreButtonsUntilReleased = false;
    }
    return;
  }

  if (M5.BtnB.wasPressed()) {
    lastButtonActivityMillis = now;
    sidePressedAt = now;
    sideLongHandled = false;
  }
  if (M5.BtnA.wasPressed()) {
    lastButtonActivityMillis = now;
    frontPressedAt = now;
    frontLongHandled = false;
  }
  if (M5.BtnA.isPressed() || M5.BtnB.isPressed()) {
    lastButtonActivityMillis = now;
  }

  if (M5.BtnB.isPressed() && sidePressedAt != 0 && !sideLongHandled &&
      now - sidePressedAt >= 1200) {
    sideLongHandled = true;
    provisioningScreen = true;
    if (WiFi.status() == WL_CONNECTED) stopPortal();
    else startPortal();
    drawScreen();
  }
  if (M5.BtnB.wasReleased()) {
    if (!sideLongHandled) {
      if (provisioningScreen) {
        provisioningScreen = false;
        stopPortal();
        if (!savedSsid.isEmpty() && WiFi.status() != WL_CONNECTED) connectWifi();
      } else {
        currentPage = static_cast<ScreenPage>((static_cast<int>(currentPage) + 1) % 3);
        saveUiState();
      }
      drawScreen();
    }
    sidePressedAt = 0;
    sideLongHandled = false;
  }

  if (provisioningScreen) {
    if (M5.BtnA.wasReleased()) {
      frontPressedAt = 0;
      frontLongHandled = false;
    }
    return;
  }
  if (M5.BtnA.isPressed() && frontPressedAt != 0 && !frontLongHandled &&
      now - frontPressedAt >= 1200) {
    frontLongHandled = true;
    requestRefresh();
  }
  if (M5.BtnA.wasReleased()) {
    if (!frontLongHandled) {
      if (currentPage == ScreenPage::Vibe) {
        currentRange = static_cast<TimeRange>((static_cast<int>(currentRange) + 1) % 3);
        saveUiState();
        drawScreen();
      } else if (currentPage == ScreenPage::Radar) {
        currentRadarPage = (currentRadarPage + 1) % radarPageCount();
        saveUiState();
        drawScreen();
      }
    }
    frontPressedAt = 0;
    frontLongHandled = false;
  }
}

}  // namespace

void setup() {
  // A full USAGE3 payload with 12 radar models is larger than the ESP32-S3
  // HW CDC default 256-byte RX queue. Allocate this before M5.begin() calls
  // Serial.begin(), otherwise the tail (including the newline) can be lost.
  Serial.setRxBufferSize(1024);
  const bool wokeByButton = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1;
  auto config = M5.config();
  config.serial_baudrate = 115200;
  config.internal_imu = false;
  config.internal_mic = false;
  config.internal_spk = false;
  M5.begin(config);
  M5.Display.setRotation(0);
  M5.Display.setBrightness(kDisplayBrightness);
  loadState();
  ignoreButtonsUntilReleased = wokeByButton;
  autoRefreshPending = wokeByButton;
  autoRefreshStartedMillis = millis();
  lastButtonActivityMillis = millis();
  drawScreen();

  if (savedSsid.isEmpty()) startPortal();
  else connectWifi();
  Serial.println("STICKS3_READY");
  Serial.printf("STICKS3_STATUS,BOARD=%d,BAT=%d,WAKE=%d\n", static_cast<int>(M5.getBoard()),
                batteryLevel(), wokeByButton);
}

void loop() {
  M5.update();
  readSerial();
  serviceWifi();
  handleButtons();
  if (autoRefreshPending && millis() - autoRefreshStartedMillis >= kAutoRefreshDelayMs) {
    autoRefreshPending = false;
    requestRefresh();
  }
  serviceScreenTimeout();

  if (refreshState == RefreshState::Pending && millis() - refreshStateMillis >= kRefreshTimeoutMs) {
    refreshState = RefreshState::Failed;
    refreshStateMillis = millis();
    drawScreen();
  } else if ((refreshState == RefreshState::Success || refreshState == RefreshState::Failed) &&
             millis() - refreshStateMillis >= 4000) {
    refreshState = RefreshState::Idle;
    drawScreen();
  }

  static uint32_t lastHeartbeat = 0;
  if (millis() - lastHeartbeat >= 2000) {
    lastHeartbeat = millis();
    Serial.printf("STICKS3_ALIVE,%d,%d,%s,SCREEN=%d,BAT=%d\n", M5.Display.width(),
                  M5.Display.height(), preferredHost.c_str(), screenAwake, batteryLevel());
  }

  static uint32_t lastMinuteDraw = 0;
  if (millis() - lastMinuteDraw >= 60000) {
    lastMinuteDraw = millis();
    drawScreen();
  }
  delay(5);
}
