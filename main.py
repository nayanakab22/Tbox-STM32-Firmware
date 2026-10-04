from machine import UART
import modem
import _thread
import checkNet
import utime
import log
import net
import ujson
import ntptime
import dataCall
import uos
import request
from misc import Power
from gnss import GnssGetData

log.basicConfig(level=log.INFO)
logger = log.getLogger("EC600_WEB")
# ============================================================
# CONFIGURATION
SERVER_URL = "https://ec600-web.onrender.com/api/data"

# Send latest data every time STM32 sends new data
SEND_INTERVAL = 1
# UART2 = STM32 UART
uart = UART(UART.UART2, 115200, 8, 0, 1, 0)

# GPS module (adjust UART index/pins to match your wiring)
gnss = GnssGetData(1, 9600, 8, 0, 1, 0)
GPS_READ_INTERVAL = 1     # seconds between GPS reads
# ============================================================
# GLOBAL DATA
mcuMsg = {}
IMEI = ""
last_send_time = 0
last_received_time = 0

gps_data = {"Latitude": None, "Longitude": None, "gps_time": 0}
gps_lock = _thread.allocate_lock()

MCU_VERSION_FILE = '/usr/mcu-version.dat'
GIT_VERSION_URL = 'https://raw.githubusercontent.com/nayanakab22/Tbox-STM32-Firmware/main/version.json'
# Add this near your MCU_VERSION_FILE definition
EC_CURRENT_VERSION = "1.2"
EC_SCRIPT_NAME = '/usr/main.py'  # Must match the name of the script QuecPython boots from
is_mcu_ota_active = False
ota_ready_event = 0 # 0 = waiting, 1 = ready, -1 = fail

def get_local_version():
    try:
        with open(MCU_VERSION_FILE, 'r') as f: return f.read().strip()
    except: return ""

def save_local_version(v):
    try:
        with open(MCU_VERSION_FILE, 'w') as f: f.write(str(v))
    except Exception as e:
        print("Failed to save local version:", e)
# ============================================================
# GET IMEI
def GetDevImei():
    global IMEI
    try:
        IMEI = modem.getDevImei()
        print("Device IMEI:", IMEI)
    except Exception as e:
        print("IMEI Error:", e)
        IMEI = ""
# ============================================================
# GPS HELPERS
def to_decimal(value, direction, max_deg):
    """
    Return signed decimal degrees.
    - If value is already decimal degrees (abs <= max_deg), only apply the sign.
    - If value is raw NMEA ddmm.mmmm / dddmm.mmmm, convert it.
    max_deg: 90 for latitude, 180 for longitude.
    """
    try:
        v = float(value)
        if abs(v) > max_deg:                 # raw NMEA format
            deg = int(v / 100)
            v = deg + (v - deg * 100) / 60.0
        if str(direction).upper() in ("S", "W"):
            v = -abs(v)
        return round(v, 6)
    except Exception:
        return None

def is_version_greater(v1, v2):
    try:
        parts1 = [int(x) for x in str(v1).split('.')]
        parts2 = [int(x) for x in str(v2).split('.')]
        return parts1 > parts2
    except: return False

def mcu_ota_thread(download_url):
    global is_mcu_ota_active, ota_ready_event
    is_mcu_ota_active = True
    firmware_path = '/usr/app_update.bin'
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        print("OTA Attempt {}/{} from: {}".format(attempt, max_attempts, download_url))
        
        # 1. Ensure a clean slate by deleting any leftover corrupted files
        try:
            uos.remove(firmware_path)
        except Exception:
            pass

        try:
            response = request.get(download_url)
            if response.status_code != 200:
                print("HTTP Error", response.status_code, "- Check if GitHub repo is Private!")
                break # Do not retry 404/403 errors
                
            with open(firmware_path, 'wb') as f:
                data_source = getattr(response, 'content', getattr(response, 'text', b""))
                if type(data_source) in (str, bytes):
                    data_source = [data_source]
                for chunk in data_source:
                    try: f.write(chunk)
                    except Exception:
                        try: f.write(chunk.encode('latin-1'))
                        except Exception: f.write(bytes(chunk))
            
            f_size = uos.stat(firmware_path)[6]
            print("Downloaded firmware size:", f_size, "bytes")
            
            if f_size < 5000:
                print("Error: File too small! Aborting.")
                break # Do not retry if the file itself is fundamentally wrong
                
        except Exception as e:
            print("Download failed:", type(e).__name__, str(e))
            utime.sleep(3)
            continue # Loop around and redownload

        # 2. Handshake with STM32
        ota_ready_event = 0
        uart.write(b"OTA_START")
        print("Sent OTA_START to MCU")
        
        timeout = 30
        while ota_ready_event == 0 and timeout > 0:
            utime.sleep(1)
            timeout -= 1
            
        if ota_ready_event != 1:
            print("MCU did not respond with READY after reset.")
            utime.sleep(3)
            continue # Retry the whole process

        # 3. Stream to MCU
        transfer_success = False
        print("MCU ready. Starting chunk transfer.")
        try:
            with open(firmware_path, 'rb') as f:
                while True:
                    chunk = f.read(512)
                    if not chunk:
                        print("OTA Transfer Complete.")
                        transfer_success = True
                        break
                    if len(chunk) < 512:
                        chunk += b'\xFF' * (512 - len(chunk))

                    ota_ready_event = 0
                    uart.write(chunk)
                    
                    chunk_timeout = 10
                    while ota_ready_event == 0 and chunk_timeout > 0:
                        utime.sleep(0.5)
                        chunk_timeout -= 0.5
                        
                    if ota_ready_event == -1:
                        print("MCU reported FLASH_FAIL")
                        break
                    elif ota_ready_event == 0:
                        print("Timeout waiting for chunk READY")
                        break
        except Exception as e:
            print("Error during OTA transfer:", type(e).__name__, str(e))
            
        # 4. Cleanup the file from the EC600 memory immediately 
        try:
            uos.remove(firmware_path)
            print("Deleted local .bin file to free memory.")
        except Exception:
            pass
            
        # 5. Evaluate Success
        if transfer_success:
            break # Exit the retry loop!
        else:
            print("OTA failed this attempt. Retrying...")
            utime.sleep(5)
            # Loop restarts, downloading a fresh copy of the file

    is_mcu_ota_active = False
    print("Exiting OTA mode, resuming normal UART.")
def ec600_ota_thread(download_url):
    print("Starting EC600 Script OTA from:", download_url)
    try:
        response = request.get(download_url)
        with open(EC_SCRIPT_NAME, 'wb') as f:
            data_source = getattr(response, 'content', getattr(response, 'text', b""))
            
            if type(data_source) in (str, bytes):
                data_source = [data_source]
                
            for chunk in data_source:
                if isinstance(chunk, str):
                    f.write(chunk.encode('utf-8'))
                else:
                    f.write(chunk)
                    
        print("EC600 Script downloaded successfully. Rebooting to apply!")
        utime.sleep(2)
        Power.powerRestart()
    except Exception as e:
        print("EC600 OTA failed:", type(e).__name__, str(e))    

def check_updates_on_boot():
    print("Checking for MCU & EC600 updates on Git...")
    local_mcu_version = get_local_version()
    
    try:
        response = request.get(GIT_VERSION_URL)
        if response.status_code == 200:
            raw_bytes = b""
            data_source = getattr(response, 'content', getattr(response, 'text', []))
            
            if type(data_source) in (str, bytes):
                data_source = [data_source]
                
            for chunk in data_source:
                if isinstance(chunk, str):
                    raw_bytes += chunk.encode('utf-8')
                else:
                    raw_bytes += chunk
                    
            remote_data = ujson.loads(raw_bytes) 
            
            # 1. EC600 Update Check
            remote_ec_version = remote_data.get("ec_version")
            ec_url = remote_data.get("ec_url")
            
            if remote_ec_version and ec_url:
                if is_version_greater(remote_ec_version, EC_CURRENT_VERSION):
                    print("New EC600 script found:", remote_ec_version, ">", EC_CURRENT_VERSION)
                    _thread.start_new_thread(ec600_ota_thread, (ec_url,))
                    return # Stop here to allow reboot
                else:
                    print("EC600 script is up to date.")

            # 2. MCU Update Check
            if not local_mcu_version:
                print("No local MCU version found. Skipping MCU check on this boot.")
                return

            remote_version = remote_data.get("version")
            firmware_url = remote_data.get("url")
            
            if remote_version and firmware_url:
                if is_version_greater(remote_version, local_mcu_version):
                    print("New MCU version found:", remote_version, ">", local_mcu_version)
                    _thread.start_new_thread(mcu_ota_thread, (firmware_url,))
                else:
                    print("MCU firmware is up to date.")
        else:
            print("Failed to fetch version JSON from Git. HTTP Code:", response.status_code)
    except Exception as e:
        print("Error during Git update check:", type(e).__name__, str(e))
def gps_thread():
    global gps_data
    print("GPS Thread Started")
    while True:
        try:
            gnss.read_gnss_data(3, 0)
            loc = gnss.getLocation()
            if loc != -1:
                lon, lon_dir, lat, lat_dir = loc
                la = to_decimal(lat, lat_dir, 90)
                lo = to_decimal(lon, lon_dir, 180)
                if la is not None and lo is not None:
                    with gps_lock:
                        gps_data = { "Latitude": la,"Longitude": lo,"gps_time": int(utime.time())}
                    print("GPS: lat={} lon={}".format(la, lo))
                else:
                    print("GPS: bad values, skipping:", loc)
            else:
                print("GPS: no fix yet, raw:", gnss.getOriginalData())
        except Exception as e:
            print("GPS THREAD ERROR:", e)
        utime.sleep(GPS_READ_INTERVAL)
# ============================================================
# SEND DATA TO WEB SERVER
def send_to_server(data):
    global last_send_time
    try:
        if not data:
            print("No MCU data to send")
            return False
        # Add EC600 information
        payload = {}
        payload.update(data)
        payload["IMEI"] = IMEI
        payload["tboxId"] = IMEI
        payload["server_time"] = int(utime.time())

        # Attach latest GPS fix (nested, so the dashboard can
        # read result.gps.Latitude / result.gps.Longitude)
        with gps_lock:
            if gps_data:
                payload["gps"] = dict(gps_data)

        # Convert dictionary to JSON
        json_payload = ujson.dumps(payload)
        print("")
        print("========== HTTP SEND ==========")
        print("URL:", SERVER_URL)
        print("Payload length:", len(json_payload))
        print(json_payload)
        # HTTP Headers
        headers = {
            "Content-Type": "application/json"}
        # POST to Render
        response = request.post(
            SERVER_URL,
            data=json_payload,
            headers=headers,
            timeout=20
        )

        print("HTTP Status:", response.status_code)
        try:
            print("Server Response:", response.text)
        except Exception:
            pass
        # Check result
        if response.status_code >= 200 and response.status_code < 300:
            print(">>> DATA SENT SUCCESSFULLY <<<")
            last_send_time = utime.time()
            return True
        else:
            print(">>> SERVER ERROR <<<")
            return False
    except Exception as e:
        print("HTTP SEND ERROR:", e)
        return False
# UART RECEIVE
def uart_recv_thread():
    global mcuMsg, last_received_time, ota_ready_event
    print("\n======================================")
    print("UART Receive Thread Started")
    print("Waiting for STM32...\n======================================")
    rx_buffer = ""
    
    while True:
        try:
            if uart.any() > 0:
                bytes_avail = uart.any()
                data = uart.read(bytes_avail)
                if data is None:
                    utime.sleep_ms(10)
                    continue
                
                try:
                    received = data.decode("utf-8", "ignore")
                except Exception:
                    received = ""
                    
                if received:
                    rx_buffer += received
                    
                    # --- OTA Interception ---
                    if is_mcu_ota_active:
                        if "READY" in rx_buffer:
                            ota_ready_event = 1
                            rx_buffer = ""
                        elif "FLASH_FAIL" in rx_buffer:
                            ota_ready_event = -1
                            rx_buffer = ""
                        
                        if len(rx_buffer) > 500:
                            rx_buffer = "" 
                        continue 
                    # ------------------------

                    # --- Robust JSON Extraction (Bracket Counting) ---
                    while "{" in rx_buffer:
                        start_idx = rx_buffer.find("{")
                        bracket_count = 0
                        end_idx = -1
                        
                        # Count brackets to find the true end of the nested JSON
                        for i in range(start_idx, len(rx_buffer)):
                            if rx_buffer[i] == "{":
                                bracket_count += 1
                            elif rx_buffer[i] == "}":
                                bracket_count -= 1
                                
                            if bracket_count == 0:
                                end_idx = i
                                break
                                
                        if end_idx != -1:
                            # We found a complete JSON object!
                            json_string = rx_buffer[start_idx:end_idx + 1]
                            rx_buffer = rx_buffer[end_idx + 1:] # Remove it from buffer
                            
                            try:
                                parsed_data = ujson.loads(json_string)
                                
                                sv = parsed_data.get("sv")
                                if sv is not None:
                                    current_sv = get_local_version()
                                    if current_sv != str(sv):
                                        save_local_version(sv)
                                        print("Saved new MCU version locally:", sv)

                                mcuMsg.clear()
                                mcuMsg.update(parsed_data)
                                last_received_time = utime.time()
                                send_to_server(mcuMsg)
                            except Exception as e:
                                print("JSON PARSE ERROR:", e)
                        else:
                            # JSON is incomplete, break loop and wait for more UART bytes
                            break
                        
                    # Prevent memory overflow if garbage data accumulates
                    if len(rx_buffer) > 4096:
                        rx_buffer = rx_buffer[-1000:]
                    # -------------------------------------------------
            else:
                utime.sleep_ms(50)
        except Exception as e:
            print("UART THREAD ERROR:", e)
            utime.sleep(1)
# NETWORK CALLBACK
def nw_cb(args):
    print("Network callback:", args)
# MAIN
def main():
    try:
        print("EC600 T-BOX WEB CLIENT")
        try:
            ntptime.settime()
            utime.setTimeZone(5) 
            print("Time synchronized")
        except Exception as e:
            print("NTP failed:", e)
        #Get IMEI
        GetDevImei()
        check_updates_on_boot()
        # Start UART thread
        print("Starting UART receive thread...")
        _thread.start_new_thread(uart_recv_thread,())
        print("UART thread started")
        # Start GPS thread
        print("Starting GPS thread...")
        _thread.start_new_thread( gps_thread, ())
        print("GPS thread started")
        # Keep main alive
        while True:
            utime.sleep(10)
    except Exception as e:
        print("MAIN ERROR:", e)

# PROGRAM START
if __name__ == "__main__":
    PROJECT_NAME = "EC600 T-BOX WEB"
    PROJECT_VERSION = "1.0.0"
    try:
        # APN
        net.setApn("dialogbb", 0)
        net.setModemFun(0)
        utime.sleep(2)
        net.setModemFun(1)
        utime.sleep(2)
        net.setApn("dialogbb", 0)
        print("")
        print("APN:", net.getApn(0))
        # Network checker
        checknet = checkNet.CheckNetwork(
            PROJECT_NAME,
            PROJECT_VERSION
        )
        checknet.poweron_print_once()
        print("")
        print("Waiting for network...")
        stagecode, subcode = \
            checknet.wait_network_connected(30)
        print(
            "Network status: stage={}, sub={}".format(
                stagecode,
                subcode
            )
        )
        # Network OK
        if subcode == 1 and stagecode == 3:
            print("4G NETWORK CONNECTED")
            # Register network callback
            try:
                dataCall.setCallback(nw_cb)
            except Exception as e:
                print("Callback error:", e)
            # Start application
            main()
        # Network failed
        else:
            print("")
            print("NETWORK CONNECTION FAILED")
            print(
                "stagecode = {} subcode = {}".format(
                    stagecode,
                    subcode))
            print("Restarting in 10 seconds...")
            utime.sleep(10)
            Power.powerRestart()
    except Exception as e:
        print("FATAL ERROR")
        print(e)
        utime.sleep(10)
        Power.powerRestart()
