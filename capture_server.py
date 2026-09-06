"""
Memento of Self — Persistent Capture Server
Initializes all cameras ONCE and keeps them alive, waiting for repeated
trigger signals. Eliminates per-visitor init/teardown overhead.

Usage:
  py -3.10 capture_server.py
  Then press Enter each time you want to capture (or trigger via file/socket).
  Type 'quit' + Enter to shut down cleanly.
"""

import PySpin
import os
import gc
import json
import time
from datetime import datetime
from pathlib import Path
import json

OUTPUT_DIR = "captures"
IMAGE_FORMAT = PySpin.SPINNAKER_IMAGE_FILE_FORMAT_JPEG
WARMUP_SECONDS = 2.0
LOCKED_SETTINGS_FILE = "locked_camera_settings.json"

def get_ordered_cameras(cam_list, order_file="camera_order.json"):
    """
    Reorder cameras by serial number according to a saved mapping,
    so camera_0/camera_1/etc. always refer to the same physical camera
    regardless of PySpin's detection order.
    """
    if not Path(order_file).exists():
        print(f"WARNING: {order_file} not found, using default detection order.")
        return list(cam_list)

    with open(order_file) as f:
        order_map = json.load(f)  # serial (str) -> desired index

    cameras_by_serial = {}
    for cam in cam_list:
        cam.Init()  # need Init() before reading serial in some cases; safe to call once
        serial = str(cam.DeviceSerialNumber.GetValue())
        cameras_by_serial[serial] = cam

    ordered = [None] * len(order_map)
    for serial, index in order_map.items():
        if serial in cameras_by_serial:
            ordered[index] = cameras_by_serial[serial]
        else:
            print(f"WARNING: expected camera serial {serial} not found among connected cameras.")

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
    """Run AWB/AE warmup ONCE, lock values, save them for reuse."""
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
    """Fire a single capture. Cameras are assumed already initialized + locked."""
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

    # Reuse saved settings if available, otherwise calibrate now
    if Path(LOCKED_SETTINGS_FILE).exists():
        print(f"\nFound saved settings in {LOCKED_SETTINGS_FILE}, reusing (skip warmup).")
        with open(LOCKED_SETTINGS_FILE) as f:
            settings = json.load(f)
        apply_locked_settings(ordered_cams, settings)
    else:
        settings = calibrate_once(ordered_cams)

    processor = PySpin.ImageProcessor()
    processor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)

    print("\nCameras ready and staying initialized.")
    print("Press Enter to capture. Type 'recalibrate' to redo exposure lock. Type 'quit' to exit.\n")

    last_folder = None
    try:
        while True:
            cmd = input("> ").strip().lower()
            if cmd == "quit":
                break
            elif cmd == "recalibrate":
                settings = calibrate_once(ordered_cams)
            else:
                last_folder = capture_all(ordered_cams, processor)
                folder_name = Path(last_folder).name
                with open("_last_capture_folder.txt", "w") as f:
                    f.write(folder_name)

                # Automatically run reconstruction + training + printing.
                # This blocks the capture prompt until done, since only one
                # printer/GPU is available -- next visitor waits until this
                # finishes. Runs as a separate process so a crash here can't
                # take down the camera server itself.
                print(f"\nStarting post-processing for {folder_name} ...")
                import subprocess
                subprocess.run(
                    f'py -3.10 .\\post_process.py "{folder_name}"',
                    shell=True
                )
                print("\nReady for next capture.")
    finally:
        print("\nShutting down cameras...")
        cleanup(ordered_cams, system)
        print("Done.")


if __name__ == "__main__":
    main()