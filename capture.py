"""
Memento of Self — Multi-Camera Capture Script
Captures one image from all connected FLIR Blackfly S cameras simultaneously.
Saves each image to the output folder as camera_0.jpg, camera_1.jpg, etc.

Requirements: PySpin (py -3.10), numpy<2
Usage: py -3.10 capture.py
"""

import PySpin
import os
import gc
import time
from datetime import datetime

# --- Config ---
OUTPUT_DIR = "captures"  # folder where images will be saved
IMAGE_FORMAT = PySpin.SPINNAKER_IMAGE_FILE_FORMAT_JPEG
WARMUP_SECONDS = 2.0  # how long to let AWB/AE settle


def setup_camera(cam, index):
    """Configure a single camera for capture."""
    cam.Init()
    cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
    cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)
    cam.TriggerSource.SetValue(PySpin.TriggerSource_Software)
    cam.TriggerMode.SetValue(PySpin.TriggerMode_On)

    # Auto white balance and exposure during warmup
    cam.BalanceWhiteAuto.SetValue(PySpin.BalanceWhiteAuto_Continuous)
    cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Continuous)
    cam.GainAuto.SetValue(PySpin.GainAuto_Continuous)

    # GigE packet settings
    cam.GevSCPSPacketSize.SetValue(9000)
    cam.GevSCPD.SetValue(50000)

    # Stagger transmission to avoid bandwidth collision
    try:
        cam.GevSCFTD.SetValue(index * 200000)
    except:
        pass

    # Stream buffer settings
    s_node_map = cam.GetTLStreamNodeMap()
    buffer_count = PySpin.CIntegerPtr(s_node_map.GetNode("StreamBufferCountManual"))
    if PySpin.IsAvailable(buffer_count) and PySpin.IsWritable(buffer_count):
        buffer_count.SetValue(10)
    buffer_mode = PySpin.CEnumerationPtr(s_node_map.GetNode("StreamBufferHandlingMode"))
    if PySpin.IsAvailable(buffer_mode) and PySpin.IsWritable(buffer_mode):
        mode_entry = buffer_mode.GetEntryByName("NewestOnly")
        buffer_mode.SetIntValue(mode_entry.GetValue())

    print(f"  camera_{index} = Serial {cam.DeviceSerialNumber.GetValue()}")

def capture_all(cam_list):
    """Run warmup, lock exposure, then trigger all cameras simultaneously."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(OUTPUT_DIR, timestamp)
    os.makedirs(session_dir, exist_ok=True)
    print(f"\nSaving images to: {session_dir}")

    processor = PySpin.ImageProcessor()
    processor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)

    # --- Step 1: Warmup in continuous mode so AWB/AE converge ---
    print(f"Warming up cameras for {WARMUP_SECONDS}s (AWB + AE settling)...")
    for cam in cam_list:
        cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)
        cam.BeginAcquisition()

    warmup_start = time.time()
    while time.time() - warmup_start < WARMUP_SECONDS:
        for cam in cam_list:
            try:
                frame = cam.GetNextImage(100)
                frame.Release()
            except:
                pass

    for cam in cam_list:
        cam.EndAcquisition()

    time.sleep(0.5)  # let cameras fully stop before locking and restarting

    # --- Step 3: Switch to trigger mode and capture ---
    print("\nSwitching to triggered capture...")
    for cam in cam_list:
        cam.TriggerMode.SetValue(PySpin.TriggerMode_On)
        cam.BeginAcquisition()

    print("Triggering all cameras...")
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
            print(f"  Camera {i} saved: {filename}")
            image_result.Release()
        except PySpin.SpinnakerException as e:
            print(f"  ERROR on camera {i}: {e}")

    for cam in cam_list:
        cam.EndAcquisition()

    print(f"\nCapture complete. Images saved to: {session_dir}")
    return session_dir


def cleanup(cam_list, system):
    for i in range(cam_list.GetSize()):
        cam = cam_list[i]
        try:
            cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)
            cam.DeInit()
        except Exception as e:
            print(f"  Warning camera {i}: {e}")
        del cam
    cam_list.Clear()
    del cam_list
    gc.collect()
    try:
        system.ReleaseInstance()
    except:
        pass
    print("Done.")


def main():
    system = PySpin.System.GetInstance()
    cam_list = system.GetCameras()
    num_cameras = cam_list.GetSize()

    if num_cameras == 0:
        print("No cameras found. Check connections and try again.")
        cam_list.Clear()
        system.ReleaseInstance()
        return

    print(f"Found {num_cameras} camera(s). Initializing...")
    for i, cam in enumerate(cam_list):
        setup_camera(cam, i)

    input("\nPress Enter to capture...")
    capture_all(cam_list)
    cleanup(cam_list, system)


if __name__ == "__main__":
    main()