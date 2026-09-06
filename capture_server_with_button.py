"""
Memento of Self — Persistent Capture Server (with physical button support)
Initializes all cameras ONCE and keeps them alive, waiting for repeated
trigger signals -- either pressing Enter in this terminal, OR pressing
the physical Arduino button.

Usage:
  py -3.10 capture_server_with_button.py
  Then press Enter, or press the physical button, each time you want to capture.
  Type 'quit' + Enter to shut down cleanly.
"""

import PySpin
import os
import gc
import json
import time
import threading
from datetime import datetime
from pathlib import Path

try:
    import serial
except ImportError:
    serial = None

OUTPUT_DIR = "captures"
IMAGE_FORMAT = PySpin.SPINNAKER_IMAGE_FILE_FORMAT_JPEG
WARMUP_SECONDS = 2.0
LOCKED_SETTINGS_FILE = "assets/locked_camera_settings.json"

# --- Arduino config ---
ARDUINO_PORT = "COM4"   # <-- update to match your Arduino's actual COM port
ARDUINO_BAUD = 9600

# Shared flag the Arduino listener thread sets; main loop checks it
button_pressed = threading.Event()
stop_listener = threading.Event()


def arduino_listener():
    """Background thread: watches the serial port for 'TRIGGER' lines."""
    if serial is None:
        print("pyserial not installed -- physical button will not work. "
              "Run: py -3.10 -m pip install pyserial")
        return

    try:
        ser = serial.Serial(ARDUINO_PORT, ARDUINO_BAUD, timeout=1)
        print(f"Arduino listener started on {ARDUINO_PORT}.")
    except Exception as e:
        print(f"Could not open Arduino port {ARDUINO_PORT}: {e}")
        print("Physical button will not work, but Enter key still will.")
        return

    while not stop_listener.is_set():
        try:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                print(f"[DEBUG] serial received: {repr(line)}")
            if line == "triggered":
                print("\n[Physical button pressed]")
                button_pressed.set()
        except Exception as e:
            print(f"[DEBUG] serial read error: {e}")

    ser.close()


def get_ordered_cameras(cam_list, order_file="assets/camera_order.json"):
    if not Path(order_file).exists():
        print(f"WARNING: {order_file} not found, using default detection order.")
        return list(cam_list)

    with open(order_file) as f:
        order_map = json.load(f)

    cameras_by_serial = {}
    for cam in cam_list:
        cam.Init()
        serial_num = str(cam.DeviceSerialNumber.GetValue())
        cameras_by_serial[serial_num] = cam

    ordered = [None] * len(order_map)
    for s, index in order_map.items():
        if s in cameras_by_serial:
            ordered[index] = cameras_by_serial[s]
        else:
            print(f"WARNING: expected camera serial {s} not found among connected cameras.")

    if any(c is None for c in ordered):
        print("WARNING: not all expected cameras were found, falling back to detected order.")
        return list(cam_list)

    return ordered


def setup_camera(cam, index):
    cam.Init()
    cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
    cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)
    cam.TriggerSource.SetValue(PySpin.TriggerSource_Software)
    cam.TriggerMode.SetValue(PySpin.TriggerMode_On)

    cam.GevSCPSPacketSize.SetValue(9000)
    cam.GevSCPD.SetValue(50000)
    try:
        cam.GevSCFTD.SetValue(index * 200000)
    except:
        pass

    s_node_map = cam.GetTLStreamNodeMap()
    buffer_count = PySpin.CIntegerPtr(s_node_map.GetNode("StreamBufferCountManual"))
    if PySpin.IsAvailable(buffer_count) and PySpin.IsWritable(buffer_count):
        buffer_count.SetValue(10)
    buffer_mode = PySpin.CEnumerationPtr(s_node_map.GetNode("StreamBufferHandlingMode"))
    if PySpin.IsAvailable(buffer_mode) and PySpin.IsWritable(buffer_mode):
        mode_entry = buffer_mode.GetEntryByName("NewestOnly")
        buffer_mode.SetIntValue(mode_entry.GetValue())

    print(f"  camera_{index} = Serial {cam.DeviceSerialNumber.GetValue()}")


def calibrate_once(cam_list):
    print(f"\nCalibrating exposure/white balance ({WARMUP_SECONDS}s warmup)...")
    for cam in cam_list:
        cam.BalanceWhiteAuto.SetValue(PySpin.BalanceWhiteAuto_Continuous)
        cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Continuous)
        cam.GainAuto.SetValue(PySpin.GainAuto_Continuous)
        cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)
        cam.BeginAcquisition()

    start = time.time()
    while time.time() - start < WARMUP_SECONDS:
        for cam in cam_list:
            try:
                frame = cam.GetNextImage(100)
                frame.Release()
            except:
                pass

    for cam in cam_list:
        cam.EndAcquisition()

    time.sleep(0.5)

    ref_cam = cam_list[0]
    exposure_time = ref_cam.ExposureTime.GetValue()
    gain = ref_cam.Gain.GetValue()
    ref_cam.BalanceRatioSelector.SetValue(PySpin.BalanceRatioSelector_Red)
    wb_red = ref_cam.BalanceRatio.GetValue()
    ref_cam.BalanceRatioSelector.SetValue(PySpin.BalanceRatioSelector_Blue)
    wb_blue = ref_cam.BalanceRatio.GetValue()

    settings = {
        "exposure_time": exposure_time,
        "gain": gain,
        "wb_red": wb_red,
        "wb_blue": wb_blue,
    }
    with open(LOCKED_SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

    apply_locked_settings(cam_list, settings)
    print(f"  Locked and saved: exposure={exposure_time:.1f}us gain={gain:.2f}dB")
    return settings


def apply_locked_settings(cam_list, settings):
    for i, cam in enumerate(cam_list):
        cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
        cam.GainAuto.SetValue(PySpin.GainAuto_Off)
        cam.BalanceWhiteAuto.SetValue(PySpin.BalanceWhiteAuto_Off)
        cam.ExposureTime.SetValue(settings["exposure_time"])
        cam.Gain.SetValue(settings["gain"])
        cam.BalanceRatioSelector.SetValue(PySpin.BalanceRatioSelector_Red)
        cam.BalanceRatio.SetValue(settings["wb_red"])
        cam.BalanceRatioSelector.SetValue(PySpin.BalanceRatioSelector_Blue)
        cam.BalanceRatio.SetValue(settings["wb_blue"])
        cam.TriggerMode.SetValue(PySpin.TriggerMode_On)


def capture_all(cam_list, processor):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(OUTPUT_DIR, timestamp)
    os.makedirs(session_dir, exist_ok=True)

    for cam in cam_list:
        cam.BeginAcquisition()

    for cam in cam_list:
        cam.TriggerSoftware.Execute()
        time.sleep(0.05)

    for i, cam in enumerate(cam_list):
        try:
            image_result = cam.GetNextImage(10000)
            if image_result.IsIncomplete():
                status = image_result.GetImageStatus()
                print(f"  WARNING: Camera {i} image incomplete. Status: {status}")
                image_result.Release()
                continue
            image_converted = processor.Convert(image_result, PySpin.PixelFormat_RGB8)
            filename = os.path.join(session_dir, f"camera_{i}.jpg")
            image_converted.Save(filename)
            print(f"  camera_{i} saved")
            image_result.Release()
        except PySpin.SpinnakerException as e:
            print(f"  ERROR on camera {i}: {e}")

    for cam in cam_list:
        cam.EndAcquisition()

    print(f"Capture complete: {session_dir}")
    return session_dir


def cleanup(cam_list, system):
    for i, cam in enumerate(cam_list):
        try:
            cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)
            cam.DeInit()
        except Exception as e:
            print(f"  Warning camera {i}: {e}")
        del cam
    del cam_list
    gc.collect()
    try:
        system.ReleaseInstance()
    except:
        pass


STATUS_FILE = "assets/_booth_status.json"

PREVIEW_CAMERA_INDEX = 0  # whichever camera_N you want to preview from
SPOUT_SENDER_NAME = "MementoPreview"

preview_running = threading.Event()

try:
    import SpoutGL
    from OpenGL import GL
    SPOUT_AVAILABLE = True
except ImportError:
    SPOUT_AVAILABLE = False
    print("SpoutGL not installed -- live preview will not work. "
          "Run: py -3.10 -m pip install SpoutGL PyOpenGL")


def preview_stream_worker(cam, processor):
    """
    Runs in its own thread. Puts the preview camera into continuous/free-run
    mode and continuously sends frames to TouchDesigner via Spout (live GPU
    texture sharing -- no disk writes, real video framerate).
    """
    try:
        cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)
        cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
        cam.BeginAcquisition()
    except Exception as e:
        print(f"[Preview] Could not start continuous mode: {e}")
        return

    spout_sender = None
    if SPOUT_AVAILABLE:
        try:
            spout_sender = SpoutGL.SpoutSender()
            spout_sender.setSenderName(SPOUT_SENDER_NAME)
            print(f"[Preview] Spout sender '{SPOUT_SENDER_NAME}' started.")
        except Exception as e:
            print(f"[Preview] Could not start Spout sender: {e}")
            spout_sender = None

    while preview_running.is_set():
        try:
            image_result = cam.GetNextImage(200)
            if not image_result.IsIncomplete():
                image_converted = processor.Convert(image_result, PySpin.PixelFormat_RGB8)
                if spout_sender is not None:
                    arr = image_converted.GetNDArray()  # HxWx3, uint8, RGB
                    h, w = arr.shape[0], arr.shape[1]
                    spout_sender.sendImage(
                        arr.tobytes(), w, h, GL.GL_RGB, False, 0
                    )
            image_result.Release()
        except Exception:
            pass

    if spout_sender is not None:
        try:
            spout_sender.releaseSender()
        except Exception:
            pass

    try:
        cam.EndAcquisition()
    except Exception:
        pass


def stop_preview_and_arm_trigger(cam):
    """Called right as the countdown begins: stop free-run, switch back to
    software trigger mode so this camera is ready for the synchronized capture."""
    preview_running.clear()
    time.sleep(0.15)  # brief pause to let the streaming thread exit its loop cleanly
    try:
        cam.TriggerSource.SetValue(PySpin.TriggerSource_Software)
        cam.TriggerMode.SetValue(PySpin.TriggerMode_On)
    except Exception as e:
        print(f"[Preview] Could not re-arm trigger mode: {e}")


def write_status(state, extra=None):
    """Write current booth state to a file TouchDesigner can watch."""
    data = {"state": state, "timestamp": time.time()}
    if extra:
        data.update(extra)
    with open(STATUS_FILE, "w") as f:
        json.dump(data, f)


def run_capture_and_process(ordered_cams, processor):
    """Runs one capture + post-process cycle."""
    write_status("countdown")

    # Keep the live preview streaming through the countdown itself, so
    # visitors see themselves live while "3, 2, 1" plays. Only switch the
    # preview camera back to trigger mode right at the very end, just
    # before the synchronized group capture fires.
    time.sleep(3)  # matches the 3-2-1 countdown video length

    write_status("smile")
    stop_preview_and_arm_trigger(ordered_cams[PREVIEW_CAMERA_INDEX])

    folder = capture_all(ordered_cams, processor)
    folder_name = Path(folder).name
    with open("assets/_last_capture_folder.txt", "w") as f:
        f.write(folder_name)

    write_status("reconstructing", {"folder": folder_name})
    print(f"\nStarting post-processing for {folder_name} ...")
    import subprocess
    subprocess.run(
        f'py -3.10 .\\post_process.py "{folder_name}"',
        shell=True
    )

    print("\nReady for next capture.")

    # After a display period, return to idle. post_process.py already wrote
    # the "receipt" status as soon as the render was live, so this just
    # controls how long the reveal screen stays up before resetting.
    time.sleep(8)
    write_status("welcome")

    # Restart the live preview stream for the next visitor
    preview_running.set()
    preview_thread = threading.Thread(
        target=preview_stream_worker,
        args=(ordered_cams[PREVIEW_CAMERA_INDEX], processor),
        daemon=True
    )
    preview_thread.start()


def main():
    system = PySpin.System.GetInstance()
    cam_list = system.GetCameras()
    ordered_cams = get_ordered_cameras(cam_list)
    num_cameras = cam_list.GetSize()

    if num_cameras == 0:
        print("No cameras found.")
        cam_list.Clear()
        system.ReleaseInstance()
        return

    print(f"Found {num_cameras} camera(s). Initializing (once)...")
    for i, cam in enumerate(ordered_cams):
        setup_camera(cam, i)

    if Path(LOCKED_SETTINGS_FILE).exists():
        print(f"\nFound saved settings in {LOCKED_SETTINGS_FILE}, reusing (skip warmup).")
        with open(LOCKED_SETTINGS_FILE) as f:
            settings = json.load(f)
        apply_locked_settings(ordered_cams, settings)
    else:
        settings = calibrate_once(ordered_cams)

    processor = PySpin.ImageProcessor()
    processor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)

    # Start the Arduino listener in the background
    listener_thread = threading.Thread(target=arduino_listener, daemon=True)
    listener_thread.start()

    # Start the live preview stream on the designated camera
    preview_running.set()
    preview_thread = threading.Thread(
        target=preview_stream_worker,
        args=(ordered_cams[PREVIEW_CAMERA_INDEX], processor),
        daemon=True
    )
    preview_thread.start()

    print("\nCameras ready and staying initialized.")
    print("Press Enter, or press the physical button, to capture.")
    print("Type 'recalibrate' + Enter to redo exposure lock.")
    print("Type 'quit' + Enter to exit.\n")

    write_status("welcome")

    import msvcrt

    keyboard_buffer = ""

    try:
        while True:
            # Check the physical button first (non-blocking)
            if button_pressed.is_set():
                print("[DEBUG] button_pressed flag detected in main loop")
                button_pressed.clear()
                run_capture_and_process(ordered_cams, processor)
                continue

            # Non-blocking keyboard check (Windows only)
            if msvcrt.kbhit():
                ch = msvcrt.getwche()
                if ch in ("\r", "\n"):
                    cmd = keyboard_buffer.strip().lower()
                    keyboard_buffer = ""
                    print()  # newline after Enter
                    if cmd == "quit":
                        break
                    elif cmd == "recalibrate":
                        settings = calibrate_once(ordered_cams)
                    else:
                        run_capture_and_process(ordered_cams, processor)
                else:
                    keyboard_buffer += ch

            time.sleep(0.05)  # avoid pegging a CPU core while idling
    finally:
        stop_listener.set()
        preview_running.clear()
        time.sleep(0.2)
        print("\nShutting down cameras...")
        cleanup(ordered_cams, system)
        print("Done.")


if __name__ == "__main__":
    main()